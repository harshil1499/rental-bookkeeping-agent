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
import email_docs
import inbox_status
from promote import read_import, resolve
from capture import open_sheet
from import_relay import drive_service, load_drive_config, list_inbox_files

USER = os.environ["GMAIL_USER"]
PW = os.environ["GMAIL_APP_PASSWORD"]
TO = os.environ.get("PREVIEW_TO", USER)
# FORCE=1 re-sends even if this inbox set was already previewed (manual testing only —
# the scheduled runs must never set it, or every poll would email).
FORCE = os.environ.get("FORCE", "").strip().lower() in ("1", "true", "yes")
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

def inbox_fileset(svc, cfg):
    """Everything that has arrived by either path — a newly emailed document changes the
    hash exactly like a newly uploaded one, so it triggers a fresh preview."""
    names = {f["name"] for f in list_inbox_files(svc, cfg["inbox_folder_id"])}
    return sorted(names | set(email_docs.names()))


def inbox_url(cfg):
    """Deep link to the Drive inbox. Built from drive_config at run time — the folder ID is a
    secret and must never be hardcoded in this (public) repo."""
    fid = cfg.get("inbox_folder_id")
    return f"https://drive.google.com/drive/folders/{fid}" if fid else None


def fileset_hash(names):
    return hashlib.sha256("\n".join(names).encode()).hexdigest()[:10]


def already_previewed(h):
    """True if a preview email for this exact inbox set is already in the mailbox."""
    m = imaplib.IMAP4_SSL("imap.gmail.com")
    try:
        m.login(USER, PW)
        m.select("INBOX")
        typ, data = m.search(None, "SUBJECT", f'"[{h}]"')  # quoted: IMAP rejects some bare terms
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


def skip_bucket(reason):
    """Group promote's skip reasons into buckets that mean different things to a human.
    Lumping them into one 'skipped' number is misleading: 'already booked' needs no action,
    'held' means a file is missing, 'excluded' is by design (transfers, superseded drafts)."""
    r = (reason or "").lower()
    if r.startswith("already"):   # already promoted / already in register
        return "booked"
    if r.startswith("held"):      # awaiting its statement or CSV
        return "held"
    return "excluded"             # superseded draft / transfer / you marked Skip


def collect_preview():
    """-> (properties, n_book). Classifies each staged Import row exactly as promote will."""
    properties, n = [], 0
    for sheet_name in SHEETS:
        res = read_import(open_sheet(sheet_name))
        if not res:
            continue
        _ws, rows = res
        book = []
        counts = {"booked": 0, "held": 0, "excluded": 0, "attention": 0}
        for r in rows:
            action, target = resolve(r)
            if action in ACTION_BOOK:
                n += 1
                book.append({"num": n, "action": action, **r})
            elif action == "ask":
                counts["attention"] += 1
            else:
                counts[skip_bucket(target)] += 1
        if book or any(counts.values()):
            properties.append({
                "label": config.INBOX_PROPS.get(sheet_name, sheet_name),
                "book": book, "counts": counts,
            })
    return properties, n


def collect_needs(scan):
    """-> [(property label, [things still missing])] from the shared inbox scan, so the email
    answers 'what do I drop?' and not just 'what would book?'."""
    out = []
    by_prop, csv_months = scan["by_prop"], scan["csv_months"]
    for sheet, label in config.INBOX_PROPS.items():
        b = by_prop.get(sheet)
        if not b:
            continue
        items = []
        drafted = csv_months.get(sheet, set())
        held = {mon for mon, _amt, _name in b["statements"] if mon not in drafted}
        if held:
            # chronological, not alphabetical — "July, August", never "August, July"
            months = sorted(held, key=lambda m: inbox_status.MONTHS.index(m)
                            if m in inbox_status.MONTHS else 99)
            plural = "" if len(held) == 1 else "s"
            items.append(f"Relay CSV for {', '.join(months)} — {len(held)} mortgage "
                         f"statement{plural} waiting on it")
        if sheet == config.INBOX_APPFOLIO_SHEET and not b["appfolio"]:
            items.append("AppFolio owner-statement export")
        if items:
            out.append((label, items))
    return out


# ---------------- Render + send ----------------
# Email clients ignore <style> blocks and external CSS unpredictably, so every rule here is
# inlined. Tables use role="presentation" so screen readers treat them as layout, not data.

FONT = "-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
MONO = "ui-monospace,SFMono-Regular,Menlo,Consolas,monospace"
INK, MUTED, FAINT = "#1f2328", "#57606a", "#8b949e"
LINE, HAIR = "#d9dee3", "#eceff2"
GREEN = "#1a7f37"

