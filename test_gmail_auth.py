#!/usr/bin/env python3
"""
test_gmail_auth.py — regression guard on the Gmail OAuth sign-in.

Gmail has rejected the app-password login from GitHub-hosted runners since ~21:00 UTC
2026-10-10; gmail_auth.py signs in with XOAUTH2 instead. What's load-bearing:

  1. **The SASL string is byte-exact.** `user=…^Aauth=Bearer …^A^A`. Any drift is an
     "Invalid credentials" that looks exactly like the outage this replaced.
  2. **A rejected token gets an empty reply, once.** Gmail answers a bad token with a JSON
     error challenge and waits for an empty line. Re-sending the auth string loops.
     Checked through imaplib's own authenticator, not a re-implementation of it.
  3. **One token refresh per run.** Every login in a poll shares it.
  4. **A failed refresh says why** (Google's error body, e.g. invalid_grant = re-run
     gmail_oauth_setup.py) and never echoes the refresh token or client secret.
  5. **The app password is a local-only fallback.** Used only when no OAuth env is set (the
     dashboard build on the Mac); with OAuth set, OAuth always wins.

Needs no secrets and no network: the token endpoint is stubbed.

    python3 test_gmail_auth.py
"""
import base64
import imaplib
import io
import json
import os
import sys
import urllib.error

import gmail_auth as g

fails = 0


def check(name, ok, detail=""):
    global fails
    fails += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name:<52} {detail if not ok else ''}")


AUTH = "user=owner@example.com\x01auth=Bearer TOK\x01\x01"

check("SASL string is byte-exact", g.xoauth2("owner@example.com", "TOK") == AUTH)

# IMAP, through imaplib's own authenticator: Gmail opens with an empty "+ " challenge.
a = imaplib._Authenticator(g._responder(AUTH.encode()))
check("IMAP: first challenge gets the auth string", base64.b64decode(a.process(b"")) == AUTH.encode())
err = base64.b64encode(json.dumps({"status": "400", "schemes": "Bearer"}).encode())
check("IMAP: error challenge gets an empty reply", a.process(err) == b"")

# SMTP: smtplib calls authobject() for the initial response, then authobject(challenge).
r = g._responder(AUTH)
check("SMTP: initial response is the auth string", r() == AUTH)
check("SMTP: error challenge gets an empty reply", r(b'{"status":"400"}') == "")


class FakeSMTP:
    def __init__(self):
        self.calls = []

    def ehlo_or_helo_if_needed(self):
        self.calls.append("ehlo")

    def auth(self, mech, obj, initial_response_ok=True):
        self.calls.append((mech, obj(), initial_response_ok))


s = FakeSMTP()
g.smtp_login(s, "owner@example.com", "TOK")
check("SMTP: EHLO before AUTH XOAUTH2", s.calls == ["ehlo", ("XOAUTH2", AUTH, True)], str(s.calls))

# Token refresh, with the endpoint stubbed.
SECRETS = {"GMAIL_OAUTH_CLIENT_ID": "cid", "GMAIL_OAUTH_CLIENT_SECRET": "csecret",
           "GMAIL_OAUTH_REFRESH_TOKEN": "rtoken"}
os.environ.update(SECRETS)
requests = []


def ok_urlopen(url, data, timeout):
    requests.append((url, data))
    return io.BytesIO(json.dumps({"access_token": f"AT{len(requests)}", "expires_in": 3599}).encode())


g.urllib.request.urlopen = ok_urlopen
g._TOKEN = None
first, second = g.token(), g.token()
check("one refresh per run, shared by every login", (first, second, len(requests)) == ("AT1", "AT1", 1),
      f"{first} {second} {len(requests)}")
sent = dict(p.split("=", 1) for p in requests[0][1].decode().split("&"))
check("refresh posts grant_type=refresh_token + the three secrets",
      requests[0][0] == g.TOKEN_URI and sent == {"client_id": "cid", "client_secret": "csecret",
                                                 "refresh_token": "rtoken",
                                                 "grant_type": "refresh_token"}, str(sent))


def bad_urlopen(url, data, timeout):
    body = io.BytesIO(b'{"error": "invalid_grant", "error_description": "Token has been expired or revoked."}')
    raise urllib.error.HTTPError(url, 400, "Bad Request", {}, body)


g.urllib.request.urlopen = bad_urlopen
g._TOKEN = None
try:
    g.token()
    msg = ""
except RuntimeError as e:
    msg = str(e)
check("failed refresh names the cause", "invalid_grant" in msg and "HTTP 400" in msg, msg)
check("failed refresh never echoes a secret", not any(v in msg for v in SECRETS.values()), msg)

check("configured() true with all three set", g.configured())
os.environ["GMAIL_OAUTH_REFRESH_TOKEN"] = ""
check("configured() false when one is blank", not g.configured())


# Local fallback (the dashboard build on the Mac): app password only when OAuth isn't set.
class FakeIMAP:
    def __init__(self):
        self.calls = []

    def login(self, user, pw):
        self.calls.append(("login", user, pw))

    def authenticate(self, mech, obj):
        self.calls.append(("authenticate", mech))


class FakeSMTP2(FakeSMTP):
    def login(self, user, pw):
        self.calls.append(("login", user, pw))


os.environ["GMAIL_APP_PASSWORD"] = "apppw"
check("configured() true with only an app password", g.configured())
m, s = FakeIMAP(), FakeSMTP2()
g.imap_login(m, "owner@example.com")
g.smtp_login(s, "owner@example.com")
check("no OAuth env: IMAP + SMTP use the app password",
      m.calls == [("login", "owner@example.com", "apppw")]
      and s.calls == [("login", "owner@example.com", "apppw")], f"{m.calls} {s.calls}")

os.environ["GMAIL_OAUTH_REFRESH_TOKEN"] = "rtoken"
g._TOKEN = "AT"
m, s = FakeIMAP(), FakeSMTP2()
g.imap_login(m, "owner@example.com")
g.smtp_login(s, "owner@example.com")
check("OAuth env set: OAuth wins even with an app password",
      m.calls == [("authenticate", "XOAUTH2")] and s.calls[-1][0] == "XOAUTH2", f"{m.calls} {s.calls}")

print()
if fails:
    print(f"{fails} FAILURE(S) — the Gmail OAuth sign-in is not behaving.")
    sys.exit(1)
print("All cases passed — Gmail OAuth sign-in builds the right handshake.")
