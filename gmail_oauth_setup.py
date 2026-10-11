"""One-time, local: get a Gmail OAuth refresh token and store it as GitHub secrets.

  .venv/bin/python gmail_oauth_setup.py CLIENT_JSON GMAIL_ACCOUNT OPS_REPO
      CLIENT_JSON  the "Desktop app" OAuth client downloaded from Google Cloud
      OPS_REPO     owner/name of the private repo that runs the pipeline

1. Opens the browser to Google's consent page, pre-filled for GMAIL_ACCOUNT. Sign in as that
   account and click Allow (the app is unverified, so click through the warning).
2. Signs in over IMAP + SMTP from this machine with the new token (gmail_auth.check).
3. Only if that passes, sets GMAIL_OAUTH_CLIENT_ID / _CLIENT_SECRET / _REFRESH_TOKEN on the
   ops repo with `gh secret set`.

Nothing is printed or written to disk. Needs google-auth-oauthlib (in .venv; the runner
doesn't need it). Delete the downloaded client JSON afterwards.
"""
import json
import subprocess
import sys

from google_auth_oauthlib.flow import InstalledAppFlow

from gmail_auth import SCOPES, check


def main(client_json, gmail_account, ops_repo):
    with open(client_json) as f:
        client = json.load(f)["installed"]      # a "Desktop app" client; "web" won't work

    flow = InstalledAppFlow.from_client_secrets_file(client_json, SCOPES)
    # offline + consent: without both, Google can skip issuing a refresh token.
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent",
                                  login_hint=gmail_account)
    if not creds.refresh_token:
        sys.exit("Google returned no refresh token — nothing saved.")

    check(gmail_account, creds.token)           # raises on a bad token, before any secret moves

    for name, value in (("GMAIL_OAUTH_CLIENT_ID", client["client_id"]),
                        ("GMAIL_OAUTH_CLIENT_SECRET", client["client_secret"]),
                        ("GMAIL_OAUTH_REFRESH_TOKEN", creds.refresh_token)):
        subprocess.run(["gh", "secret", "set", name, "-R", ops_repo],
                       input=value, text=True, check=True)
    print("Secrets set. Next: the GitHub check run.")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    main(*sys.argv[1:])
