import os
import re
import psycopg2
import psycopg2.extras
from typing import Optional
from dotenv import load_dotenv
from logger import get_logger

load_dotenv()

log = get_logger("db")


# ── Connection ────────────────────────────────────────────────
def get_connection():
    return psycopg2.connect(os.environ["DATABASE_URL"])


# ── Write: leads ──────────────────────────────────────────────
def upsert_lead(conn, data: dict):
    sql = """
        INSERT INTO leads (
            monday_item_id, monday_board_id, monday_group_id, monday_group_name,
            name, email, phone, company, location, website,
            client_status, spanish_speaking, position, value_level,
            mood, follow_up_status, sentiment,
            due_date, notes_text, assigned_to_name,
            raw_column_values, monday_created_at, monday_updated_at, last_synced_at
        )
        VALUES (
            %(monday_item_id)s, %(monday_board_id)s, %(monday_group_id)s, %(monday_group_name)s,
            %(name)s, %(email)s, %(phone)s, %(company)s, %(location)s, %(website)s,
            %(client_status)s, %(spanish_speaking)s, %(position)s, %(value_level)s,
            %(mood)s, %(follow_up_status)s, %(sentiment)s,
            %(due_date)s, %(notes_text)s, %(assigned_to_name)s,
            %(raw_column_values)s, %(monday_created_at)s, %(monday_updated_at)s, NOW()
        )
        ON CONFLICT (monday_item_id) DO UPDATE SET
            monday_group_id     = EXCLUDED.monday_group_id,
            monday_group_name   = EXCLUDED.monday_group_name,
            name                = EXCLUDED.name,
            email               = EXCLUDED.email,
            phone               = EXCLUDED.phone,
            company             = EXCLUDED.company,
            location            = EXCLUDED.location,
            website             = EXCLUDED.website,
            client_status       = EXCLUDED.client_status,
            spanish_speaking    = EXCLUDED.spanish_speaking,
            position            = EXCLUDED.position,
            value_level         = EXCLUDED.value_level,
            mood                = EXCLUDED.mood,
            follow_up_status    = EXCLUDED.follow_up_status,
            sentiment           = EXCLUDED.sentiment,
            due_date            = EXCLUDED.due_date,
            notes_text          = EXCLUDED.notes_text,
            assigned_to_name    = EXCLUDED.assigned_to_name,
            raw_column_values   = EXCLUDED.raw_column_values,
            monday_updated_at   = EXCLUDED.monday_updated_at,
            last_synced_at      = NOW()
    """
    with conn.cursor() as cur:
        cur.execute(sql, {**data, "raw_column_values": psycopg2.extras.Json(data.get("raw_column_values"))})


# ── Write: lead notes ─────────────────────────────────────────
def upsert_lead_note(conn, data: dict):
    sql = """
        INSERT INTO lead_notes (
            monday_update_id, lead_id, body_html, body_text,
            creator_name, creator_email, monday_created_at
        )
        VALUES (
            %(monday_update_id)s, %(lead_id)s, %(body_html)s, %(body_text)s,
            %(creator_name)s, %(creator_email)s, %(monday_created_at)s
        )
        ON CONFLICT (monday_update_id) DO NOTHING
    """
    with conn.cursor() as cur:
        cur.execute(sql, data)


# ── Write: sync state ─────────────────────────────────────────
def get_sync_cursor(conn, source: str) -> Optional[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT cursor FROM sync_state WHERE source = %s", (source,))
        row = cur.fetchone()
        return row[0] if row else None


def save_sync_state(conn, source: str, cursor: Optional[str], items_added: int = 0):
    sql = """
        INSERT INTO sync_state (source, cursor, last_synced_at, total_items_synced)
        VALUES (%s, %s, NOW(), %s)
        ON CONFLICT (source) DO UPDATE SET
            cursor              = EXCLUDED.cursor,
            last_synced_at      = NOW(),
            total_items_synced  = sync_state.total_items_synced + EXCLUDED.total_items_synced
    """
    with conn.cursor() as cur:
        cur.execute(sql, (source, cursor, items_added))


# ── Read: agent query functions ───────────────────────────────
def get_leads_by_status(conn, status: str):
    sql = """
        SELECT id, name, email, phone, company, client_status,
               follow_up_status, assigned_to_name, monday_updated_at
        FROM leads
        WHERE LOWER(client_status) = LOWER(%s)
        ORDER BY monday_updated_at DESC
        LIMIT 50
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, (status,))
        return [dict(r) for r in cur.fetchall()]


def get_stale_leads(conn, days: int = 30):
    sql = """
        SELECT id, name, email, phone, company, client_status,
               follow_up_status, assigned_to_name, monday_updated_at
        FROM leads
        WHERE monday_updated_at < NOW() - INTERVAL '1 day' * %s
          AND client_status NOT ILIKE '%%closed%%'
          AND client_status NOT ILIKE '%%won%%'
          AND client_status NOT ILIKE '%%lost%%'
        ORDER BY monday_updated_at ASC
        LIMIT 50
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, (days,))
        return [dict(r) for r in cur.fetchall()]


