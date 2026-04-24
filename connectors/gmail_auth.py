import os
import pickle
os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/userinfo.email",
]

def authenticate(token_file: str, credentials_file: str = "creds.json"):
    creds = None

    if os.path.exists(token_file):
        with open(token_file, "rb") as f:
            creds = pickle.load(f)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(credentials_file, SCOPES)
            creds = flow.run_local_server(port=0, access_type="offline")

        with open(token_file, "wb") as f:
            pickle.dump(creds, f)

    return creds


if __name__ == "__main__":
    accounts = [
        ("token_gmail3.pkl", "Outlook_Redirected"),
        ("token_gmail2.pkl", "Gmail Account 2"),
        ("token_gmail1.pkl", "Gmail Account 1"),
    ]
    for token_file, label in accounts:
        print(f"\nAuthenticating {label}...")
        print("Browser will open — log in with the correct account.")
        input("Press ENTER when ready...")
        creds = authenticate(token_file)
        print(f"Success! Saved to {token_file}")
