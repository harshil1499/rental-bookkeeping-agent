#!/usr/bin/env python3
"""
preview_email.py — stage the Import tabs and email a numbered preview of what `promote` would book.

Step 2 of the automation roadmap ("drop files -> get a preview"). Intended to run on a schedule
after documents land in the Drive "Relay Imports" inbox:

  1. Hash the current inbox file set. If a preview for that exact set is already in the mailbox,
     exit — no re-stage, no re-nag. (Held statements that sit in the inbox waiting on their CSV
     don't re-trigger; the hash only changes when a file is added or removed.)
  2. Otherwise stage the Import tabs (`import_relay.py --rebuild` — writes the scratch Import tab
     only, moves no files, preserves Skip/Promoted marks) and email a numbered, per-property
     preview of what `promote` would book, plus the reply-command legend. The file-set hash goes
     in the subject so the next run can tell it was already sent.

NO ledger write and NO file moves happen here. The human still gates booking — step 3 will parse
the `confirm` reply and run promote. Row numbering is global and deterministic (SHEETS order, then
Import-tab order) so a reply's row number maps back to a staged row.

Auth: Gmail app password in GMAIL_USER / GMAIL_APP_PASSWORD; Google service account via config.py.
Set PREVIEW_TO to override the recipient (defaults to the Gmail account, i.e. send-to-self).
"""
import hashlib
import imaplib
import os
import smtplib
import ssl
import subprocess
import sys
import warnings
from email.message import EmailMessage

warnings.filterwarnings("ignore")

import config
from promote import read_import, resolve
from capture import open_sheet
from import_relay import drive_service, load_drive_config, list_inbox_files

USER = os.environ["GMAIL_USER"]
PW = os.environ["GMAIL_APP_PASSWORD"]
TO = os.environ.get("PREVIEW_TO", USER)
SHEETS = config.SHEETS
HERE = os.path.dirname(os.path.abspath(__file__)) or "."
ACTION_BOOK = ("income", "accumulate", "variable")

LEGEND = [
    ("confirm", "book everything shown below"),
    ("confirm except <edits>", 'e.g. "confirm except 3 -> Repairs, skip 7"'),
    ("<n> -> <Category>", "recategorize row n"),
    ("<n> -> income", "book row n as income (e.g. a forfeited deposit)"),
    ("skip <n>", "drop row n from this batch"),
    ("<n> = <amount>", "fix row n's amount"),
    ("hold", "do nothing; keep this open"),
]


# ---------------- Idempotency (mailbox as state) ----------------

def inbox_fileset():
    svc = drive_service()
    cfg = load_drive_config()
    return sorted(f["name"] for f in list_inbox_files(svc, cfg["inbox_folder_id"]))


def fileset_hash(names):
    return hashlib.sha256("\n".join(names).encode()).hexdigest()[:10]


def already_previewed(h):
    """True if a preview email for this exact inbox set is already in the mailbox."""
    m = imaplib.IMAP4_SSL("imap.gmail.com")
    try:
        m.login(USER, PW)
        m.select("INBOX")
        typ, data = m.search(None, "SUBJECT", f"[{h}]")
        return typ == "OK" and bool(data and data[0].split())
    finally:
        try:
            m.logout()
        except Exception:
            pass


# ---------------- Stage + classify ----------------

def stage_import_tabs():
    """Run the tested staging path (writes Import tabs only; moves nothing). Returns True on
    success, False if there was nothing to stage (e.g. only unrecognized files)."""
    r = subprocess.run([sys.executable, "import_relay.py", "--rebuild"],
                       cwd=HERE, capture_output=True, text=True)
    print(r.stdout)
    if r.returncode != 0:
        print(f"(import_relay --rebuild exited {r.returncode}: {r.stderr.strip()[:200]})")
        return False
    return True


def collect_preview():
    """-> (properties, n_book). Classifies each staged Import row exactly as promote will."""
    properties, n = [], 0
    for sheet_name in SHEETS:
        res = read_import(open_sheet(sheet_name))
        if not res:
            continue
        _ws, rows = res
        book, held, attention = [], 0, 0
        for r in rows:
            action, _target = resolve(r)
            if action in ACTION_BOOK:
                n += 1
                book.append({"num": n, "action": action, **r})
            elif action == "ask":
                attention += 1
            else:
                held += 1
        if book or held or attention:
            properties.append({
                "label": config.INBOX_PROPS.get(sheet_name, sheet_name),
                "book": book, "held": held, "attention": attention,
            })
    return properties, n


# ---------------- Render + send ----------------

def render_text(properties, n_book, h):
    L = [f"Bookkeeping preview — {n_book} row(s) ready to book. "
         f"Nothing is booked until you reply 'confirm'.", ""]
    for p in properties:
        L.append(f"> {p['label']}")
        for b in p["book"]:
            sign = "+" if b["type"] == "Income" else "-"
            cat = "income" if b["action"] == "income" else (b["category"] or "-")
            L.append(f"   {b['num']:>3}. {b['date']:<10} {sign}{abs(b['amount']):>9.2f}  "
                     f"{cat:<26} {b['payee']}")
        if not p["book"]:
            L.append("     (nothing new to book)")
        flags = []
        if p["held"]:
            flags.append(f"{p['held']} held/duplicate (auto-skipped)")
        if p["attention"]:
            flags.append(f"{p['attention']} need attention")
        if flags:
            L.append(f"     [{', '.join(flags)}]")
        L.append("")
    L.append("Reply to book:")
    for cmd, desc in LEGEND:
        L.append(f"   {cmd:<24} {desc}")
    L.append("")
    L.append(f"(ref {h})")
    return "\n".join(L)


def send(subject, text):
    msg = EmailMessage()
    msg["From"] = USER
    msg["To"] = TO
    msg["Subject"] = subject
    msg.set_content(text)
    # Monospace HTML alternative so the numbered columns line up in most clients.
    esc = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    msg.add_alternative(
        f'<pre style="font-family:ui-monospace,Menlo,Consolas,monospace;font-size:13px;'
        f'line-height:1.45;color:#1f2328">{esc}</pre>', subtype="html")
    ctx = ssl.create_default_context()
    with smtplib.SMTP("smtp.gmail.com", 587) as s:
        s.starttls(context=ctx)
        s.login(USER, PW)
        s.send_message(msg)


def main():
    names = inbox_fileset()
    if not names:
        print("Inbox empty — nothing to preview.")
        return
    h = fileset_hash(names)
    if already_previewed(h):
        print(f"Preview for inbox set [{h}] already in mailbox — skipping.")
        return
    if not stage_import_tabs():
        print("Nothing staged — no preview sent.")
        return
    properties, n_book = collect_preview()
    if not properties:
        print("Nothing to preview after staging — no email sent.")
        return
    subject = f"Bookkeeping preview — {n_book} to book [{h}]"
    send(subject, render_text(properties, n_book, h))
    print(f"Preview sent to {TO} — {n_book} row(s) to book, set [{h}].")


if __name__ == "__main__":
    main()
