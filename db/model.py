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
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        raise RuntimeError("DATABASE_URL environment variable is not set")
    try:
        return psycopg2.connect(
            db_url,
            options="-c statement_timeout=120000",
            keepalives=1,
            keepalives_idle=30,
            keepalives_interval=10,
            keepalives_count=5,
            connect_timeout=10,
        )
    except psycopg2.OperationalError as e:
        log.error("db_connection_failed", error=str(e))
        raise RuntimeError(f"Cannot connect to database: {e}") from e


# ── Write: leads ──────────────────────────────────────────────
def upsert_lead(conn, data: dict):
    sql = """
        INSERT INTO leads (
            monday_item_id, monday_board_id, monday_group_id, monday_group_name,
            name, first_name, last_name, email, phone, company,
            lead_score, status, sequence_status, sba, cim_sent,
            is_broker, proof_of_funds, industry_tags, listing_number,
            notes_text, assigned_to_name,
            start_date, sequence_start_date, date_sent,
            raw_column_values, monday_created_at, monday_updated_at, last_synced_at
        )
        VALUES (
            %(monday_item_id)s, %(monday_board_id)s, %(monday_group_id)s, %(monday_group_name)s,
            %(name)s, %(first_name)s, %(last_name)s, %(email)s, %(phone)s, %(company)s,
            %(lead_score)s, %(status)s, %(sequence_status)s, %(sba)s, %(cim_sent)s,
            %(is_broker)s, %(proof_of_funds)s, %(industry_tags)s, %(listing_number)s,
            %(notes_text)s, %(assigned_to_name)s,
            %(start_date)s, %(sequence_start_date)s, %(date_sent)s,
            %(raw_column_values)s, %(monday_created_at)s, %(monday_updated_at)s, NOW()
        )
        ON CONFLICT (monday_item_id) DO UPDATE SET
            monday_group_id         = EXCLUDED.monday_group_id,
            monday_group_name       = EXCLUDED.monday_group_name,
            name                    = EXCLUDED.name,
            first_name              = EXCLUDED.first_name,
            last_name               = EXCLUDED.last_name,
            email                   = EXCLUDED.email,
            phone                   = EXCLUDED.phone,
            company                 = EXCLUDED.company,
            lead_score              = EXCLUDED.lead_score,
            status                  = EXCLUDED.status,
            sequence_status         = EXCLUDED.sequence_status,
            sba                     = EXCLUDED.sba,
            cim_sent                = EXCLUDED.cim_sent,
            is_broker               = EXCLUDED.is_broker,
            proof_of_funds          = EXCLUDED.proof_of_funds,
            industry_tags           = EXCLUDED.industry_tags,
            listing_number          = EXCLUDED.listing_number,
            notes_text              = EXCLUDED.notes_text,
            assigned_to_name        = EXCLUDED.assigned_to_name,
            start_date              = EXCLUDED.start_date,
            sequence_start_date     = EXCLUDED.sequence_start_date,
            date_sent               = EXCLUDED.date_sent,
            raw_column_values       = EXCLUDED.raw_column_values,
            monday_updated_at       = EXCLUDED.monday_updated_at,
            last_synced_at          = NOW()
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
        SELECT id, name, email, phone, company, status,
               sequence_status, lead_score, assigned_to_name, monday_updated_at
        FROM leads
        WHERE LOWER(status) = LOWER(%s)
        ORDER BY monday_updated_at DESC
        LIMIT 50
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, (status,))
        return [dict(r) for r in cur.fetchall()]


def get_stale_leads(conn, days: int = 30):
    sql = """
        SELECT id, name, email, phone, company, status,
               sequence_status, lead_score, assigned_to_name, monday_updated_at
        FROM leads
        WHERE monday_updated_at < NOW() - INTERVAL '1 day' * %s
          AND status NOT ILIKE '%%closed%%'
          AND status NOT ILIKE '%%won%%'
          AND status NOT ILIKE '%%lost%%'
        ORDER BY monday_updated_at ASC
        LIMIT 50
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, (days,))
        return [dict(r) for r in cur.fetchall()]