TH = (f"padding:7px 10px;text-align:left;font-size:11px;font-weight:600;letter-spacing:.4px;"
      f"text-transform:uppercase;color:{MUTED};border-bottom:1px solid {LINE};")
TD = f"padding:8px 10px;border-bottom:1px solid {HAIR};vertical-align:top;"
TABLE = "width:100%;border-collapse:collapse;font-size:14px;"


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def amount_of(row):
    """-> (formatted amount, is_income). Register stores magnitudes; type carries direction."""
    inc = row["type"] == "Income"
    sign = "+" if inc else "-"
    return f"{sign}{abs(row['amount']):,.2f}", inc


def category_of(row):
    return "income" if row["action"] == "income" else (row["category"] or "-")


def flags_of(p):
    c = p["counts"]
    labels = [("booked", "already booked"), ("held", "held, waiting on a file"),
              ("excluded", "excluded"), ("attention", "need attention")]
    return [f"{c[k]} {text}" for k, text in labels if c[k]]


def render_text(properties, n_book, h, needs, url):
    """Plain-text fallback for clients that don't render HTML."""
    L = [f"Bookkeeping preview - {n_book} row(s) ready to book.",
         "Nothing is booked until you reply 'confirm'.", ""]
    for p in properties:
        L.append(p["label"])
        L.append("-" * len(p["label"]))
        if p["book"]:
            for b in p["book"]:
                amt, _ = amount_of(b)
                L.append(f"  {b['num']:>3}. {b['date']:<10} {amt:>12}  "
                         f"{category_of(b):<26} {b['payee']}")
        else:
            L.append("  (nothing new to book)")
        flags = flags_of(p)
        if flags:
            L.append(f"  {' | '.join(flags)}")
        L.append("")
    if needs:
        L.append("STILL NEEDED")
        for label, items in needs:
            L.append(f"  {label}")
            for it in items:
                L.append(f"    - {it}")
        if url:
            L.append(f"  Drop files in the Relay Imports inbox: {url}")
        L.append("")
    L.append("Reply to book:")
    for cmd, desc in LEGEND:
        L.append(f"  {cmd:<24} {desc}")
    L.append("")
    L.append(f"(ref {h})")
    return "\n".join(L)


def rows_table(book, show_num=True):
    """show_num=False for the post-booking receipt: the numbers exist so a reply can say
    "skip 7", which is meaningless once the rows are already written."""
    num_head = f'<th style="{TH}text-align:right;width:34px">#</th>' if show_num else ""
    head = (f'<tr>{num_head}'
            f'<th style="{TH}">Date</th>'
            f'<th style="{TH}text-align:right">Amount</th>'
            f'<th style="{TH}">Category</th>'
            f'<th style="{TH}">Payee</th></tr>')
    body = []
    for b in book:
        amt, inc = amount_of(b)
        colour = GREEN if inc else INK
        num_cell = (f'<td style="{TD}text-align:right;font-family:{MONO};font-size:12px;'
                    f'color:{FAINT}">{b["num"]}</td>') if show_num else ""
        body.append(
            f'<tr>{num_cell}'
            f'<td style="{TD}white-space:nowrap;color:{MUTED}">{esc(b["date"])}</td>'
            f'<td style="{TD}text-align:right;white-space:nowrap;font-family:{MONO};color:{colour}">{amt}</td>'
            f'<td style="{TD}">{esc(category_of(b))}</td>'
            f'<td style="{TD}">{esc(b["payee"])}</td>'
            f'</tr>')
    rows = head + "".join(body)
    return f'<table role="presentation" cellpadding="0" cellspacing="0" style="{TABLE}">{rows}</table>'


def legend_table():
    chip = (f"font-family:{MONO};font-size:12.5px;background:#f2f4f6;"
            f"border:1px solid {HAIR};border-radius:5px;padding:2px 6px;white-space:nowrap")
    rows = "".join(
        f'<tr><td style="{TD}white-space:nowrap"><code style="{chip}">{esc(cmd)}</code></td>'
        f'<td style="{TD}color:{MUTED}">{esc(desc)}</td></tr>'
        for cmd, desc in LEGEND)
    return f'<table role="presentation" cellpadding="0" cellspacing="0" style="{TABLE}">{rows}</table>'


