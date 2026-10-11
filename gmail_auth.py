"""Gmail sign-in with OAuth (XOAUTH2) over IMAP and SMTP. Stdlib only.

Since ~21:00 UTC 2026-10-10 Gmail rejects the app-password login from GitHub-hosted runners
("Invalid credentials") while the same password works from a home Mac. OAuth replaces only
the login: the IMAP/SMTP code that reads and sends mail stays as it is.

Env: GMAIL_USER, GMAIL_OAUTH_CLIENT_ID, GMAIL_OAUTH_CLIENT_SECRET, GMAIL_OAUTH_REFRESH_TOKEN.
The refresh token comes from gmail_oauth_setup.py (run once, locally).

Local runs on the Mac have no OAuth secrets: the portfolio dashboard build imports
still_needed / email_docs with only GMAIL_APP_PASSWORD (from property-management's
.env.optimizer). Gmail still accepts that from home, so with no OAuth env set, the login falls
back to it. CI never sets GMAIL_APP_PASSWORD, so runners always use OAuth.

The ops repo keeps its own copy of the OAuth logic (gmail_oauth.py) for the failure alert and
the monthly reminder, which must work even when cloning this repo is what failed.

  python gmail_auth.py --check   # sign in over IMAP + SMTP, open INBOX read-only, log out.
                                 # Reads no mail, sends no mail.
"""
import imaplib
import json
import os
import smtplib
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

TOKEN_URI = "https://oauth2.googleapis.com/token"
# The only scope Gmail accepts for IMAP/SMTP — the same full-mailbox reach the app password had.
SCOPES = ["https://mail.google.com/"]
ENV = ("GMAIL_OAUTH_CLIENT_ID", "GMAIL_OAUTH_CLIENT_SECRET", "GMAIL_OAUTH_REFRESH_TOKEN")

_TOKEN = None  # per-process memo; an access token lasts an hour and a run takes under 20 min


def _oauth():
    return all(os.environ.get(k) for k in ENV)


def configured():
    return _oauth() or bool(os.environ.get("GMAIL_APP_PASSWORD"))


def access_token(client_id, client_secret, refresh_token):
    """Trade the long-lived refresh token for a one-hour access token."""
    body = urllib.parse.urlencode({"client_id": client_id, "client_secret": client_secret,
                                   "refresh_token": refresh_token,
                                   "grant_type": "refresh_token"}).encode()
    try:
        with urllib.request.urlopen(TOKEN_URI, body, timeout=30) as r:
            return json.load(r)["access_token"]
    except urllib.error.HTTPError as e:
        # Google's error body names the cause and holds no secret. "invalid_grant" = the refresh
        # token expired or was revoked: re-run gmail_oauth_setup.py.
        detail = e.read()[:300].decode("utf-8", "replace")
        raise RuntimeError(f"Gmail OAuth token refresh failed: HTTP {e.code} {detail}") from None


def token():
    global _TOKEN
    if _TOKEN is None:
        _TOKEN = access_token(*(os.environ[k] for k in ENV))
    return _TOKEN


def xoauth2(user, tok):
    return f"user={user}\x01auth=Bearer {tok}\x01\x01"


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


def _app_password():
    """The local fallback: an app password, only when no OAuth is configured."""
    return None if _oauth() else os.environ.get("GMAIL_APP_PASSWORD")


def imap_login(m, user, tok=None):
    pw = None if tok else _app_password()
    if pw:
        m.login(user, pw)
        return
    m.authenticate("XOAUTH2", _responder(xoauth2(user, tok or token()).encode()))


def smtp_login(s, user, tok=None):
    """Call after starttls()."""
    pw = None if tok else _app_password()
    if pw:
        s.login(user, pw)
        return
    s.ehlo_or_helo_if_needed()   # STARTTLS resets the session; Gmail wants EHLO before AUTH
    s.auth("XOAUTH2", _responder(xoauth2(user, tok or token())), initial_response_ok=True)


def check(user, tok):
    """Sign in over IMAP and SMTP. Opens INBOX read-only; reads and sends nothing."""
    m = imaplib.IMAP4_SSL("imap.gmail.com")
    imap_login(m, user, tok)
    typ, _ = m.select("INBOX", readonly=True)
    m.logout()
    if typ != "OK":
        raise RuntimeError(f"IMAP login worked but INBOX did not open: {typ}")
    print("IMAP: OAuth login OK, INBOX opened read-only")

    with smtplib.SMTP("smtp.gmail.com", 587) as s:
        s.starttls(context=ssl.create_default_context())
        smtp_login(s, user, tok)
    print("SMTP: OAuth login OK (nothing sent)")


if __name__ == "__main__":
    if sys.argv[1:] != ["--check"]:
        sys.exit(__doc__)
    check(os.environ["GMAIL_USER"], token())
