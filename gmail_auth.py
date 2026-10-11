"""Gmail sign-in with OAuth (XOAUTH2) over IMAP and SMTP.

Since ~21:00 UTC 2026-10-10 Gmail rejects the app-password login from GitHub-hosted runners
("Invalid credentials") while the same password works from a home Mac. OAuth replaces only
the login: the IMAP/SMTP code that reads and sends mail stays as it is.

Env: GMAIL_USER, GMAIL_OAUTH_CLIENT_ID, GMAIL_OAUTH_CLIENT_SECRET, GMAIL_OAUTH_REFRESH_TOKEN.
The refresh token comes from gmail_oauth_setup.py (run once, locally).

  python gmail_auth.py --check   # sign in over IMAP + SMTP, open INBOX read-only, log out.
                                 # Reads no mail, sends no mail.
"""
import imaplib
import os
import smtplib
import ssl
import sys

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

TOKEN_URI = "https://oauth2.googleapis.com/token"
# The only scope Gmail accepts for IMAP/SMTP — the same full-mailbox reach the app password had.
SCOPES = ["https://mail.google.com/"]


def access_token(client_id, client_secret, refresh_token):
    """Trade the long-lived refresh token for a one-hour access token."""
    creds = Credentials(None, refresh_token=refresh_token, client_id=client_id,
                        client_secret=client_secret, token_uri=TOKEN_URI)
    creds.refresh(Request())
    return creds.token


def env_access_token():
    return access_token(os.environ["GMAIL_OAUTH_CLIENT_ID"],
                        os.environ["GMAIL_OAUTH_CLIENT_SECRET"],
                        os.environ["GMAIL_OAUTH_REFRESH_TOKEN"])


def xoauth2(user, token):
    return f"user={user}\x01auth=Bearer {token}\x01\x01"


def _responder(first):
    """SASL callback: the auth string once, then empty.

    On a rejected token Gmail answers with a JSON error challenge and waits for an empty
    reply before sending the failure. Answering it with the auth string again would loop.
    """
    sent = []

    def respond(challenge=None):
        if sent:
            return b"" if isinstance(first, bytes) else ""
        sent.append(True)
        return first
    return respond


def imap_login(m, user, token):
    m.authenticate("XOAUTH2", _responder(xoauth2(user, token).encode()))


def smtp_login(s, user, token):
    """Call after starttls()."""
    s.ehlo_or_helo_if_needed()   # STARTTLS resets the session; Gmail wants EHLO before AUTH
    s.auth("XOAUTH2", _responder(xoauth2(user, token)), initial_response_ok=True)


def check(user, token):
    """Sign in over IMAP and SMTP. Opens INBOX read-only; reads and sends nothing."""
    m = imaplib.IMAP4_SSL("imap.gmail.com")
    imap_login(m, user, token)
    typ, _ = m.select("INBOX", readonly=True)
    m.logout()
    if typ != "OK":
        raise RuntimeError(f"IMAP login worked but INBOX did not open: {typ}")
    print("IMAP: OAuth login OK, INBOX opened read-only")

    with smtplib.SMTP("smtp.gmail.com", 587) as s:
        s.starttls(context=ssl.create_default_context())
        smtp_login(s, user, token)
    print("SMTP: OAuth login OK (nothing sent)")


if __name__ == "__main__":
    if sys.argv[1:] != ["--check"]:
        sys.exit(__doc__)
    check(os.environ["GMAIL_USER"], env_access_token())
