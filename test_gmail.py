from googleapiclient.discovery import build
from connectors.gmail_auth import authenticate

creds = authenticate("token_gmail1.pkl")
service = build("gmail", "v1", credentials=creds)

# Fetch 5 most recent emails
results = service.users().messages().list(userId="me", maxResults=5).execute()
messages = results.get("messages", [])

for msg in messages:
    detail = service.users().messages().get(userId="me", id=msg["id"], format="metadata").execute()
    headers = {h["name"]: h["value"] for h in detail["payload"]["headers"]}
    print(f"From: {headers.get('From')}")
    print(f"Subject: {headers.get('Subject')}")
    print(f"Date: {headers.get('Date')}")
    print("---")
