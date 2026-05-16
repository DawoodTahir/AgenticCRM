import re
import json
import psycopg2.extras
from langchain_core.tools import tool
from connectors.embeddings import generate_embedding
from db.model import (
    get_connection,
    get_leads_by_status,
    get_stale_leads,
    semantic_search,
    get_lead_with_notes,
    get_summary_stats,
)
from logger import get_logger

log = get_logger("tools")


def _serialize(data) -> str:
    """Convert query results to a clean JSON string for the agent."""
    return json.dumps(data, default=str, indent=2)


SOURCE_LABELS = {
    "gmail":              "Gmail",
    "tw_outlook_inbox":   "Outlook Inbox",
    "tw_outlook_sent":    "Outlook Sent",
    "whatsapp":           "WhatsApp",
    "monday_inline":      "Monday note",
    "monday_comment":     "Monday comment",
    "readai":             "Read.ai meeting",
    "outlook_inbox":      "Outlook Inbox",
    "outlook_sent":       "Outlook Sent",
}


def _label(source: str) -> str:
    return SOURCE_LABELS.get(source, source or "unknown")


def _source_bucket(source: str) -> str:
    """Group a source into a high-level bucket the agent can reason about."""
    if source in ("gmail", "tw_outlook_inbox", "tw_outlook_sent",
                  "outlook_inbox", "outlook_sent"):
        return "emails"
    if source in ("monday_inline", "monday_comment"):
        return "monday_notes"
    if source == "whatsapp":
        return "whatsapp"
    if source == "readai":
        return "meetings"
    return "other"


def _fetch_contact_content(conn, contact_id: int, per_bucket: int = 8, snippet_chars: int = 800) -> dict:
    """
    Pull recent content for one contact, grouped by bucket
    (emails / meetings / whatsapp / monday_notes).
    Each item has a longer snippet so the agent can summarize bodies, not just metadata.
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT id, source, source_ref_id, content_text, created_at
            FROM contact_embeddings
            WHERE contact_id = %s
            ORDER BY created_at DESC NULLS LAST, id DESC
            LIMIT 200
        """, (contact_id,))
        rows = cur.fetchall()

    buckets = {"emails": [], "meetings": [], "whatsapp": [], "monday_notes": [], "other": []}
    for r in rows:
        bucket = _source_bucket(r["source"])
        if len(buckets[bucket]) >= per_bucket:
            continue
        snippet = (r["content_text"] or "")[:snippet_chars]
        buckets[bucket].append({
            "source": _label(r["source"]),
            "source_ref_id": r.get("source_ref_id"),
            "date": r.get("created_at"),
            "snippet": snippet,
        })
    return buckets


