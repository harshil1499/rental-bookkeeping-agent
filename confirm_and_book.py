#!/usr/bin/env python3
"""
confirm_and_book.py — book a staged batch after you reply "confirm" to a preview email.

Step 3 (v1) of the automation roadmap. This is what closes the laptop-free loop:
    reminder -> drop files in Drive -> preview email -> reply "confirm" -> booked.

Per run:
  1. Look in the mailbox for a REPLY from you to a "Bookkeeping preview ... [hash]" email whose
     first lines say `confirm`.
  2. Skip any hash already handled — mailbox-as-state idempotency, the same pattern as
     send_reminder.py / preview_email.py. A re-read can never double-book.
  3. Run `promote.py --write` and email a "Booked [hash]" summary (or "Bookkeeping ERROR [hash]").

Deliberate v1 limits, both load-bearing:
  - **Only the bare word `confirm` books.** The edit legend (`skip 7`, `3 -> Repairs`) is not
    parsed yet. To change something, edit the `Import` tab (the Sheets app works fine on a phone)
    and then reply `confirm`.
  - **This NEVER runs import.** That is exactly what makes the edit-the-Import-tab escape hatch
    safe: re-running import would regenerate the tab and wipe your edits. It also keeps the
    documented import-before-promote ordering intact.
  - **Files are not moved to Done/ after booking.** Harmless: re-staging is idempotent (preserved
    promoted stamps + amount reconciliation), so nothing double-books — the inbox just accumulates.

Security notes: only a reply From your own address, on a thread whose subject carries a known
preview hash, and containing the explicit word `confirm`, will book anything. That rests on the
From header plus knowledge of the hash — strong in practice for a personal mailbox, but not
cryptographic. Worth knowing, since this is the one job that writes dollar figures.

Auth: Gmail app password in GMAIL_USER / GMAIL_APP_PASSWORD; service account via config.py.
"""
import email as emaillib
import imaplib
import os
import re
import smtplib
import ssl
import subprocess
import sys
import warnings
from email.message import EmailMessage

warnings.filterwarnings("ignore")

USER = os.environ["GMAIL_USER"]
PW = os.environ["GMAIL_APP_PASSWORD"]
HERE = os.path.dirname(os.path.abspath(__file__)) or "."

PREVIEW_SUBJECT = "Bookkeeping preview"
BOOKED_SUBJECT = "Booked"
ERROR_SUBJECT = "Bookkeeping ERROR"
HASH_RE = re.compile(r"\[([0-9a-f]{6,16})\]")
# Where the quoted original begins. This is a SAFETY control, not cosmetics: the preview's own
# second line reads "Nothing is booked until you reply 'confirm'", so if a client quotes the
# original without ">" markers, a blank reply would otherwise look like a confirmation and
# auto-book. Stop at any marker that can begin the quoted message.
QUOTE_RE = re.compile(
    r"^\s*(>|on .+wrote:|-+\s*original message|from:\s|sent from |bookkeeping preview\b)", re.I)


def imap_connect():
    m = imaplib.IMAP4_SSL("imap.gmail.com")
    m.login(USER, PW)
    m.select("INBOX")
    return m


def logout(m):
    try:
        m.logout()
    except Exception:
        pass


def plain_body(msg):
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                try:
                    charset = part.get_content_charset() or "utf-8"
                    return part.get_payload(decode=True).decode(charset, "replace")
                except Exception:
                    continue
        return ""
    try:
        return msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", "replace")
    except Exception:
        return ""


def intent(body):
    """Read only the lines ABOVE the quoted original. The preview email itself contains the word
    'confirm' in its legend, so scanning the whole body would make every reply self-trigger."""
    head = []
    for line in body.splitlines():
        s = line.strip()
        if not s:
            continue
        if QUOTE_RE.match(s):
            break
        head.append(s)
        if len(head) >= 6:
            break
    text = " ".join(head).lower()
    if re.search(r"\bconfirm\b", text):
        return "confirm"
    if re.search(r"\bhold\b", text):
        return "hold"
    return None


def subjects_matching(m, needle):
    """-> set of hashes found in subjects containing `needle`."""
    out = set()
    typ, data = m.search(None, "SUBJECT", needle)
    if typ != "OK" or not data or not data[0]:
        return out
    for num in data[0].split():
        typ, raw = m.fetch(num, "(BODY[HEADER.FIELDS (SUBJECT)])")
        if typ != "OK" or not raw or not raw[0]:
            continue
        found = HASH_RE.search(raw[0][1].decode("utf-8", "replace"))
        if found:
            out.add(found.group(1))
    return out


def handled_hashes(m):
    """Hashes already booked or already alerted on — never processed twice."""
    return subjects_matching(m, BOOKED_SUBJECT) | subjects_matching(m, ERROR_SUBJECT)


def pending_confirmations(m):
    """-> hashes the owner replied `confirm` to that haven't been handled yet."""
    typ, data = m.search(None, "FROM", USER, "SUBJECT", PREVIEW_SUBJECT)
    if typ != "OK" or not data or not data[0]:
        return []
    handled, found = handled_hashes(m), []
    for num in data[0].split():
        typ, raw = m.fetch(num, "(RFC822)")
        if typ != "OK" or not raw or not raw[0]:
            continue
        msg = emaillib.message_from_bytes(raw[0][1])
        subject = str(msg.get("Subject", ""))
        if not subject.lower().lstrip().startswith("re:"):
            continue                                  # the original preview, not a reply
        if USER.lower() not in str(msg.get("From", "")).lower():
            continue                                  # only act on mail from the owner
        h = HASH_RE.search(subject)
        if not h:
            continue
        h = h.group(1)
        if h in handled or h in found:
            continue
        if intent(plain_body(msg)) == "confirm":
            found.append(h)
    return found


def run_promote():
    """Promote only — never import (see the module docstring)."""
    r = subprocess.run([sys.executable, "promote.py", "--write"],
                       cwd=HERE, capture_output=True, text=True)
    return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()


def send_summary(h, ok, output):
    subject = f"{BOOKED_SUBJECT} [{h}]" if ok else f"{ERROR_SUBJECT} [{h}]"
    lead = ("Booked. Here's what landed in the registers:" if ok else
            "Booking FAILED — nothing may have been written. Details below; "
            "re-run the workflow manually once it's fixed.")
    msg = EmailMessage()
    msg["From"] = USER
    msg["To"] = USER
    msg["Subject"] = subject
    msg.set_content(f"{lead}\n\n{output}\n")
    ctx = ssl.create_default_context()
    with smtplib.SMTP("smtp.gmail.com", 587) as s:
        s.starttls(context=ctx)
        s.login(USER, PW)
        s.send_message(msg)


def main():
    m = imap_connect()
    try:
        pending = pending_confirmations(m)
    finally:
        logout(m)

    if not pending:
        print("No new 'confirm' replies — nothing to book.")
        return

    print(f"Confirm received for: {', '.join(pending)} — running promote --write ...")
    code, output = run_promote()
    print(output)
    # promote books every eligible staged row at once, so it runs once regardless of how many
    # previews were confirmed; each hash still gets a summary so each is marked handled.
    for h in pending:
        send_summary(h, code == 0, output)
        print(f"{'Booked' if code == 0 else 'ERROR'} [{h}] — summary emailed.")


if __name__ == "__main__":
    main()
