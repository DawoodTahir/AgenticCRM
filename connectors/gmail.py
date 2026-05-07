import os
import re
import base64
import hashlib
from datetime import datetime
from dotenv import load_dotenv
from googleapiclient.discovery import build
from connectors.gmail_auth import authenticate
from db.model import get_connection, find_or_create_contact
from logger import get_logger


load_dotenv()
log = get_logger("gmail")

def get_email_body(payload):
    if payload.get("body",{}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors = "ignore")

    for part in payload.get("parts", []):
        if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
            return base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors = "ignore")

    return ""


def sync_gmail(token_file: str, account_label: str):
    if not os.path.exists(token_file):
        log.warning("token_file_missing", file=token_file, account=account_label)
        print(f"  Skipping {account_label} — token file '{token_file}' not found. Run gmail_auth.py first.")
        return

    conn = get_connection()
    creds = authenticate(token_file)
    service = build("gmail", "v1", credentials=creds)

    print(f"\nSyncing {account_label}...")

    # Load already-synced Gmail message IDs from DB
    with conn.cursor() as cur:
        cur.execute(
            "SELECT source_ref_id FROM contact_embeddings WHERE source = 'gmail'"
        )
        synced_ids = {row[0] for row in cur.fetchall()}

    print(f"  {len(synced_ids)} emails already in DB, skipping those...")

    total = 0
    skipped = 0
    page_token = None

    try:
        while True:
            params = {"userId": "me", "maxResults": 100}
            if page_token:
                params["pageToken"] = page_token

            result = service.users().messages().list(**params).execute()
            messages = result.get("messages", [])

            if not messages:
                break

            for msg in messages:
                # Skip if already synced
                if msg["id"] in synced_ids:
                    skipped += 1
                    continue

                try:
                    detail = service.users().messages().get(
                        userId="me", id = msg["id"], format = "full"
                    ).execute()

                    headers = {h["name"]: h["value"] for h in detail.get("payload", {}).get("headers", [])}
                    subject = headers.get("Subject", "")
                    sender  = headers.get("From", "")
                    recipient = headers.get("To", "")
                    body    = get_email_body(detail.get("payload", {}))

                    email_match = re.search(r"<(.+?)>", sender)
                    sender_email = email_match.group(1) if email_match else sender if "@" in sender else None

                    name_match  = re.search(r"^(.+?)\s*<", sender)
                    sender_name = name_match.group(1).strip() if name_match else sender_email

                    if not sender_email and not sender_name:
                        continue

                    contact_id, _ = find_or_create_contact(
                        conn,
                        name    = sender_name,
                        email   = sender_email,
                        phone   = None,
                        company = None,
                        lead_id = None,
                    )

                    content = f"Subject: {subject}\nFrom: {sender}\nTo: {recipient}\n\n{body}".strip()
                    content_hash = hashlib.md5(content.encode()).hexdigest()

                    with conn.cursor() as cur:
                        cur.execute("""
                            INSERT INTO contact_embeddings
                                (contact_id, source, source_ref_id, content_text, content_hash)
                            VALUES (%s, 'gmail', %s, %s, %s)
                            ON CONFLICT (content_hash) DO NOTHING """,
                             (contact_id, msg["id"], content, content_hash))

                    total += 1
                except Exception as e:
                    log.error("gmail_message_failed", msg_id=msg.get("id"), error=str(e))
                    continue

            conn.commit()

            print(f"  {total} new emails synced, {skipped} skipped...")

            next_token = result.get("nextPageToken")
            if not next_token:
                break
            page_token = next_token

        print(f"\n  Done! {total} new, {skipped} already existed.")

    except Exception as e:
        conn.rollback()
        print(f"Error syncing {account_label}: {e}")
        raise
    finally:
        conn.close()






def sync_all_gmail():
    accounts = [
        ("token_gmail1.pkl", "account1"),
        ("token_gmail2.pkl", "account2"),
        ("token_gmail3.pkl", "outlook_redirected"),
    ]
    for token_file, label in accounts:
        try:
            sync_gmail(token_file, label)
        except Exception as e:
            log.error("gmail_account_sync_failed", account=label, error=str(e))
            print(f"  Failed to sync {label}: {e} — continuing with next account...")




