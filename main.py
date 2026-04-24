import sys
from connectors.monday import sync_monday_leads
from connectors.gmail import sync_all_gmail
from connectors.whatsapp import sync_whatsapp
from agents.lead_agent import ask
from bot.telegram_bot import run_bot
from db.model import get_connection, resolve_monday_contacts, merge_duplicate_contacts
from connectors.embeddings import embed_all_pending


if __name__ == "__main__":
    if len(sys.argv) > 1:
        if sys.argv[1] == "sync":
            sync_monday_leads()

        elif sys.argv[1] == "resolve":
            print("=== Resolving contacts from Monday leads ===")
            conn = get_connection()
            try:
                stats = resolve_monday_contacts(conn)
                conn.commit()
                print(f"  Created  : {stats['created']}")
                print(f"  By email : {stats['email_match']}")
                print(f"  By phone : {stats['phone_match']}")
                print(f"  Skipped  : {stats['skipped']}")
            finally:
                conn.close()

        elif sys.argv[1] == "gmail":
            sync_all_gmail()
        elif sys.argv[1] == "whatsapp":
            sync_whatsapp()
        elif sys.argv[1] == "bot":
            run_bot()
        elif sys.argv[1] == "embed":
            embed_all_pending()
        elif sys.argv[1] == "merge":
            print("=== Merging duplicate contacts ===")
            conn = get_connection()
            try:
                stats = merge_duplicate_contacts(conn)
                print(f"\n  Duplicate groups found : {stats['groups_found']}")
                print(f"  Contacts merged        : {stats['contacts_merged']}")
            finally:
                conn.close()
        elif sys.argv[1] == "ask":
            question = " ".join(sys.argv[2:])
            print(ask(question))
    else:
        sync_monday_leads()
        run_bot()
