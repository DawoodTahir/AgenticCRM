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
                    "client_status": lead.get("client_status"),
                    "follow_up_status": lead.get("follow_up_status"),
                    "mood": lead.get("mood"),
                    "value_level": lead.get("value_level"),
                    "sentiment": lead.get("sentiment"),
                    "position": lead.get("position"),
                    "due_date": lead.get("due_date"),
                    "assigned_to": lead.get("assigned_to_name"),
                    "notes": lead.get("notes", []),
                }

        # --- Step 4: Recent conversations (WhatsApp + Gmail) ---
        search_text = contact.get("name") or contact.get("email") or "contact"
        query_embedding = generate_embedding(search_text)
        conversations = semantic_search(
            conn, query_embedding, contact_id=contact["id"], limit=5
        )

        if conversations:
            result["conversations"] = [
                {
                    "source": c["source"],
                    "preview": c["content_text"][:400],
                }
                for c in conversations
            ]

        return _serialize(result)

    finally:
        conn.close()


@tool
def tool_get_leads_by_status(status: str) -> str:
    """
    Get all leads with a specific client status.
    Valid statuses in this board: 'Potential Client', 'Client'.
    Use this to see all leads at a particular stage.
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
def tool_get_leads_by_follow_up_status(status: str) -> str:
    """
    Filter leads by their follow-up status.
    Valid values: 'Cool', 'LATE', 'IN PROGRESS'.
    Use 'LATE' to find overdue leads that need immediate attention.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name, email, company, client_status,
                       follow_up_status, due_date, assigned_to_name
                FROM leads
                WHERE LOWER(follow_up_status) = LOWER(%s)
                ORDER BY due_date ASC NULLS LAST
                LIMIT 50
                """,
                (status,)
            )
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]
        return _serialize(rows) if rows else f"No leads with follow-up status '{status}'."
    finally:
        conn.close()


@tool
def tool_get_leads_by_mood(mood: str) -> str:
    """
    Get leads filtered by their mood/relationship status.
    Valid values: 'Happy', 'fine', 'Doesnt know us', 'Needs Some...', 'Neutral', 'Positive'.
    Use this to find warm leads (Happy) or cold leads (Doesnt know us).
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name, email, company, client_status, mood, assigned_to_name
                FROM leads
                WHERE mood ILIKE %s
                ORDER BY monday_updated_at DESC
                LIMIT 50
                """,
                (f"%{mood}%",)
            )
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]
        return _serialize(rows) if rows else f"No leads found with mood '{mood}'."
    finally:
        conn.close()


# All tools in one list — imported by the agent
ALL_TOOLS = [
    tool_lookup_person,
    tool_get_leads_by_status,
    tool_get_stale_leads,
    tool_get_lead_details,
    tool_get_pipeline_summary,
    tool_get_leads_by_follow_up_status,
    tool_get_leads_by_mood,
]
