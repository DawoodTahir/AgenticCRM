import os
import json
import requests
import hashlib
from datetime import datetime
from dotenv import load_dotenv
from logger import get_logger
from db.model import (
    get_connection, upsert_lead, upsert_lead_note,
    get_sync_cursor, save_sync_state
)

load_dotenv()
log = get_logger("monday")

API_URL     = "https://api.monday.com/v2"
API_KEY     = os.environ.get("MONDAY_API_KEY")
BOARD_ID    = os.environ.get("MONDAY_BOARD_ID")

if not API_KEY or not BOARD_ID:
    raise RuntimeError("MONDAY_API_KEY and MONDAY_BOARD_ID must be set in environment")

SOURCE_KEY  = f"monday_board_{BOARD_ID}"   # used in sync_state table


# ── GraphQL helper ────────────────────────────────────────────
import time

def run_query(query: str, max_retries: int = 3) -> dict:
    """Send a GraphQL query to Monday with retry on timeout."""
    last_error = None
    for attempt in range(max_retries):
        try:
            response = requests.post(
                API_URL,
                headers={"Authorization": API_KEY, "Content-Type": "application/json"},
                json={"query": query},
                timeout=60,        # bumped from 30 — Monday's slow on busy boards
            )
            response.raise_for_status()
            data = response.json()

            if "errors" in data:
                log.error("monday_api_error", errors=data["errors"])
                raise RuntimeError(f"Monday API returned errors: {data['errors']}")

            return data

        except (requests.exceptions.Timeout,
                requests.exceptions.ConnectionError,
                requests.exceptions.ReadTimeout) as e:
            last_error = e
            wait = 2 ** attempt   # 1s, 2s, 4s
            log.warning("monday_api_retry",
                        attempt=attempt + 1,
                        max=max_retries,
                        wait=wait,
                        error=str(e))
            time.sleep(wait)

    log.error("monday_api_request_failed", error=str(last_error))
    raise RuntimeError(f"Monday API request failed after {max_retries} retries: {last_error}") from last_error


# ── Column parser ─────────────────────────────────────────────
def parse_column_values(column_values: list) -> dict:
    """
    Extract fields by matching column TITLE (not type).
    Board: CRM - Buyers & Buyer Deals (18408828473)
    """
    result = {
        "first_name":           None,
        "last_name":            None,
        "email":                None,
        "phone":                None,
        "company":              None,
        "lead_score":           None,
        "status":               None,
        "sequence_status":      None,
        "sba":                  None,
        "cim_sent":             None,
        "is_broker":            False,
        "proof_of_funds":       False,
        "industry_tags":        None,
        "listing_number":       None,
        "notes_text":           None,
        "assigned_to_name":     None,
        "start_date":           None,
        "sequence_start_date":  None,
        "date_sent":            None,
    }

    for col in column_values:
        col_type  = col.get("type", "")
        col_text  = col.get("text") or ""
        col_value = col.get("value")
        title     = (col.get("column", {}).get("title") or "").lower().strip()

        parsed = {}
        if col_value:
            try:
                result_json = json.loads(col_value)
                if isinstance(result_json, dict):
                    parsed = result_json
            except (json.JSONDecodeError, TypeError):
                pass

        # ── Email ──────────────────────────────────────────────
        if col_type == "email" or "email" in title:
            result["email"] = parsed.get("email") or col_text or None

        # ── Phone ──────────────────────────────────────────────
        elif col_type == "phone" or "phone" in title:
            result["phone"] = parsed.get("phone") or col_text or None

        # ── First Name / Last Name ─────────────────────────────
        elif "first name" in title:
            result["first_name"] = col_text or None

        elif "last name" in title:
            result["last_name"] = col_text or None

        # ── Person / Assigned To ───────────────────────────────
        elif col_type == "people" or "assigned" in title:
            result["assigned_to_name"] = col_text or None

        # ── Business Name (dropdown) ───────────────────────────
        elif "business name" in title:
            result["company"] = col_text or None

        # ── Notes / Notas ──────────────────────────────────────
        elif ("notas" in title or "notes" in title) and col_type in ("text", "long_text", "tags"):
            result["notes_text"] = col_text or None

        # ── Industry or Notes (tags) ───────────────────────────
        elif "industry" in title:
            result["industry_tags"] = col_text or None

        # ── Listing # ──────────────────────────────────────────
        elif "listing" in title:
            result["listing_number"] = col_text or None

        # ── Checkboxes ─────────────────────────────────────────
        elif "broker" in title and col_type == "checkbox":
            result["is_broker"] = parsed.get("checked", False) if parsed else False

        elif "proof of funds" in title and col_type == "checkbox":
            result["proof_of_funds"] = parsed.get("checked", False) if parsed else False

        # ── Date columns — matched by title ────────────────────
        elif col_type == "date":
            date_str = parsed.get("date") or col_text
            date_val = None
            if date_str:
                try:
                    date_val = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
                except (ValueError, TypeError):
                    pass

            if "sequence start" in title:
                result["sequence_start_date"] = date_val
            elif "date sent" in title:
                result["date_sent"] = date_val
            elif "start date" in title:
                result["start_date"] = date_val

        # ── Status columns — matched by title ──────────────────
        elif col_type == "status":
            label = col_text or parsed.get("label") or None

            if "lead score" in title:
                result["lead_score"] = label
            elif "sequence status" in title:
                result["sequence_status"] = label
            elif "sba" in title:
                result["sba"] = label
            elif "cim sent" in title:
                result["cim_sent"] = label
            elif title == "status":
                result["status"] = label

    return result

