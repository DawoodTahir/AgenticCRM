import os
import json
import requests
from datetime import datetime
from dotenv import load_dotenv
from db.model import (
    get_connection, upsert_lead, upsert_lead_note,
    get_sync_cursor, save_sync_state
)

load_dotenv()

API_URL     = "https://api.monday.com/v2"
API_KEY     = os.environ["MONDAY_API_KEY"]
BOARD_ID    = os.environ["MONDAY_BOARD_ID"]
SOURCE_KEY  = f"monday_board_{BOARD_ID}"   # used in sync_state table


# ── GraphQL helper ────────────────────────────────────────────
def run_query(query: str) -> dict:
    """Send a GraphQL query to Monday and return the JSON response."""
    response = requests.post(
        API_URL,
        headers={"Authorization": API_KEY, "Content-Type": "application/json"},
        json={"query": query},
        timeout=30
    )
    response.raise_for_status()
    return response.json()


# ── Column parser ─────────────────────────────────────────────
def parse_column_values(column_values: list) -> dict:
    """
    Extract fields by matching column TITLE (not type).
    Your board has 5 status columns so we must use titles to tell them apart.
    Titles are matched case-insensitively and support Spanish/English names.
    """
    result = {
        "email":            None,
        "phone":            None,
        "company":          None,
        "location":         None,
        "website":          None,
        "client_status":    None,
        "spanish_speaking": None,
        "position":         None,
        "value_level":      None,
        "mood":             None,
        "follow_up_status": None,
        "sentiment":        None,
        "due_date":         None,
        "notes_text":       None,
        "assigned_to_name": None,
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
                # Only use as dict if it actually parsed to a dict
                if isinstance(result_json, dict):
                    parsed = result_json
            except (json.JSONDecodeError, TypeError):
                pass

        # ── Email ──────────────────────────────────────────────
        if col_type == "email" or "correo" in title or "email" in title:
            result["email"] = parsed.get("email") or col_text or None

        # ── Phone ──────────────────────────────────────────────
        elif col_type == "phone" or "teléfono" in title or "telefono" in title or "phone" in title:
            result["phone"] = parsed.get("phone") or col_text or None

        # ── Person / Assigned To ───────────────────────────────
        elif col_type == "people" or "assigned" in title or "responsable" in title:
            result["assigned_to_name"] = col_text or None

        # ── Location ───────────────────────────────────────────
        elif col_type == "location" or "city" in title or "country" in title or "ciudad" in title:
            result["location"] = col_text or None

        # ── Website ────────────────────────────────────────────
        elif col_type == "link" or "website" in title or "web" in title or "url" in title:
            result["website"] = parsed.get("url") or col_text or None

        # ── Notes (inline text column, not updates) ────────────
        elif ("notes" in title or "nota" in title) and col_type in ("text", "long_text"):
            result["notes_text"] = col_text or None

        # ── Due Date ───────────────────────────────────────────
        elif col_type == "date" or "due" in title or "fecha" in title or "date" in title:
            date_str = parsed.get("date") or col_text
            if date_str:
                try:
                    result["due_date"] = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
                except (ValueError, TypeError):
                    pass

        # ── Status columns — matched by title ──────────────────
        elif col_type == "status":
            label = col_text or parsed.get("label") or None

            if "client status" in title or "estado" in title:
                result["client_status"] = label

            elif "spanish" in title or "español" in title or "habla" in title:
                result["spanish_speaking"] = label

            elif "position" in title or "posición" in title or "posicion" in title or "rol" in title:
                result["position"] = label

            elif "value" in title or "valor" in title:
                result["value_level"] = label

            elif "mood" in title or "humor" in title or "feeling" in title:
                result["mood"] = label

            elif "status" in title or "estado seguimiento" in title:
                result["follow_up_status"] = label

            elif "sentiment" in title:
                result["sentiment"] = label

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

    print(f"Starting Monday sync for board {BOARD_ID}...")

    try:
        while True:
            print(f"  Fetching page {page_num}...")
            items, next_cursor = fetch_page(cursor)

            if not items:
                break

            for item in items:
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
                    lead_id = cur.fetchone()[0]

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

            total    += len(items)
            conn.commit()
            save_sync_state(conn, SOURCE_KEY, next_cursor, len(items))
            conn.commit()

            print(f"  Page {page_num} done — {total} items synced so far")

            if not next_cursor:
                break

            cursor    = next_cursor
            page_num += 1

    except Exception as e:
        conn.rollback()
        print(f"Sync failed on page {page_num}: {e}")
        raise
    finally:
        conn.close()

    print(f"Sync complete. Total items synced: {total}")
