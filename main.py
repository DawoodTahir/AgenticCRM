import sys
from connectors.monday import sync_monday_leads,embed_monday_notes
from connectors.gmail import sync_all_gmail
from connectors.whatsapp import sync_whatsapp
from connectors.outlook_mbox import import_outlook_mbox
from agents.lead_agent import ask
from bot.telegram_bot import run_bot
from db.model import get_connection, resolve_monday_contacts, merge_duplicate_contacts
from connectors.embeddings import embed_all_pending

if __name__ == "__main__":
    if len(sys.argv) > 1:
        if sys.argv[1] == "monday":
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
        elif sys.argv[1] == "outlook":
            import_outlook_mbox()
        elif sys.argv[1] == "import-mbox":
            from connectors.outlook_mbox import import_mbox
            path  = sys.argv[2]
            label = sys.argv[3]
            field = sys.argv[4] if len(sys.argv) > 4 else "from"
            import_mbox(path, source=label, contact_field=field)
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
        elif sys.argv[1] == "monday-notes":
            embed_monday_notes()
        elif sys.argv[1] == "eval":
            from tests.run_eval import run_eval
            run_eval()
        elif sys.argv[1] == "faithfulness":
            from tests.run_faithfulness import run as run_faith
            run_faith()
        elif sys.argv[1] == "cost-report":
            from tests.cost_report import parse_log, group_into_queries, report
            log_path = sys.argv[2] if len(sys.argv) > 2 else "logs/crm.log"
            events = parse_log(log_path)
            queries = group_into_queries(events)
            report(queries)
        elif sys.argv[1] == "sync-all":
            # Render cron entry point — runs every step that doesn't need local token files.
            # Each step is independent: a failure logs and moves on, never aborts the run.
            import time
            t_total = time.time()
            print("=== SYNC ALL ===", flush=True)

            print("\n[1/4] Monday board → leads + lead_notes", flush=True)
            try:
                t0 = time.time()
                sync_monday_leads()
                print(f"  OK ({time.time()-t0:.1f}s)", flush=True)
            except Exception as e:
                print(f"  FAILED: {e}", flush=True)

            print("\n[2/4] Resolve Monday leads → contacts", flush=True)
            try:
                t0 = time.time()
                conn = get_connection()
                stats = resolve_monday_contacts(conn)
                conn.commit()
                print(f"  OK ({time.time()-t0:.1f}s) — "
                      f"created={stats['created']} email={stats['email_match']} "
                      f"phone={stats['phone_match']} name={stats['name_match']} "
                      f"failed={stats.get('failed', 0)}", flush=True)
            except Exception as e:
                print(f"  FAILED: {e}", flush=True)
            finally:
                try: conn.close()
                except Exception: pass

            print("\n[3/4] Monday notes/comments → contact_embeddings", flush=True)
            try:
                t0 = time.time()
                embed_monday_notes()
                print(f"  OK ({time.time()-t0:.1f}s)", flush=True)
            except Exception as e:
                print(f"  FAILED: {e}", flush=True)

            print("\n[4/4] Generate embeddings for pending rows", flush=True)
            try:
                t0 = time.time()
                embed_all_pending()
                print(f"  OK ({time.time()-t0:.1f}s)", flush=True)
            except Exception as e:
                print(f"  FAILED: {e}", flush=True)

            print(f"\n=== SYNC ALL DONE ({time.time()-t_total:.1f}s total) ===", flush=True)
        elif sys.argv[1] == "ask":
            question = " ".join(sys.argv[2:])
            print(ask(question))
    else:
        sync_monday_leads()
        run_bot()