@tool
def tool_lookup_person(name: str = None, email: str = None, phone: str = None) -> str:
    """
    Look up EVERYTHING about a person across ALL sources — Monday leads,
    WhatsApp messages, Gmail emails — in one call.
    Returns: contact info, Monday lead details + notes, and recent conversations.

    ALWAYS call this tool with whatever name the user gives, even partial names.
    The tool has fuzzy matching built in — it will suggest close matches if
    the spelling is wrong or the name is partial. Only ask the user for more
    info after seeing the tool's response.
    Provide at least one of: name, email, or phone.
    """
    log.info("lookup_person_called", name=name, email=email, phone=phone)
    conn = get_connection()
    try:
        # --- Step 1: Find the contact ---
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if email:
                log.info("search_by", method="email", value=email)
                cur.execute("SELECT * FROM contacts WHERE email = %s", (email.lower().strip(),))
                rows = [cur.fetchone()] if cur.rowcount else []
            elif phone:
                clean_phone = re.sub(r"\D", "", phone)
                cur.execute("SELECT * FROM contacts WHERE phone = %s", (clean_phone,))
                rows = [cur.fetchone()] if cur.rowcount else []
            elif name:
                log.info("search_by", method="name", value=name)
                cur.execute(
                    "SELECT * FROM contacts WHERE name ILIKE %s LIMIT 5",
                    (f"%{name}%",)
                )
                rows = [dict(r) for r in cur.fetchall()]
                log.info("exact_match_results", count=len(rows))

                if not rows:
                    log.info("fuzzy_search_triggered", name=name)
                    cur.execute("""
                    SELECT *, similarity(name, %s) AS sim
                    FROM contacts
                    WHERE name is not NULL
                    AND similarity(name, %s) > 0.2
                    ORDER BY similarity(name, %s) DESC
                    LIMIT 5""", (name, name, name))

                    fuzzy = [dict(r) for r in cur.fetchall()]
                    log.info("fuzzy_match_results", count=len(fuzzy))

                    if fuzzy:
                        suggestions = "\n".join(
                            f"- {r['name']} ({r.get('email') or r.get('phone') or 'no email/phone'})"
                            for r in fuzzy
                        )
                        return f"No exact match for '{name}'. Did you mean:\n{suggestions}\nAsk the user to confirm."

                    return f"No contact found matching '{name}'."

                
            else:
                return "Please provide a name, email, or phone number."

        if not rows or rows[0] is None:
            return "No contact found matching the given info."

        if len(rows) > 1:
            matches = "\n".join(
                f"- {r['name']} ({r.get('email') or r.get('phone') or 'no email/phone'})"
                for r in rows
            )
            return f"Multiple contacts found:\n{matches}\nAsk the user to specify which one — by full name, email, or phone."

        contact = dict(rows[0])
        result = {}

        sources = []
        if contact.get("in_monday"): sources.append("Monday")
        if contact.get("in_gmail"): sources.append("Gmail")
        if contact.get("in_whatsapp"): sources.append("WhatsApp")

        result["contact"] = {
            "name": contact.get("name"),
            "email": contact.get("email"),
            "phone": contact.get("phone"),
            "company": contact.get("company"),
            "sources": ", ".join(sources) if sources else "Unknown",
        }

        # --- Step 3: Monday lead details + notes (if linked) ---
        if contact.get("monday_lead_id"):
            lead = get_lead_with_notes(conn, contact["monday_lead_id"])
            if lead:
                result["monday_lead"] = {
                    "pipeline_stage": lead.get("monday_group_name"),
                    "status": lead.get("status"),
                    "lead_score": lead.get("lead_score"),
                    "sequence_status": lead.get("sequence_status"),
                    "sba": lead.get("sba"),
                    "cim_sent": lead.get("cim_sent"),
                    "is_broker": lead.get("is_broker"),
                    "proof_of_funds": lead.get("proof_of_funds"),
                    "industry_tags": lead.get("industry_tags"),
                    "listing_number": lead.get("listing_number"),
                    "assigned_to": lead.get("assigned_to_name"),
                    "start_date": lead.get("start_date"),
                    "sequence_start_date": lead.get("sequence_start_date"),
                    "notes": lead.get("notes", []),
                }

        # --- Step 4: Recent content grouped by source bucket ---
        buckets = _fetch_contact_content(conn, contact["id"], per_bucket=4, snippet_chars=600)
        if buckets["emails"]:        result["recent_emails"]       = buckets["emails"]
        if buckets["meetings"]:      result["recent_meetings"]     = buckets["meetings"]
        if buckets["whatsapp"]:      result["recent_whatsapp"]     = buckets["whatsapp"]
        if buckets["monday_notes"]:  result["recent_monday_notes"] = buckets["monday_notes"]

        return _serialize(result)

    finally:
        conn.close()


