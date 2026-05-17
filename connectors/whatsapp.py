
import os
import re
import zipfile
import hashlib
from dotenv import load_dotenv
from db.model import get_connection, find_or_create_contact


load_dotenv()
  
WHATSAPP_DIR = "whatsapp"

MSG_PATTERN = re.compile(
    r"^\[(\d{1,2}/\d{1,2}/\d{2,4}),\s*(\d{1,2}:\d{2}:\d{2}\s*[APM]*)\]\s*(.+?):\s*(.*)$"
)


OUR_NAMES = [
    "pablo calad",
    "transworld",
]


def is_our_message(sender: str) -> list[dict]:
    sender_lower = sender.lower()
    return any(name in sender_lower for name in OUR_NAMES)


def parse_chat_file(text: str) -> list[dict]:
    messages = []
    current = None

    for line in text.splitlines():
        match = MSG_PATTERN.match(line)
        if match:
            if current: 
                messages.append(current)
            date, time, sender, body = match.groups()
            current = {
                "date":   date.strip(),
                "time":   time.strip(),
                "sender": sender.strip(),
                "body":   body.strip(),
            }

        elif current:
            current["body"] += " " + line.strip()

    if current:
        messages.append(current)
    

    return messages
            


def extract_contact_name(zip_file: str) -> str:
    name = zip_file.replace("WhatsApp Chat - ", "").replace(".zip","")
    return name.strip()


def sync_whatsapp_zip(zip_path: str) -> dict:
    """
    Process a SINGLE WhatsApp .zip export and insert chunks into contact_embeddings.
    Idempotent: re-running the same zip just dedupes via content_hash.

    Returns: {"contact": str, "messages": int, "new_chunks": int, "skipped": bool}
    """
    zip_file = os.path.basename(zip_path)
    contact_name = extract_contact_name(zip_file)

    phone = None
    if zip_file.startswith("WhatsApp Chat - +"):
        phone = re.sub(r"\D", "", contact_name)

    try:
        with zipfile.ZipFile(zip_path, "r") as z:
            txt_files = [f for f in z.namelist() if f.endswith(".txt")]
            if not txt_files:
                return {"contact": contact_name, "messages": 0, "new_chunks": 0,
                        "skipped": True, "reason": "no .txt file inside zip"}
            raw = z.read(txt_files[0])
            text = raw.decode("utf-8", errors="ignore") or raw.decode("latin-1")
    except Exception as e:
        return {"contact": contact_name, "messages": 0, "new_chunks": 0,
                "skipped": True, "reason": f"could not read zip: {e}"}

    messages = parse_chat_file(text)
    if not messages:
        return {"contact": contact_name, "messages": 0, "new_chunks": 0,
                "skipped": True, "reason": "no parseable messages"}

    conn = get_connection()
    try:
        contact_id, _ = find_or_create_contact(
            conn,
            name=contact_name if not phone else None,
            email=None,
            phone=phone,
            company=None,
            lead_id=None,
        )

        with conn.cursor() as cur:
            cur.execute(
                "UPDATE contacts SET in_whatsapp = TRUE WHERE id = %s",
                (contact_id,)
            )

        new_chunks = 0
        chunk_size = 20
        for i in range(0, len(messages), chunk_size):
            chunk = messages[i:i + chunk_size]
            content = "\n".join(
                f"[{m['date']} {m['time']}] {m['sender']}: {m['body']}"
                for m in chunk
            )
            content_hash = hashlib.md5(content.encode()).hexdigest()

            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO contact_embeddings
                        (contact_id, source, source_ref_id, content_text, content_hash)
                    VALUES (%s, 'whatsapp', %s, %s, %s)
                    ON CONFLICT (content_hash) DO NOTHING
                """, (contact_id, zip_file, content, content_hash))
                if cur.rowcount > 0:
                    new_chunks += 1

        conn.commit()
        return {
            "contact": contact_name,
            "messages": len(messages),
            "new_chunks": new_chunks,
            "skipped": False,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def sync_whatsapp():
    """Loop the whatsapp/ folder and import every zip via sync_whatsapp_zip."""
    total_contacts = 0
    total_messages = 0
    total_new_chunks = 0

    try:
        for zip_file in os.listdir(WHATSAPP_DIR):
            if not zip_file.endswith(".zip"):
                continue

            zip_path = os.path.join(WHATSAPP_DIR, zip_file)
            print(f" Processing: {zip_file}")
            try:
                stats = sync_whatsapp_zip(zip_path)
            except Exception as e:
                print(f"    Failed: {e}")
                continue

            if stats.get("skipped"):
                print(f"    Skipped: {stats.get('reason')}")
                continue

            total_messages += stats["messages"]
            total_new_chunks += stats["new_chunks"]
            total_contacts += 1

        print(f"\nWhatsApp sync done.")
        print(f"  Contacts processed : {total_contacts}")
        print(f"  Messages parsed    : {total_messages}")
        print(f"  New chunks inserted: {total_new_chunks}")

    except Exception as e:
        print(f"Error: {e}")
        raise


