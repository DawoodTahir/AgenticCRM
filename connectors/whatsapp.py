
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


def sync_whatsapp():

    conn = get_connection()
    total_contacts = 0
    total_messages = 0

    try:
        for zip_file in os.listdir(WHATSAPP_DIR):
            if not zip_file.endswith(".zip"):
                continue
            
            contact_name = extract_contact_name(zip_file)
            zip_path = os.path.join(WHATSAPP_DIR, zip_file)

            print(f" Processing: {contact_name}")

            phone = None 
            if zip_file.startswith("WhatsApp Chat - +"):
                phone = re.sub(r"\D", "", contact_name)


            try:
                with zipfile.ZipFile(zip_path,"r") as z:
                    txt_files = [f for f in z.namelist() if f.endswith(".txt")]
                    if not txt_files:
                        continue
                    
                    raw = z.read(txt_files[0])
                    text = raw.decode("utf-8", errors="ignore") or raw.decode("latin-1")
            except Exception as e:
                print(f"    Could not read {zip_file}: {e}")
                continue 

            messages = parse_chat_file(text)
            if not messages:
                continue

            contact_id, _ = find_or_create_contact(
                conn,
                name    = contact_name if not phone else None,
                email   = None,
                phone   = phone,
                company = None,
                lead_id = None,
            )
            

            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE contacts SET in_whatsapp = TRUE WHERE id = %s",
                    (contact_id,)
                )


            chunk_size = 20
            for i in range(0, len(messages), chunk_size):
                chunk = messages[i: i + chunk_size]
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


            total_messages += len(messages)
            total_contacts += 1 
            conn.commit()


        print(f"\nWhatsApp sync done.")
        print(f"  Contacts processed : {total_contacts}")
        print(f"  Messages stored    : {total_messages}")

    except Exception as e:
        conn.rollback()
        print(f"Error: {e}")
        raise
    finally:
        conn.close()