@tool
def tool_person_profile(name: str = None, email: str = None) -> str:
    """
    Build a DEEP profile for a person — use this for questions like:
      - "tell me about X"
      - "give me a paragraph about X"
      - "summarize what we know about X"
      - "what is X interested in"
      - "what's the latest on X"

    Returns: contact info, Monday lead/status + notes, and the last 8 items per source
    (emails, Read.ai meetings, WhatsApp, Monday notes) with content snippets long enough
    to summarize the body — not just metadata.

    Use tool_lookup_person for short factual queries (email address, phone, status).
    Use tool_person_profile when the user wants a synthesis.
    """
    log.info("person_profile_called", name=name, email=email)
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if email:
                cur.execute("SELECT * FROM contacts WHERE email = %s", (email.lower().strip(),))
                rows = [cur.fetchone()] if cur.rowcount else []
            elif name:
                cur.execute("SELECT * FROM contacts WHERE name ILIKE %s LIMIT 5", (f"%{name}%",))
                rows = [dict(r) for r in cur.fetchall()]
                if not rows:
                    cur.execute("""
                        SELECT *, similarity(name, %s) AS sim
                        FROM contacts
                        WHERE name IS NOT NULL AND similarity(name, %s) > 0.2
                        ORDER BY similarity(name, %s) DESC
                        LIMIT 5
                    """, (name, name, name))
                    fuzzy = [dict(r) for r in cur.fetchall()]
                    if fuzzy:
                        suggestions = "\n".join(
                            f"- {r['name']} ({r.get('email') or r.get('phone') or 'no contact info'})"
                            for r in fuzzy
                        )
                        return f"No exact match for '{name}'. Did you mean:\n{suggestions}\nAsk the user to confirm."
                    return f"No contact found matching '{name}'."
            else:
                return "Provide a name or email."

        if not rows or rows[0] is None:
            return "No contact found."

        if len(rows) > 1:
            matches = "\n".join(
                f"- {r['name']} ({r.get('email') or r.get('phone') or 'no contact info'})"
                for r in rows
            )
            return f"Multiple contacts found:\n{matches}\nAsk the user to specify."

        contact = dict(rows[0])
        sources = []
        if contact.get("in_monday"):   sources.append("Monday")
        if contact.get("in_gmail"):    sources.append("Gmail")
        if contact.get("in_whatsapp"): sources.append("WhatsApp")

        profile = {
            "contact": {
                "name": contact.get("name"),
                "email": contact.get("email"),
                "phone": contact.get("phone"),
                "company": contact.get("company"),
                "known_in": ", ".join(sources) or "Unknown",
            }
        }

        # Monday lead + notes
        if contact.get("monday_lead_id"):
            lead = get_lead_with_notes(conn, contact["monday_lead_id"])
            if lead:
                profile["monday_lead"] = {
                    "pipeline_stage":      lead.get("monday_group_name"),
                    "status":              lead.get("status"),
                    "lead_score":          lead.get("lead_score"),
                    "sequence_status":     lead.get("sequence_status"),
                    "sba":                 lead.get("sba"),
                    "cim_sent":            lead.get("cim_sent"),
                    "is_broker":           lead.get("is_broker"),
                    "proof_of_funds":      lead.get("proof_of_funds"),
                    "industry_tags":       lead.get("industry_tags"),
                    "listing_number":      lead.get("listing_number"),
                    "assigned_to":         lead.get("assigned_to_name"),
                    "start_date":          lead.get("start_date"),
                    "sequence_start_date": lead.get("sequence_start_date"),
                    "inline_notes":        lead.get("notes_text"),
                    "comments":            lead.get("notes", []),
                }

        # Deep per-source content — last 8 per bucket, 1200-char snippets
        buckets = _fetch_contact_content(conn, contact["id"], per_bucket=8, snippet_chars=1200)
        if buckets["emails"]:        profile["recent_emails"]       = buckets["emails"]
        if buckets["meetings"]:      profile["recent_meetings"]     = buckets["meetings"]
        if buckets["whatsapp"]:      profile["recent_whatsapp"]     = buckets["whatsapp"]
        if buckets["monday_notes"]:  profile["recent_monday_notes"] = buckets["monday_notes"]

        return _serialize(profile)

    finally:
        conn.close()