def search_leads(conn, query: str):
    sql = """
        SELECT id, name, email, phone, company, client_status,
               follow_up_status, assigned_to_name, monday_updated_at
        FROM leads
        WHERE name    ILIKE %s
           OR email   ILIKE %s
           OR company ILIKE %s
        ORDER BY monday_updated_at DESC
        LIMIT 20
    """
    pattern = f"%{query}%"
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, (pattern, pattern, pattern))
        return [dict(r) for r in cur.fetchall()]


def get_lead_with_notes(conn, lead_id: int) -> Optional[dict]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM leads WHERE id = %s", (lead_id,))
        lead = cur.fetchone()
        if not lead:
            return None
        lead = dict(lead)
        cur.execute(
            "SELECT body_text, creator_name, monday_created_at FROM lead_notes WHERE lead_id = %s ORDER BY monday_created_at ASC",
            (lead_id,)
        )
        lead["notes"] = [dict(r) for r in cur.fetchall()]
        return lead


def get_summary_stats(conn) -> dict:
    sql = """
        SELECT client_status, COUNT(*) as count
        FROM leads
        GROUP BY client_status
        ORDER BY count DESC
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql)
        return {"by_status": [dict(r) for r in cur.fetchall()]}


# ── Contact resolution ────────────────────────────────────────

def normalize_phone(phone: str) -> Optional[str]:
    """Strip everything except digits. +1 (647) 123-4567 → 16471234567"""
    if not phone:
        return None
    normalized = re.sub(r"\D", "", phone)
    return normalized if normalized else None


def find_or_create_contact(conn, name, email, phone, company, lead_id) -> tuple:
    """
    Try to find an existing contact by email, phone, or name.
    If found → link to this lead and fill in missing fields.
    If not found → create a new contact.
    Returns (contact_id, method).
    """
    email = email.lower().strip() if email else None
    phone = normalize_phone(phone)

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:

        # Level 1 — exact email match
        if email:
            cur.execute("SELECT id FROM contacts WHERE email = %s", (email,))
            row = cur.fetchone()
            if row:
                cur.execute("""
                    UPDATE contacts
                    SET monday_lead_id = COALESCE(monday_lead_id, %s),
                        name = COALESCE(name, %s),
                        phone = COALESCE(phone, %s),
                        company = COALESCE(company, %s),
                        in_monday = in_monday OR %s,
                        updated_at = NOW()
                    WHERE id = %s
                """, (lead_id, name, phone, company, lead_id is not None, row["id"]))
                return row["id"], "email_match"

        # Level 2 — exact phone match
        if phone:
            cur.execute("SELECT id FROM contacts WHERE phone = %s", (phone,))
            row = cur.fetchone()
            if row:
                cur.execute("""
                    UPDATE contacts
                    SET monday_lead_id = COALESCE(monday_lead_id, %s),
                        name = COALESCE(name, %s),
                        email = COALESCE(email, %s),
                        company = COALESCE(company, %s),
                        in_monday = in_monday OR %s,
                        updated_at = NOW()
                    WHERE id = %s
                """, (lead_id, name, email, company, lead_id is not None, row["id"]))
                return row["id"], "phone_match"

        # Level 3 — exact name match (case-insensitive)
        if name:
            cur.execute("SELECT id FROM contacts WHERE LOWER(name) = LOWER(%s)", (name,))
            row = cur.fetchone()
            if row:
                cur.execute("""
                    UPDATE contacts
                    SET monday_lead_id = COALESCE(monday_lead_id, %s),
                        email = COALESCE(email, %s),
                        phone = COALESCE(phone, %s),
                        company = COALESCE(company, %s),
                        in_monday = in_monday OR %s,
                        updated_at = NOW()
                    WHERE id = %s
                """, (lead_id, email, phone, company, lead_id is not None, row["id"]))
                return row["id"], "name_match"

        # Level 4 — create new contact
        cur.execute("""
            INSERT INTO contacts (name, email, phone, company, monday_lead_id, in_monday, resolution_status)
            VALUES (%s, %s, %s, %s, %s, TRUE, 'auto')
            RETURNING id
        """, (name, email, phone, company, lead_id))

        return cur.fetchone()["id"], "created"


def resolve_monday_contacts(conn) -> dict:
    """
    Read all leads and create/link contacts.
    Safe to re-run — matches by email/phone to avoid duplicates.
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT id, name, email, phone, company FROM leads ORDER BY id ASC")
        leads = cur.fetchall()

    stats = {"created": 0, "email_match": 0, "phone_match": 0, "name_match": 0, "skipped": 0}

    for lead in leads:
        if not lead["email"] and not lead["phone"] and not lead["name"]:
            stats["skipped"] += 1
            continue

        _, method = find_or_create_contact(
            conn,
            name    = lead["name"],
            email   = lead["email"],
            phone   = lead["phone"],
            company = lead["company"],
            lead_id = lead["id"],
        )
        stats[method] += 1

    return stats