def search_leads(conn, query: str):
    sql = """
        SELECT id, name, email, phone, company, status,
               sequence_status, lead_score, assigned_to_name, monday_updated_at
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
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT monday_group_name, COUNT(*) as count
            FROM leads
            GROUP BY monday_group_name
            ORDER BY count DESC
        """)
        by_group = [dict(r) for r in cur.fetchall()]

        cur.execute("""
            SELECT lead_score, COUNT(*) as count
            FROM leads
            WHERE lead_score IS NOT NULL
            GROUP BY lead_score
            ORDER BY count DESC
        """)
        by_score = [dict(r) for r in cur.fetchall()]

        cur.execute("""
            SELECT sequence_status, COUNT(*) as count
            FROM leads
            WHERE sequence_status IS NOT NULL
            GROUP BY sequence_status
            ORDER BY count DESC
        """)
        by_sequence = [dict(r) for r in cur.fetchall()]

    return {
        "by_pipeline_stage": by_group,
        "by_lead_score": by_score,
        "by_sequence_status": by_sequence,
    }


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
                        phone = CASE WHEN phone IS NULL
                                     AND NOT EXISTS (SELECT 1 FROM contacts c2 WHERE c2.phone = %s AND c2.id != contacts.id)
                                     THEN %s ELSE phone END,
                        company = COALESCE(company, %s),
                        in_monday = in_monday OR %s,
                        updated_at = NOW()
                    WHERE id = %s
                """, (lead_id, name, phone, phone, company, lead_id is not None, row["id"]))
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
                        email = CASE WHEN email IS NULL
                                     AND NOT EXISTS (SELECT 1 FROM contacts c2 WHERE c2.email = %s AND c2.id != contacts.id)
                                     THEN %s ELSE email END,
                        company = COALESCE(company, %s),
                        in_monday = in_monday OR %s,
                        updated_at = NOW()
                    WHERE id = %s
                """, (lead_id, name, email, email, company, lead_id is not None, row["id"]))
                return row["id"], "phone_match"

        # Level 3 — exact name match (case-insensitive)
        if name:
            cur.execute("SELECT id FROM contacts WHERE LOWER(name) = LOWER(%s)", (name,))
            row = cur.fetchone()
            if row:
                cur.execute("""
                    UPDATE contacts
                    SET monday_lead_id = COALESCE(monday_lead_id, %s),
                        email = CASE WHEN email IS NULL
                                     AND NOT EXISTS (SELECT 1 FROM contacts c2 WHERE c2.email = %s AND c2.id != contacts.id)
                                     THEN %s ELSE email END,
                        phone = CASE WHEN phone IS NULL
                                     AND NOT EXISTS (SELECT 1 FROM contacts c2 WHERE c2.phone = %s AND c2.id != contacts.id)
                                     THEN %s ELSE phone END,
                        company = COALESCE(company, %s),
                        in_monday = in_monday OR %s,
                        updated_at = NOW()
                    WHERE id = %s
                """, (lead_id, email, email, phone, phone, company, lead_id is not None, row["id"]))
                return row["id"], "name_match"

        # Level 4 — create new contact (skip if email/phone already taken)
        cur.execute("""
            INSERT INTO contacts (name, email, phone, company, monday_lead_id, in_monday, resolution_status)
            VALUES (
                %s,
                CASE WHEN NOT EXISTS (SELECT 1 FROM contacts WHERE email = %s) THEN %s ELSE NULL END,
                CASE WHEN NOT EXISTS (SELECT 1 FROM contacts WHERE phone = %s) THEN %s ELSE NULL END,
                %s, %s, TRUE, 'auto'
            )
            RETURNING id
        """, (name, email, email, phone, phone, company, lead_id))

        return cur.fetchone()["id"], "created"


def resolve_monday_contacts(conn) -> dict:
    """
    Read all leads and create/link contacts.
    Safe to re-run — matches by email/phone to avoid duplicates.
    """
    with conn.cursor() as cur:
        cur.execute("SET statement_timeout = '60s'")

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT id, name, email, phone, company FROM leads ORDER BY id ASC")
        leads = cur.fetchall()

    stats = {"created": 0, "email_match": 0, "phone_match": 0, "name_match": 0, "skipped": 0, "failed": 0}
    total = len(leads)

    for i, lead in enumerate(leads):
        if not lead["email"] and not lead["phone"] and not lead["name"]:
            stats["skipped"] += 1
            continue

        try:
            _, method = find_or_create_contact(
                conn,
                name    = lead["name"],
                email   = lead["email"],
                phone   = lead["phone"],
                company = lead["company"],
                lead_id = lead["id"],
            )
            stats[method] += 1
        except Exception as e:
            stats["failed"] += 1
            log.error("resolve_failed", lead_id=lead["id"], error=str(e))
            conn.rollback()
            continue

        if (i + 1) % 100 == 0:
            conn.commit()
            log.info("resolve_progress", done=i+1, total=total, stats=stats)

    conn.commit()
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