@tool
def tool_list_recent_meetings(days: int = 30, limit: int = 10) -> str:
    """
    List recent Read.ai meeting summaries chronologically (most recent first),
    WITHOUT requiring a specific person name.

    Use this when the user asks about meetings/calls in general, not a specific person:
      - "show me my last N meetings"
      - "what meetings did we have last week"
      - "summarize my recent Read.ai sessions"
      - "what calls happened recently"

    days: how far back to look (default 30)
    limit: how many meetings to return (default 10)

    Returns one entry per meeting (deduped) with date, participants, and summary snippet.
    """
    from datetime import datetime, timedelta
    log.info("list_recent_meetings_called", days=days, limit=limit)
    cutoff = datetime.now() - timedelta(days=days)
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT
                    ce.source_ref_id,
                    MAX(ce.created_at) AS meeting_at,
                    (array_agg(ce.content_text ORDER BY ce.id))[1] AS content_text,
                    STRING_AGG(DISTINCT c.name, ', ') AS participants
                FROM contact_embeddings ce
                LEFT JOIN contacts c ON c.id = ce.contact_id
                WHERE ce.source = 'readai'
                  AND ce.created_at >= %s
                GROUP BY ce.source_ref_id
                ORDER BY MAX(ce.created_at) DESC
                LIMIT %s
            """, (cutoff, limit))
            rows = cur.fetchall()

        if not rows:
            return f"No Read.ai meetings found in the last {days} days."

        meetings = [{
            "date":         r["meeting_at"],
            "participants": r["participants"],
            "summary":      (r["content_text"] or "")[:1500],
        } for r in rows]
        return _serialize(meetings)
    finally:
        conn.close()


@tool
def tool_list_conversations_since(days: int = 7, max_per_source: int = 8) -> str:
    """
    List conversations (emails, meetings, WhatsApp, Monday comments) from the
    last N days WITHOUT requiring a specific person name.

    Use for date-relative questions about activity in general:
      - "what conversations did we have last week"     → days=7
      - "show me activity this month"                  → days=30
      - "what happened in the last 14 days"            → days=14
      - "any new emails today"                         → days=1

    Returns content grouped by source (meetings / emails / whatsapp / monday_notes).
    Each item has date, contact name, and a snippet.
    """
    from datetime import datetime, timedelta
    log.info("list_conversations_since_called", days=days, max_per_source=max_per_source)
    cutoff = datetime.now() - timedelta(days=days)
    conn = get_connection()
    try:
        result = {"period": f"Last {days} days (since {cutoff.date().isoformat()})"}

        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT MAX(ce.created_at) AS date,
                       (array_agg(ce.content_text ORDER BY ce.id))[1] AS content_text,
                       STRING_AGG(DISTINCT c.name, ', ') AS participants
                FROM contact_embeddings ce
                LEFT JOIN contacts c ON c.id = ce.contact_id
                WHERE ce.source = 'readai' AND ce.created_at >= %s
                GROUP BY ce.source_ref_id
                ORDER BY MAX(ce.created_at) DESC
                LIMIT %s
            """, (cutoff, max_per_source))
            meetings = [{
                "date": r["date"],
                "participants": r["participants"],
                "snippet": (r["content_text"] or "")[:800],
            } for r in cur.fetchall()]
        if meetings: result["meetings"] = meetings

        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT ce.created_at AS date, ce.source,
                       ce.content_text, c.name AS contact_name
                FROM contact_embeddings ce
                LEFT JOIN contacts c ON c.id = ce.contact_id
                WHERE ce.source IN ('gmail', 'tw_outlook_inbox', 'tw_outlook_sent',
                                    'outlook_inbox', 'outlook_sent')
                  AND ce.created_at >= %s
                ORDER BY ce.created_at DESC
                LIMIT %s
            """, (cutoff, max_per_source))
            emails = [{
                "date": r["date"],
                "source": _label(r["source"]),
                "contact": r["contact_name"],
                "snippet": (r["content_text"] or "")[:500],
            } for r in cur.fetchall()]
        if emails: result["emails"] = emails

        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT ce.created_at AS date, ce.source,
                       ce.content_text, c.name AS contact_name
                FROM contact_embeddings ce
                LEFT JOIN contacts c ON c.id = ce.contact_id
                WHERE ce.source IN ('whatsapp', 'monday_inline', 'monday_comment')
                  AND ce.created_at >= %s
                ORDER BY ce.created_at DESC
                LIMIT %s
            """, (cutoff, max_per_source))
            other = [{
                "date": r["date"],
                "source": _label(r["source"]),
                "contact": r["contact_name"],
                "snippet": (r["content_text"] or "")[:500],
            } for r in cur.fetchall()]
        if other: result["whatsapp_and_monday"] = other

        if len(result) == 1:
            return f"No conversations found in the last {days} days."
        return _serialize(result)
    finally:
        conn.close()


