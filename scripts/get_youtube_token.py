from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

def main():
    flow = InstalledAppFlow.from_client_secrets_file(
        "client_secret.json",
        scopes=SCOPES,
    )
    credentials = flow.run_local_server(
        host="localhost",
        port=0,
        access_type="offline",
        prompt="consent",
        open_browser=True,
    )
    if not credentials.refresh_token:
        raise RuntimeError(
            "Google did not return a refresh token. Revoke this app's access "
            "in your Google Account and run the script again."
        )
    print("\nYOUTUBE_REFRESH_TOKEN (copy this into GitHub Actions secret):")
    print(credentials.refresh_token)
    print("\nDo not share this token or commit it to GitHub.")

if __name__ == "__main__":
    main()