def needs_block(needs, url):
    """The 'what do I actually drop?' section — the useful half when nothing is bookable."""
    if not needs and not url:
        return ""
    items = []
    for label, things in needs:
        lis = "".join(f'<li style="margin:2px 0">{esc(t)}</li>' for t in things)
        items.append(
            f'<div style="margin:0 0 10px">'
            f'<div style="font-size:13.5px;font-weight:600;color:{INK}">{esc(label)}</div>'
            f'<ul style="margin:4px 0 0;padding-left:20px;font-size:13.5px;color:{MUTED}">{lis}</ul>'
            f'</div>')
    link = (f'<a href="{esc(url)}" style="color:#0969da;text-decoration:underline;'
            f'font-size:13.5px">Open the Relay Imports inbox &rarr;</a>') if url else ""
    heading = ("Still needed" if needs else "Drive inbox")
    return (f'<div style="margin:24px 0 0;padding:14px 16px;background:#fbfcfd;'
            f'border:1px solid {LINE};border-radius:8px">'
            f'<div style="font-size:12px;font-weight:600;letter-spacing:.4px;'
            f'text-transform:uppercase;color:{MUTED};margin:0 0 10px">{heading}</div>'
            f'{"".join(items)}{link}</div>')


def render_html(properties, n_book, h, needs, url):
    blocks = []
    for p in properties:
        inner = rows_table(p["book"]) if p["book"] else (
            f'<div style="font-size:14px;color:{MUTED};padding:4px 0 2px">Nothing new to book.</div>')
        flags = flags_of(p)
        note = (f'<div style="font-size:12.5px;color:{FAINT};margin:6px 0 0">'
                f'{esc(" · ".join(flags))}</div>') if flags else ""
        blocks.append(
            f'<h3 style="font-size:14px;font-weight:600;margin:24px 0 8px;color:{INK}">'
            f'{esc(p["label"])}</h3>{inner}{note}')
    plural = "" if n_book == 1 else "s"
    headline = (f'<strong>{n_book}</strong> row{plural} ready to book' if n_book
                else "Nothing new to book yet")
    body = "".join(blocks) + needs_block(needs, url)
    # An explicit background is required, not cosmetic: without it, a dark-themed client
    # (Gmail dark mode) paints its own dark canvas behind this hardcoded dark text and the
    # headings/expense amounts become unreadable.
    return (
        f'<div style="font-family:{FONT};color:{INK};background:#ffffff;font-size:15px;'
        f'line-height:1.5;max-width:680px;margin:0 auto;padding:16px 18px 22px">'
        f'<h2 style="font-size:19px;font-weight:600;margin:0 0 6px">Bookkeeping preview</h2>'
        f'<p style="margin:0;color:{MUTED};font-size:14px">{headline} — nothing is booked '
        f'until you reply <strong style="color:{INK}">confirm</strong>.</p>'
        f'{body}'
        f'<h3 style="font-size:14px;font-weight:600;margin:28px 0 8px;'
        f'border-top:1px solid {LINE};padding-top:16px">Reply to book</h3>'
        f'{legend_table()}'
        f'<p style="margin:18px 0 0;font-size:11.5px;color:{FAINT}">ref {esc(h)}</p>'
        f'</div>')


def send(subject, text, html):
    msg = EmailMessage()
    msg["From"] = USER
    msg["To"] = TO
    msg["Subject"] = subject
    msg.set_content(text)                       # plain-text fallback
    msg.add_alternative(html, subtype="html")   # preferred rendering
    ctx = ssl.create_default_context()
    with smtplib.SMTP("smtp.gmail.com", 587) as s:
        s.starttls(context=ctx)
        s.login(USER, PW)
        s.send_message(msg)


def main():
    cfg = load_drive_config()
    svc = drive_service()
    names = inbox_fileset(svc, cfg)
    if not names:
        print("Inbox empty — nothing to preview.")
        return
    h = fileset_hash(names)
    if not FORCE and already_previewed(h):
        print(f"Preview for inbox set [{h}] already in mailbox — skipping.")
        return
    if not stage_import_tabs():
        print("Nothing staged — no preview sent.")
        return
    properties, n_book = collect_preview()
    if not properties:
        print("Nothing to preview after staging — no email sent.")
        return
    needs = collect_needs(inbox_status.scan(svc, cfg))
    url = inbox_url(cfg)
    subject = f"Bookkeeping preview — {n_book} to book [{h}]"
    send(subject,
         render_text(properties, n_book, h, needs, url),
         render_html(properties, n_book, h, needs, url))
    print(f"Preview sent to {TO} — {n_book} row(s) to book, "
          f"{len(needs)} propert{'y' if len(needs) == 1 else 'ies'} still needing files, set [{h}].")


if __name__ == "__main__":
    main()