@tool
def tool_get_leads_by_status(status: str) -> str:
    """
    Get all leads with a specific status.
    Status is a column on the Monday board — possible values depend on what the team sets.
    Use this to see all leads at a particular status.
    """
    conn = get_connection()
    try:
        result = get_leads_by_status(conn, status)
        return _serialize(result) if result else f"No leads found with status '{status}'."
    finally:
        conn.close()


@tool
def tool_get_stale_leads(days: int = 30) -> str:
    """
    Get leads that have not been updated in N days.
    These are leads going cold — good re-engagement targets.
    Default is 30 days. Use a smaller number (e.g. 14) for more urgency.
    """
    conn = get_connection()
    try:
        result = get_stale_leads(conn, days)
        return _serialize(result) if result else f"No stale leads found beyond {days} days."
    finally:
        conn.close()


@tool
def tool_get_lead_details(lead_id: int) -> str:
    """
    Get full details of a specific lead including all their notes and comments.
    Use this after finding a lead's ID to dig deeper into their history.
    """
    conn = get_connection()
    try:
        result = get_lead_with_notes(conn, lead_id)
        return _serialize(result) if result else f"No lead found with ID {lead_id}."
    finally:
        conn.close()


@tool
def tool_get_pipeline_summary() -> str:
    """
    Get a high-level summary of the entire pipeline — lead counts grouped by status.
    Use this for questions about overall business health or pipeline overview.
    """
    conn = get_connection()
    try:
        result = get_summary_stats(conn)
        return _serialize(result)
    finally:
        conn.close()


@tool
def tool_get_leads_by_sequence_status(status: str) -> str:
    """
    Filter leads by their sequence status (outreach progress).
    Use this to find leads at a specific stage of the email/outreach sequence.
    """
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, name, email, company, status,
                       sequence_status, lead_score, assigned_to_name
                FROM leads
                WHERE LOWER(sequence_status) = LOWER(%s)
                ORDER BY monday_updated_at DESC
                LIMIT 50
                """,
                (status,)
            )
            rows = [dict(r) for r in cur.fetchall()]
        return _serialize(rows) if rows else f"No leads with sequence status '{status}'."
    finally:
        conn.close()


@tool
def tool_get_leads_by_lead_score(score: str) -> str:
    """
    Filter leads by their lead score.
    Use this to find high-priority or low-priority leads based on their score.
    """
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, name, email, company, status,
                       lead_score, sequence_status, assigned_to_name
                FROM leads
                WHERE LOWER(lead_score) = LOWER(%s)
                ORDER BY monday_updated_at DESC
                LIMIT 50
                """,
                (score,)
            )
            rows = [dict(r) for r in cur.fetchall()]
        return _serialize(rows) if rows else f"No leads found with lead score '{score}'."
    finally:
        conn.close()


@tool
def tool_get_leads_by_pipeline_stage(stage: str) -> str:
    """
    Filter leads by their pipeline stage (Monday board group).
    Stages: New Leads, NDA Sent, NDA Signed, CIM Sent, Successful Call,
    Offer/LOI, Under Contract, Working, Warm, Cool, Dead, Brokers.
    Use this to see all leads at a specific point in the sales funnel.
    """
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, name, email, company, status,
                       lead_score, sequence_status, assigned_to_name
                FROM leads
                WHERE LOWER(monday_group_name) = LOWER(%s)
                ORDER BY monday_updated_at DESC
                LIMIT 50
                """,
                (stage,)
            )
            rows = [dict(r) for r in cur.fetchall()]
        return _serialize(rows) if rows else f"No leads found in pipeline stage '{stage}'."
    finally:
        conn.close()


# All tools in one list — imported by the agent
ALL_TOOLS = [
    tool_lookup_person,
    tool_person_profile,
    tool_list_recent_meetings,
    tool_list_conversations_since,
    tool_get_leads_by_status,
    tool_get_stale_leads,
    tool_get_lead_details,
    tool_get_pipeline_summary,
    tool_get_leads_by_sequence_status,
    tool_get_leads_by_lead_score,
    tool_get_leads_by_pipeline_stage,
]