# ── Fetch one page of items ───────────────────────────────────
def fetch_page(cursor: str | None) -> tuple[list, str | None]:
    """
    Fetch up to 100 items from the board.
    First call: uses items_page (no cursor).
    Next calls: uses next_items_page (with cursor).
    Returns (items_list, next_cursor).
    """
    if cursor is None:
        query = f"""
        {{
            boards(ids: [{BOARD_ID}]) {{
                items_page(limit: 100) {{
                    cursor
                    items {{
                        id name created_at updated_at
                        group {{ id title }}
                        column_values {{
                            id type text value
                            column {{ title }}
                        }}
                        updates(limit: 50) {{
                            id body text_body created_at
                            creator {{ id name email }}
                        }}
                    }}
                }}
            }}
        }}
        """
        data    = run_query(query)
        page    = data["data"]["boards"][0]["items_page"]
    else:
        query = f"""
        {{
            next_items_page(cursor: "{cursor}", limit: 100) {{
                cursor
                items {{
                    id name created_at updated_at
                    group {{ id title }}
                    column_values {{
                        id type text value
                        column {{ title }}
                    }}
                    updates(limit: 50) {{
                        id body text_body created_at
                        creator {{ id name email }}
                    }}
                }}
            }}
        }}
        """
        data    = run_query(query)
        page    = data["data"]["next_items_page"]

    return page["items"], page.get("cursor")


# ── Main sync function ────────────────────────────────────────
def sync_monday_leads():
    """
    Pull all leads from Monday board and upsert into Postgres.
    Saves cursor after each page so it can resume on failure.
    """
    conn        = get_connection()
    cursor      = None        # always do a full sync (fresh each run)
    total       = 0
    page_num    = 1

    log.info("monday_sync_started", board_id=BOARD_ID)

    try:
        while True:
            log.info("monday_page_fetching", page=page_num)
            items, next_cursor = fetch_page(cursor)

            if not items:
                break

            for item in items:
                try:
                    # Parse column values into flat fields
                    parsed = parse_column_values(item.get("column_values", []))

                    lead_data = {
                        "monday_item_id":   str(item["id"]),
                        "monday_board_id":  str(BOARD_ID),
                        "monday_group_id":  item.get("group", {}).get("id"),
                        "monday_group_name": item.get("group", {}).get("title"),
                        "name":             item.get("name"),
                        "monday_created_at": item.get("created_at"),
                        "monday_updated_at": item.get("updated_at"),
                        "raw_column_values": item.get("column_values", []),
                        **parsed,
                    }

                    upsert_lead(conn, lead_data)

                    # Get the internal lead ID to link notes
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT id FROM leads WHERE monday_item_id = %s",
                            (str(item["id"]),)
                        )
                        row = cur.fetchone()
                        if not row:
                            log.warning("lead_id_not_found", monday_item_id=item["id"])
                            continue
                        lead_id = row[0]

                    # Upsert notes
                    for update in item.get("updates", []):
                        upsert_lead_note(conn, {
                            "monday_update_id": str(update["id"]),
                            "lead_id":          lead_id,
                            "body_html":        update.get("body"),
                            "body_text":        update.get("text_body"),
                            "creator_name":     (update.get("creator") or {}).get("name"),
                            "creator_email":    (update.get("creator") or {}).get("email"),
                            "monday_created_at": update.get("created_at"),
                        })
                except Exception as e:
                    log.error("item_sync_failed", item_id=item.get("id"), error=str(e))
                    continue

            total    += len(items)
            conn.commit()
            save_sync_state(conn, SOURCE_KEY, next_cursor, len(items))
            conn.commit()

            log.info("monday_page_done", page=page_num, total_synced=total)

            if not next_cursor:
                break

            cursor    = next_cursor
            page_num += 1

    except Exception as e:
        conn.rollback()
        log.error("monday_sync_failed", page=page_num, error=str(e))
        raise
    finally:
        conn.close()

    log.info("monday_sync_complete", total_synced=total)




