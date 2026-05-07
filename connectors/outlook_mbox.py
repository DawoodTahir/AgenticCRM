"""
One-time importer for Outlook .mbox exports.
After this runs, ongoing email sync is handled by Gmail (the redirected account).
"""
import os
import re
import mailbox
import hashlib
from email.header import decode_header
from email.utils import getaddresses
from db.model import get_connection, find_or_create_contact
from logger import get_logger

log = get_logger("outlook_mbox")

INBOX_PATH = "db/Inbox.partial.mbox/mbox"
SENT_PATH  = "db/Sent.partial.mbox/mbox.mbox"


def decode_mime_header(value: str) -> str:
    """Decode RFC 2047 MIME-encoded header values like '=?UTF-8?B?...?='"""
    if not value:
        return ""
    parts = decode_header(value)
    decoded = []
    for text, charset in parts:
        if isinstance(text, bytes):
            try:
                decoded.append(text.decode(charset or "utf-8", errors="ignore"))
            except (LookupError, UnicodeDecodeError):
                decoded.append(text.decode("utf-8", errors="ignore"))
        else:
            decoded.append(text)
    return " ".join(decoded).strip()


def get_body(msg) -> str:
    """Extract plain text body, walking multipart. Falls back to stripped HTML."""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disposition = str(part.get("Content-Disposition", ""))
            if ctype == "text/plain" and "attachment" not in disposition:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    try:
                        return payload.decode(charset, errors="ignore")
                    except (LookupError, UnicodeDecodeError):
                        return payload.decode("utf-8", errors="ignore")
        # Fallback: strip HTML
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    html = payload.decode(charset, errors="ignore")
                    return re.sub(r"<[^>]+>", " ", html)
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            try:
                return payload.decode(charset, errors="ignore")
            except (LookupError, UnicodeDecodeError):
                return payload.decode("utf-8", errors="ignore")
    return ""


def parse_address(header_value: str) -> tuple:
    """Parse 'Name <email@x.com>' → (name, email). Handles MIME-encoded names."""
    if not header_value:
        return None, None
    decoded = decode_mime_header(header_value)
    addrs = getaddresses([decoded])
    if not addrs:
        return None, None
    name, email = addrs[0]
    name = name.strip() or None
    email = email.lower().strip() if email else None
    return name, email


def parse_all_addresses(*header_values: str) -> list:
    """Parse To/Cc/Bcc headers → de-duplicated list of (name, email) tuples."""
    decoded_headers = [decode_mime_header(h) for h in header_values if h]
    if not decoded_headers:
        return []
    seen = set()
    result = []
    for name, email in getaddresses(decoded_headers):
        name = (name or "").strip() or None
        email = email.lower().strip() if email else None
        if not email and not name:
            continue
        key = email or name
        if key in seen:
            continue
        seen.add(key)
        result.append((name, email))
    return result


def import_mbox(mbox_path: str, source: str, contact_field: str):
    """
    Import an mbox file into contact_embeddings.
    contact_field = 'from' for inbox (sender = contact)
                  = 'to'   for sent  (first recipient = contact)
    """
    if not os.path.exists(mbox_path):
        print(f"  Skipping {source} — file not found at {mbox_path}")
        return

    print(f"\nImporting {source} from {mbox_path}...")
    print(f"  This is a one-time import. Reading large file — please be patient.")

    conn = get_connection()

    with conn.cursor() as cur:
        cur.execute(
            "SELECT source_ref_id FROM contact_embeddings WHERE source = %s",
            (source,)
        )
        synced_ids = {row[0] for row in cur.fetchall() if row[0]}
    print(f"  {len(synced_ids)} messages already imported, skipping those.")

    mbox = mailbox.mbox(mbox_path)

    total_seen = 0
    total_new = 0
    total_skipped = 0
    total_failed = 0

    try:
        for key, msg in mbox.iteritems():
            total_seen += 1

            try:
                msg_id = (msg.get("Message-ID") or f"{source}-{key}").strip()

                if msg_id in synced_ids:
                    total_skipped += 1
                    continue

                subject = decode_mime_header(msg.get("Subject", ""))
                from_header = msg.get("From", "")
                to_header = msg.get("To", "")
                cc_header = msg.get("Cc", "")
                bcc_header = msg.get("Bcc", "")
                body = get_body(msg)

                if contact_field == "from":
                    name, email = parse_address(from_header)
                    contacts_to_link = [(name, email)] if (name or email) else []
                else:
                    contacts_to_link = parse_all_addresses(to_header, cc_header, bcc_header)

                if not contacts_to_link:
                    total_failed += 1
                    continue

                content = (
                    f"Subject: {subject}\n"
                    f"From: {decode_mime_header(from_header)}\n"
                    f"To: {decode_mime_header(to_header)}\n"
                    f"Cc: {decode_mime_header(cc_header)}\n\n"
                    f"{body}"
                ).strip()

                if len(content) > 12000:
                    content = content[:12000]

                inserted_for_message = False
                for contact_name, contact_email in contacts_to_link:
                    if contact_email and any(
                        p in contact_email for p in
                        ["noreply", "no-reply", "do-not-reply", "donotreply"]
                    ):
                        continue

                    contact_id, _ = find_or_create_contact(
                        conn,
                        name=contact_name,
                        email=contact_email,
                        phone=None,
                        company=None,
                        lead_id=None,
                    )

                    # Salt hash with recipient so the same Sent email can attach to multiple contacts.
                    salt = (contact_email or contact_name or "").lower()
                    content_hash = hashlib.md5(
                        (content + "|" + salt).encode("utf-8", errors="ignore")
                    ).hexdigest()

                    with conn.cursor() as cur:
                        cur.execute("""
                            INSERT INTO contact_embeddings
                                (contact_id, source, source_ref_id, content_text, content_hash)
                            VALUES (%s, %s, %s, %s, %s)
                            ON CONFLICT (content_hash) DO NOTHING
                        """, (contact_id, source, msg_id, content, content_hash))

                    inserted_for_message = True

                if inserted_for_message:
                    total_new += 1
                else:
                    total_skipped += 1

                if total_new % 100 == 0:
                    conn.commit()
                    print(f"  Progress: {total_seen} seen, {total_new} new, "
                          f"{total_skipped} skipped, {total_failed} failed")

            except Exception as e:
                total_failed += 1
                log.error("mbox_message_failed", source=source, key=key, error=str(e))
                conn.rollback()
                continue

        conn.commit()
        print(f"\n  Done: {total_seen} seen, {total_new} new, "
              f"{total_skipped} skipped, {total_failed} failed.")

    finally:
        mbox.close()
        conn.close()


def import_outlook_mbox():
    """One-time import of both Inbox and Sent mbox files."""
    import_mbox(INBOX_PATH, source="outlook_inbox", contact_field="from")
    import_mbox(SENT_PATH,  source="outlook_sent",  contact_field="to")
    print("\nOutlook mbox import complete. Run `python main.py embed` next.")
