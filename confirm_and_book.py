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


def q(s):
    """Quote an IMAP search term. Required for anything with a space or '@' — an unquoted
    multi-word term is parsed as separate tokens and the server answers BAD."""
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


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
    typ, data = m.search(None, "SUBJECT", q(needle))
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
    typ, data = m.search(None, "FROM", q(USER), "SUBJECT", q(PREVIEW_SUBJECT))
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


# --- What actually landed -------------------------------------------------------------------
# Heavy imports stay INSIDE these functions on purpose: the module must import with stdlib only
# so test_confirm_parser.py can run in CI before config.py has been materialized from secrets.

def snapshot_bookable():
    """-> (properties, keys) of what is about to book, classified exactly as the preview was."""
    from preview_email import collect_preview
    props, _n = collect_preview()
    keys = {(p["label"], b["irow"]) for p in props for b in p["book"]}
    return props, keys


def promoted_keys():
    """-> {(label, import row)} that now carry promote's '✓ promoted' stamp.

    Comparing this against the pre-promote snapshot is what makes the receipt truthful: it
    reports what the ledger actually took, not what we intended to send it. promote can decline
    a row (e.g. it refuses to add onto an already non-zero named line), and that must show up.
    """
    import config
    from capture import open_sheet
    from promote import read_import
    out = set()
    for sheet_name in config.SHEETS:
        res = read_import(open_sheet(sheet_name))
        if not res:
            continue
        _ws, rows = res
        label = config.INBOX_PROPS.get(sheet_name, sheet_name)
        out |= {(label, r["irow"]) for r in rows if r["promoted"]}
    return out


def render_summary(props, booked, missed, output, ok):
    """-> (n_booked, text, html) receipt, styled like the preview so it reads the same way."""
    from preview_email import (FONT, INK, MUTED, FAINT, LINE, MONO,
                               amount_of, category_of, esc, rows_table)

    blocks, lines, n_booked, n_missed = [], [], 0, 0
    for p in props:
        took = [b for b in p["book"] if (p["label"], b["irow"]) in booked]
        left = [b for b in p["book"] if (p["label"], b["irow"]) in missed]
        if not took and not left:
            continue
        n_booked += len(took)
        n_missed += len(left)
        lines.append(p["label"])
        lines.append("-" * len(p["label"]))
        for b in took:
            amt, _ = amount_of(b)
            lines.append(f"  {b['date']:<10} {amt:>12}  {category_of(b):<26} {b['payee']}")
        if not took:
            lines.append("  (nothing booked)")
        if left:
            lines.append(f"  NOT booked ({len(left)}):")
            for b in left:
                amt, _ = amount_of(b)
                lines.append(f"    {b['date']:<10} {amt:>12}  {category_of(b):<26} {b['payee']}")
        lines.append("")

        inner = rows_table(took, show_num=False) if took else (
            f'<div style="font-size:14px;color:{MUTED};padding:4px 0 2px">Nothing booked.</div>')
        warn = ""
        if left:
            warn = (f'<div style="margin:10px 0 0;padding:10px 12px;background:#fff8f0;'
                    f'border:1px solid #f0c9a0;border-radius:6px">'
                    f'<div style="font-size:12px;font-weight:600;text-transform:uppercase;'
                    f'letter-spacing:.4px;color:#8a5a1a;margin-bottom:6px">'
                    f'Not booked ({len(left)})</div>{rows_table(left, show_num=False)}</div>')
        blocks.append(
            f'<h3 style="font-size:14px;font-weight:600;margin:24px 0 8px;color:{INK}">'
            f'{esc(p["label"])}</h3>{inner}{warn}')

    if ok:
        headline = (f'<strong>{n_booked}</strong> row{"" if n_booked == 1 else "s"} written to '
                    f'your registers.' if n_booked else "Nothing was pending — nothing booked.")
        head_text = (f"Booked {n_booked} row(s) into your registers."
                     if n_booked else "Nothing was pending — nothing booked.")
    else:
        headline = ("<strong>Booking failed.</strong> Nothing may have been written — "
                    "check the details below.")
        head_text = "BOOKING FAILED — nothing may have been written."
    if n_missed:
        headline += (f' <span style="color:#8a5a1a">{n_missed} row'
                     f'{"" if n_missed == 1 else "s"} did not book — see below.</span>')

    text = f"{head_text}\n\n" + "\n".join(lines) + f"\n\n--- promote output ---\n{output}\n"
    html = (
        f'<div style="font-family:{FONT};color:{INK};background:#ffffff;font-size:15px;'
        f'line-height:1.5;max-width:680px;margin:0 auto;padding:16px 18px 22px">'
        f'<h2 style="font-size:19px;font-weight:600;margin:0 0 6px">'
        f'{"Booked" if ok else "Booking failed"}</h2>'
        f'<p style="margin:0;color:{MUTED};font-size:14px">{headline}</p>'
        f'{"".join(blocks)}'
        f'<h3 style="font-size:12px;font-weight:600;letter-spacing:.4px;text-transform:uppercase;'
        f'color:{MUTED};margin:28px 0 8px;border-top:1px solid {LINE};padding-top:16px">'
        f'Run log</h3>'
        f'<pre style="font-family:{MONO};font-size:11.5px;line-height:1.45;color:{FAINT};'
        f'white-space:pre-wrap;margin:0">{esc(output)}</pre>'
        f'</div>')
    return n_booked, text, html


def send_summary(h, ok, text, html):
    msg = EmailMessage()
    msg["From"] = USER
    msg["To"] = USER
    msg["Subject"] = f"{BOOKED_SUBJECT} [{h}]" if ok else f"{ERROR_SUBJECT} [{h}]"
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
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
    before, keys = snapshot_bookable()      # must be captured BEFORE promote stamps anything
    code, output = run_promote()
    print(output)
    stamped = promoted_keys() if code == 0 else set()
    n_booked, text, html = render_summary(
        before, keys & stamped, keys - stamped, output, ok=(code == 0))
    # promote books every eligible staged row at once, so it runs once regardless of how many
    # previews were confirmed; each hash still gets a summary so each is marked handled.
    for h in pending:
        send_summary(h, code == 0, text, html)
        print(f"{'Booked' if code == 0 else 'ERROR'} [{h}] — {n_booked} row(s), summary emailed.")


if __name__ == "__main__":
    main()