def semantic_search(conn, query_embedding: list, contact_id: int = None, source: str = None, limit: int = 10):

    log.info("semantic_search", contact_id=contact_id, source=source, limit=limit)

    conditions = []
    params = []
    if contact_id:
        conditions.append("ce.contact_id = %s")
        params.append(contact_id)

    if source:
        conditions.append("ce.source = %s")
        params.append(source)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    sql = f"""
        SELECT ce.content_text, ce.source, c.name AS contact_name,
                ce.embedding <=> %s::vector AS distance
        FROM contact_embeddings ce
        JOIN contacts c ON c.id = ce.contact_id
        {where}

        ORDER BY ce.embedding <=> %s::vector ASC
        LIMIT %s

    """

    params = [str(query_embedding)] + params + [str(query_embedding), limit]

    with conn.cursor(cursor_factory = psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        results = [dict(r) for r in cur.fetchall()]

    log.info("semantic_search_results",
             count=len(results),
             sources=[r["source"] for r in results],
             distances=[round(r["distance"], 3) for r in results])

    return results


def merge_duplicate_contacts(conn) -> dict:
    """
    Find contacts that share the same name (case-insensitive) and merge them.
    The contact with the most data (email/phone filled in) becomes the primary.
    All embeddings and flags are moved to the primary. Duplicates are deleted.
    """
    stats = {"groups_found": 0, "contacts_merged": 0}

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        # Find duplicate name groups
        cur.execute("""
            SELECT LOWER(name) AS lname, COUNT(*) AS cnt
            FROM contacts
            WHERE name IS NOT NULL AND name != ''
            GROUP BY LOWER(name)
            HAVING COUNT(*) > 1
            ORDER BY cnt DESC
        """)
        dup_groups = cur.fetchall()

    for group in dup_groups:
        stats["groups_found"] += 1

        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, name, email, phone, company, monday_lead_id,
                       in_monday, in_gmail, in_whatsapp
                FROM contacts
                WHERE LOWER(name) = %s
                ORDER BY id ASC
            """, (group["lname"],))
            dupes = cur.fetchall()

        if len(dupes) < 2:
            continue

        # Pick primary — the one with the most fields filled in
        def score(c):
            return sum([
                bool(c["email"]),
                bool(c["phone"]),
                bool(c["company"]),
                bool(c["monday_lead_id"]),
            ])

        dupes_sorted = sorted(dupes, key=score, reverse=True)
        primary = dupes_sorted[0]
        others = dupes_sorted[1:]

        with conn.cursor() as cur:
            for other in others:
                # Move all embeddings to the primary contact
                cur.execute(
                    "UPDATE contact_embeddings SET contact_id = %s WHERE contact_id = %s",
                    (primary["id"], other["id"])
                )

                # Delete the duplicate first (before updating primary to avoid unique conflicts)
                cur.execute("DELETE FROM contacts WHERE id = %s", (other["id"],))

                # Merge fields — fill in anything primary is missing
                cur.execute("""
                    UPDATE contacts SET
                        email = COALESCE(email, %s),
                        phone = COALESCE(phone, %s),
                        company = COALESCE(company, %s),
                        monday_lead_id = COALESCE(monday_lead_id, %s),
                        in_monday = in_monday OR %s,
                        in_gmail = in_gmail OR %s,
                        in_whatsapp = in_whatsapp OR %s,
                        updated_at = NOW()
                    WHERE id = %s
                """, (
                    other["email"], other["phone"], other["company"],
                    other["monday_lead_id"],
                    other["in_monday"], other["in_gmail"], other["in_whatsapp"],
                    primary["id"],
                ))

                stats["contacts_merged"] += 1

        conn.commit()
        print(f"  Merged: {primary['name']} (kept id={primary['id']}, removed {len(others)} duplicates)")

    return stats