def embed_monday_notes():
    conn = get_connection()
    total_inline = 0
    total_comments = 0
    skipped = 0

    try:
        # ── Inline notes from leads.notes_text ─────────────────
        with conn.cursor() as cur:
            cur.execute("""
                SELECT l.id, l.name, l.notes_text, c.id AS contact_id
                FROM leads l
                JOIN contacts c ON c.monday_lead_id = l.id
                WHERE l.notes_text IS NOT NULL
                  AND TRIM(l.notes_text) != ''
            """)
            inline_rows = cur.fetchall()

        log.info("inline_notes_found", count=len(inline_rows))

        for lead_id, lead_name, notes_text, contact_id in inline_rows:
            try:
                content = f"Monday note for {lead_name}:\n\n{notes_text}".strip()
                if len(content) > 12000:
                    content = content[:12000]

                content_hash = hashlib.md5(content.encode("utf-8", errors="ignore")).hexdigest()
                source_ref_id = f"lead_inline_{lead_id}"

                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO contact_embeddings
                            (contact_id, source, source_ref_id, content_text, content_hash)
                        VALUES (%s, 'monday_inline', %s, %s, %s)
                        ON CONFLICT (content_hash) DO NOTHING
                    """, (contact_id, source_ref_id, content, content_hash))

                    if cur.rowcount > 0:
                        total_inline += 1
                    else:
                        skipped += 1
            except Exception as e:
                log.error("inline_note_failed", lead_id=lead_id, error=str(e))
                conn.rollback()
                continue

        conn.commit()

        # ── Lead comments from lead_notes ──────────────────────
        with conn.cursor() as cur:
            cur.execute("""
                SELECT n.id, n.body_text, n.creator_name, n.monday_created_at,
                       l.name AS lead_name, c.id AS contact_id
                FROM lead_notes n
                JOIN leads l    ON l.id = n.lead_id
                JOIN contacts c ON c.monday_lead_id = l.id
                WHERE n.body_text IS NOT NULL
                  AND TRIM(n.body_text) != ''
            """)
            comment_rows = cur.fetchall()

        log.info("comments_found", count=len(comment_rows))

        for note_id, body, creator, created_at, lead_name, contact_id in comment_rows:
            try:
                header = f"Monday comment on {lead_name}"
                if creator:
                    header += f" by {creator}"
                if created_at:
                    header += f" ({created_at.strftime('%Y-%m-%d')})"

                content = f"{header}:\n\n{body}".strip()
                if len(content) > 12000:
                    content = content[:12000]

                content_hash = hashlib.md5(content.encode("utf-8", errors="ignore")).hexdigest()
                source_ref_id = f"lead_note_{note_id}"

                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO contact_embeddings
                            (contact_id, source, source_ref_id, content_text, content_hash)
                        VALUES (%s, 'monday_comment', %s, %s, %s)
                        ON CONFLICT (content_hash) DO NOTHING
                    """, (contact_id, source_ref_id, content, content_hash))

                    if cur.rowcount > 0:
                        total_comments += 1
                    else:
                        skipped += 1
            except Exception as e:
                log.error("comment_failed", note_id=note_id, error=str(e))
                conn.rollback()
                continue

        conn.commit()

        log.info(
            "monday_notes_embed_done",
            inline_added=total_inline,
            comments_added=total_comments,
            skipped=skipped,
        )
        log.info("next_step", message="Run `python main.py embed` to generate embeddings.")

    finally:
        conn.close()

