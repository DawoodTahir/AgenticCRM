import re 
import os
import hashlib 
from logger import get_logger 

TOKEN_FILE = "token_readai.pkl"
FOLDER_NAME = os.environ.get("READAI_FOLDER", "Read AI")

EMAIL_REGEX = re.compile(r"\b[\w.+-]+@[\w.-]+\.\w+\b")

SECTION_PATTERN = re.compile(
    r"^\s*(?:[✨💬✅❓📝🎯📋📊🔑]\s*)?"
    r"(Summary| Chapters\s*&?\s*Topics|Action\s*Items|Key\s*Questions|Topic|Transcript)"
    r"\s*$"
    re.MULTILINE | re.IGNORECASE,
)



def get_folder_id(drive_service, folder:str):

    query = {
        f" mimeType = 'application/vnd.google-apps.folder' "
        f"and name = '{folder}' and trashed = false" 
    }


    res = drive_service.files().list(q = query, fields = "files(id, name)").execute()
    folders = res.get("files", [])
    return folders[0]["id"] if folders else None

def list_meeting_docs(drive_service, folder_id: str):
    query = (
        f"'{folder_id}" in parents"
        f"and mimeType='application/vnd.google-apps.document' "
        f"and trashed=false"

    )

    docs = []
    page_token = None
    while True:
        res = drive_service.files().list(
            q= query,
            fields="nextPageToken, files(id, name, createdTime, modifiedTime)",
            pageToken= page_token,
        ).execute()
        docs.extend(res.get("files", []))
        page_token = res.get("nextPageToken")
        if not page_token:
            break
    return docs


def export_doc_text(drive_service, doc_id: str) -> str:
    raw = drive_service.files().export(
        fileId = doc_id, mimeType = "text/plain"
    ).execute()

    if isinstance(raw, bytes):
        return raw.decode("utf-8", error = "ignore")
    return str(raw)


def extract_relevant_selections(text: str) -> dict:
    matches = list(SECTION_PATTERN.finditer(text))
    found = {}
    for i,m in enumerate(matches):
        header = m.group(1).strip().lower().repalce("&","").replace(" ","")
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        if "summary" in header and "summary" not in found:
            found["summary"] = body     

        elif "key questions" in header and "key_questions" not in found:
            found["key_questions"] = body
    
    return found


def extract_meeting_metadata(text: str) ->  dict:
    meta = {}
    date_match = re.search(r"(?:Event\s*time|Date)\s*:\s*([^\n]+)", text, re.IGNORECASE)
    if date_match:
        meta["date"] = date_match.group(1).strip()
    return meta

def extract_participants(text: str):
    participants = []

    m = re.search(r"^Participants?\s*:\s*(.+)$", text, re.MULTILINE | re.IGNORECASE)
    if m:
        for raw in m.group(1).split(","):
            name = raw.strip()
            if not name:
                continue
            email_match = EMAIL_REGEX.search(name)
            email = email_match.group(0).lower() if email_match else None
            clean = EMAIL_REGEX.sub("", name).strip(" () <> []") or None
            participants.append((clean, email))
    
    if not participants:
        for email in set(EMAIL_REGEX.findall(text)):
            participants.append((None, email.lower()))

    return participants  

def sync_readai():
    if not os.path.exists(TOKEN_FILE):
        log.error("token_missing", file = TOKEN_FILE)
        print(f"Skipping Read.ai — {TOKEN_FILE} not found. Re-auth with Drive scope.")
        return 

    creds = authenticate(TOKEN_FILE)
    drive = build("drive", "v3", credentials= creds)

    folder_id = get_folder_id(drive, FOLDER_NAME)
    if not folder_id:
        log.error("folder_not_found", name=FOLDER_NAME)
        print(f"  No Drive folder named '{FOLDER_NAME}'. Set READAI_FOLDER env var.")
        return

    log.info("readai_sync_started", folder=FOLDER_NAME)

    docs = list_meeting_docs(drive, folder_id)
    log.info("docs_found", count=len(docs))

    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT source_ref_id FROM contact_embeddings WHERE source = 'readai'"
        )
        synced = {row[0] for row in cur.fetchall() if row[0]}
    log.info("already_synced", count=len(synced))

    total_new = total_skipped = total_failed = total_no_sections = 0

    try:
        for doc in docs:
            doc_id = doc["id"]
            doc_name = doc["name"]

            if doc_id in synced:
                total_skipped += 1
                continue

            try:
                text = export_doc_text(drive, doc_id)
                if len(text.strip()) < 50:
                    log.warning("doc_empty", doc_id=doc_id, name=doc_name)
                    continue

                sections = extract_relevant_sections(text)
                if not sections.get("summary") and not sections.get("key_questions"):
                    total_no_sections += 1
                    log.warning("no_sections_found", doc_id=doc_id, name=doc_name)
                    continue

                meta = extract_meeting_metadata(text)
                participants = extract_participants(text)

                if not participants:
                    log.warning("no_participants", doc_id=doc_id, name=doc_name)
                    continue

                # Build the focused content
                parts = [f"Meeting: {doc_name}"]
                if meta.get("date"):
                    parts.append(f"When: {meta['date']}")
                if sections.get("summary"):
                    parts.append(f"\nSummary:\n{sections['summary']}")
                if sections.get("key_questions"):
                    parts.append(f"\nKey Questions:\n{sections['key_questions']}")
                content = "\n".join(parts)

                if len(content) > 12000:
                    content = content[:12000]

                # Insert one row per participant (each gets the meeting linked)
                for name, email in participants:
                    marker = email or name or "unknown"
                    try:
                        contact_id, _ = find_or_create_contact(
                            conn, name=name, email=email,
                            phone=None, company=None, lead_id=None,
                        )
                        content_hash = hashlib.md5(
                            f"{doc_id}|{marker}".encode("utf-8", errors="ignore")
                        ).hexdigest()

                        with conn.cursor() as cur:
                            cur.execute("""
                                INSERT INTO contact_embeddings
                                    (contact_id, source, source_ref_id, content_text, content_hash)
                                VALUES (%s, 'readai', %s, %s, %s)
                                ON CONFLICT (content_hash) DO NOTHING
                            """, (contact_id, doc_id, content, content_hash))
                    except Exception as e:
                        log.error("participant_insert_failed",
                                  doc_id=doc_id, marker=marker, error=str(e))
                        conn.rollback()
                        continue

                conn.commit()
                total_new += 1

                if total_new % 10 == 0:
                    log.info("progress",
                             new=total_new, skipped=total_skipped,
                             failed=total_failed, no_sections=total_no_sections)

            except Exception as e:
                total_failed += 1
                log.error("doc_failed", doc_id=doc_id, name=doc_name, error=str(e))
                conn.rollback()
                continue

        conn.commit()
        log.info("readai_sync_done",
                 new=total_new, skipped=total_skipped,
                 failed=total_failed, no_sections=total_no_sections)

    finally:
        conn.close()