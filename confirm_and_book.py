#!/usr/bin/env python3
"""
confirm_and_book.py — book a staged batch after you reply "confirm" to a preview email.

Step 3 (v1) of the automation roadmap. This is what closes the laptop-free loop:
    reminder -> drop files in Drive -> preview email -> reply "confirm" -> booked.

Per run:
  1. Look in the mailbox for a REPLY from you to a "Bookkeeping preview ... [hash]" email (or to a
     "Didn't book [hash]" notice) whose first lines say `confirm`.
  2. Skip any hash already handled — mailbox-as-state idempotency, the same pattern as
     send_reminder.py / preview_email.py. A re-read can never double-book.
  3. Run `promote.py --write` and email a "Booked [hash]" summary (or "Bookkeeping ERROR [hash]").

`--notices` (a separate workflow step, after booking): answer each reply of yours that did NOT
book — no confirm word, unsupported edit syntax, or a confirm on a batch already booked — with a
"Didn't book [hash]" email saying what was read and what to do. Never books. `--dry-run` prints
what booking and notices would do, and does neither.

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
import hashlib
import imaplib
import os
import re
import smtplib
import ssl
import subprocess
import sys
import warnings
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

warnings.filterwarnings("ignore")

# Stdlib-only itself, so it may be imported at module level — the parser tests run in CI
# before config.py exists, and this module has to stay importable without it.
import mailbox_state

USER = os.environ["GMAIL_USER"]
PW = os.environ["GMAIL_APP_PASSWORD"]
HERE = os.path.dirname(os.path.abspath(__file__)) or "."

PREVIEW_SUBJECT = "Bookkeeping preview"
BOOKED_SUBJECT = "Booked"
ERROR_SUBJECT = "Bookkeeping ERROR"
NOTICE_SUBJECT = "Didn't book"          # wording is load-bearing — see notice_subject()
NOTICE_WINDOW = timedelta(days=7)
HASH_RE = re.compile(r"\[([0-9a-f]{6,16})\]")
REF_RE = re.compile(r"\bref ([0-9a-f]{8})\b")
# Where the quoted original begins. This is a SAFETY control, not cosmetics: the preview's own
# second line reads "Nothing is booked until you reply 'confirm'", so if a client quotes the
# original without ">" markers, a blank reply would otherwise look like a confirmation and
# auto-book. Stop at any marker that can begin the quoted message — including the heading of a
# "Didn't book" notice, which also says "reply confirm" and can also be replied to.
QUOTE_RE = re.compile(
    r"^\s*(>|on .+wrote:|-+\s*original message|from:\s|sent from |bookkeeping preview\b"
    r"|didn.t book\b)", re.I)


def q(s):
    """Quote an IMAP search term. Required for anything with a space or '@' — an unquoted
    multi-word term is parsed as separate tokens and the server answers BAD."""
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def header_text(msg, name):
    """-> a header decoded the way a human reads it, not the way it arrived on the wire.

    `message_from_bytes` parses with the compat32 policy, which hands back headers verbatim.
    One non-ASCII character anywhere makes the sender RFC 2047-encode the line, and clients
    disagree about how much of it to encode: Python encodes just the offending word, other
    composers encode the whole line as a single blob. The preview subject carries an em dash
    ("Bookkeeping preview — N to book [hash]"), so encoding is the NORMAL case here.

    Undecoded, the whole-line shape becomes
        =?utf-8?q?Re=3A_Bookkeeping_preview_..._=5B280faa2e21=5D?=
    which has no leading "Re:" and no bracketed hash — so a perfectly good confirm gets
    dropped by the subject gates before intent() is ever consulted, silently.
    """
    raw = msg.get(name)
    if raw is None:
        return ""
    try:
        return str(make_header(decode_header(str(raw))))
    except Exception:
        return str(raw)          # undecodable: fall back to the literal header, never crash


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


class _ReplyText(HTMLParser):
    """HTML reply -> the text the sender typed, one line per block, stopping at the quoted original.

    Stopping is a SAFETY control for the same reason as QUOTE_RE: the quoted preview contains the
    word 'confirm'. Cut at every structural quote marker the common clients emit, and let QUOTE_RE
    and the six-line limit in intent() catch whatever slips past (e.g. the preview's own
    "Bookkeeping preview" heading)."""
    BREAKS = {"br", "p", "div", "li", "tr", "table", "h1", "h2", "h3", "h4", "h5", "h6"}
    HIDDEN = {"head", "style", "script", "title"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.hidden, self.done = [], 0, False

    @staticmethod
    def _is_quote(tag, attrs):
        a = dict(attrs)
        cls, ident = (a.get("class") or ""), (a.get("id") or "")
        return (tag in ("blockquote", "hr")                     # Apple Mail, Thunderbird, Outlook rule
                or "gmail_quote" in cls or "yahoo_quoted" in cls
                or ident in ("divRplyFwdMsg", "appendonsend"))  # Outlook

    def handle_starttag(self, tag, attrs):
        if self.done:
            return
        if self._is_quote(tag, attrs):
            self.done = True
            return
        if tag in self.HIDDEN:
            self.hidden += 1
        if tag in self.BREAKS:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in self.HIDDEN and self.hidden:
            self.hidden -= 1
        if tag in self.BREAKS:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.done and not self.hidden:
            self.out.append(data.replace("﻿", "").replace("\xa0", " "))


def html_reply_text(html):
    p = _ReplyText()
    try:
        p.feed(html)
        p.close()
    except Exception:
        return ""          # unparseable: book nothing
    return "".join(p.out)


def _decoded(part):
    try:
        return part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
    except Exception:
        return ""


def plain_body(msg):
    """-> the reply as plain text, for intent().

    Prefers text/plain. iOS Mail sends replies to an HTML message as HTML ONLY, with no
    text/plain part; reading only text/plain dropped a real iPhone confirm on 2026-10-02. So fall
    back to the HTML part, converted to lines and cut at the quoted original. Raw HTML must never
    reach intent(): it arrives as one long line, so the quoted preview's own "reply confirm" sits
    on the first line and a BLANK reply would book."""
    parts = list(msg.walk()) if msg.is_multipart() else [msg]
    for part in parts:
        if part.get_content_type() == "text/plain":
            text = _decoded(part)
            if text.strip():
                return text
    for part in parts:
        if part.get_content_type() == "text/html":
            return html_reply_text(_decoded(part))
    return ""


# Edit syntax that is NOT implemented: "confirm except 3 -> Repairs", "skip 7", "4 = 120.50".
# The preview used to advertise these, and `confirm` matching anywhere in the head meant such a
# reply booked the ENTIRE batch and discarded the edit silently. The legend no longer offers
# them, but a reply typed from memory must not book the wrong thing — so a confirm carrying edit
# syntax books NOTHING and leaves the preview open. Fails closed, which is the only acceptable
# direction for the one path that writes dollars.
EDIT_SYNTAX_RE = re.compile(r"\bexcept\b|\bskip\s+\d|\d\s*(->|→|=)\s*\S")


def reply_head(body):
    """-> the lines typed ABOVE the quoted original (at most six). The preview email itself
    contains the word 'confirm' in its legend, so scanning the whole body would make every reply
    self-trigger."""
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
    return head


def intent(body):
    """-> 'confirm' | 'edit' | 'hold' | None, read from reply_head() only."""
    text = " ".join(reply_head(body)).lower()
    if re.search(r"\bconfirm\b", text):
        if EDIT_SYNTAX_RE.search(text):
            return "edit"          # asked for something we cannot do — book nothing
        return "confirm"
    if re.search(r"\bhold\b", text):
        return "hold"
    return None


def sent_at(msg):
    """-> the message's Date header as an aware datetime, or None if absent/unparseable."""
    try:
        d = parsedate_to_datetime(header_text(msg, "Date"))
    except Exception:
        return None
    return d if d is not None and d.tzinfo is not None else None


def receipts(m, needle):
    """-> {hash: earliest Date (or None)} for subjects containing `needle`, anywhere in the account.

    This answers "did I already book this?", so it has to survive you tidying your inbox —
    a receipt you archived or deleted still means the batch was booked. Searching INBOX
    alone would let a deleted receipt re-open a batch that already hit the ledger.
    """
    out = {}
    for raw in mailbox_state.fetch(m, ("SUBJECT", q(needle)),
                                   "(BODY[HEADER.FIELDS (SUBJECT DATE)])"):
        head = emaillib.message_from_bytes(raw if isinstance(raw, bytes) else bytes(raw))
        found = HASH_RE.search(header_text(head, "Subject"))
        if not found:
            continue
        h, when = found.group(1), sent_at(head)
        if h not in out or (when and (out[h] is None or when < out[h])):
            out[h] = when
    return out


def notified_refs(m):
    """-> refs of replies already answered by a notice. A state lookup like receipts(), so an
    archived or deleted notice still counts and a reply is never answered twice."""
    out = set()
    for raw in mailbox_state.fetch(m, ("FROM", q(USER), "SUBJECT", q(NOTICE_SUBJECT)),
                                   "(BODY[HEADER.FIELDS (SUBJECT)])"):
        head = emaillib.message_from_bytes(raw if isinstance(raw, bytes) else bytes(raw))
        found = REF_RE.search(header_text(head, "Subject"))
        if found:
            out.add(found.group(1))
    return out


def reply_ref(msg):
    """-> 8-hex id for one reply, stable across runs (keyed on Message-ID)."""
    key = header_text(msg, "Message-ID") or (header_text(msg, "Date") + header_text(msg, "Subject"))
    return hashlib.sha1(key.strip().encode()).hexdigest()[:8]


def has_files(msg):
    return any(part.get_filename() for part in msg.walk() if not part.is_multipart())


def scan(m):
    """-> dict: confirmed hashes plus everything the notice rules need.

    The confirm read is INBOX-only, deliberately, while the state lookups search everywhere.
    It is the one read that leads to a ledger write, so it stays as narrow as possible:
    deleting a preview (or a notice) withdraws it. The asymmetry is the safe direction —
    forgetting a receipt could double-book, forgetting a preview just means nothing happens.

    A reply counts on a preview thread OR on a "Didn't book" notice thread: the notice tells you
    to reply `confirm` to it, and both carry the batch hash in the subject. Every other gate is
    identical for the two.
    """
    # State lookups FIRST, because they select other folders and come back. IMAP sequence
    # numbers are scoped to a SELECT, so searching INBOX and then re-selecting it mid-loop
    # could renumber the results underneath us if mail arrived in between — and this loop
    # ends in a ledger write. Gather the cross-folder state, then search INBOX and use it.
    booked_at = receipts(m, BOOKED_SUBJECT)
    handled = set(booked_at) | set(receipts(m, ERROR_SUBJECT))
    notified = notified_refs(m)
    out = {"found": [], "replies": [], "handled": handled, "booked_at": booked_at,
           "notified": notified}
    typ, data = m.search(None, "FROM", q(USER),
                         "OR", "SUBJECT", q(PREVIEW_SUBJECT), "SUBJECT", q(NOTICE_SUBJECT))
    if typ != "OK" or not data or not data[0]:
        print("  scan: no messages matched the preview or notice subject in INBOX.")
        return out
    found = out["found"]
    # Why count: every `continue` below is a silent drop, and the caller's only output was
    # "No new 'confirm' replies" — which reads identically whether there were no replies or
    # four were found and thrown away. That ambiguity cost a two-day-late close on 2026-08-02.
    seen = {"unfetchable": 0, "not a reply": 0, "not from owner": 0, "no hash": 0,
            "already handled": 0, "no confirm word": 0,
            "used unsupported edit syntax — nothing booked": 0}
    for num in data[0].split():
        typ, raw = m.fetch(num, "(RFC822)")
        if typ != "OK" or not raw or not raw[0]:
            seen["unfetchable"] += 1
            continue
        msg = emaillib.message_from_bytes(raw[0][1])
        subject = header_text(msg, "Subject")         # decoded — see header_text()
        if not subject.lower().lstrip().startswith("re:"):
            seen["not a reply"] += 1                  # a preview or notice itself, not a reply
            continue
        if USER.lower() not in header_text(msg, "From").lower():
            seen["not from owner"] += 1               # only act on mail from the owner
            continue
        h = HASH_RE.search(subject)
        if not h:
            seen["no hash"] += 1
            continue
        h = h.group(1)
        body = plain_body(msg)
        what = intent(body)
        out["replies"].append({"hash": h, "intent": what, "said": " / ".join(reply_head(body)),
                               "ref": reply_ref(msg), "sent": sent_at(msg),
                               "files": has_files(msg)})
        if h in handled or h in found:
            seen["already handled"] += 1
            continue
        if what == "edit":
            seen["used unsupported edit syntax — nothing booked"] += 1
            continue
        if what != "confirm":
            seen["no confirm word"] += 1
            continue
        found.append(h)
    dropped = ", ".join(f"{n} {why}" for why, n in seen.items() if n)
    print(f"  scan: {len(data[0].split())} message(s) on preview/notice threads; "
          f"{len(found)} confirmed{f'; skipped {dropped}' if dropped else ''}.")
    return out


# --- "Didn't book" notices -------------------------------------------------------------------
# A reply that doesn't book used to produce nothing but a line in a CI log, and that silence cost
# a two-day-late close in August and a stuck September in October. A notice goes out ONLY when a
# reply of yours didn't do what it looked like it was trying to do — never as reassurance (see
# the no-actionless-notifications rule). Runs as its own workflow step (`--notices`) after
# booking, so a notice failure can never be mistaken for a ledger failure.

def notice_for(r, *, handled, booked_at, notified, confirmed, now):
    """-> 'unread' | 'edit' | 'already' | None for one reply record from scan(). Pure."""
    if r["files"]:
        return None      # attaching files to a reply is how files are sent; the next preview covers it
    if r["sent"] is None or now - r["sent"] > NOTICE_WINDOW:
        return None      # stale mail: no burst of notices on deploy or on old threads
    if r["ref"] in notified:
        return None      # one notice per reply, ever
    h, what = r["hash"], r["intent"]
    if h in handled:
        booked = booked_at.get(h)
        if booked and what in ("confirm", "edit") and r["sent"] > booked:
            return "already"   # asked for something on a batch that had already landed
        return None      # incl. the reply that caused the booking, and ERROR-handled batches
    if h in confirmed:
        return None      # a confirm for this batch is waiting; the next booking run takes it
    if what is None:
        return "unread"
    if what == "edit":
        return "edit"
    return None          # 'hold' was meant


def due_notices(s, now):
    """-> [(reply, kind)] to send, from a scan() result."""
    out, refs = [], set()
    confirmed = {r["hash"] for r in s["replies"] if r["intent"] == "confirm"} - s["handled"]
    for r in s["replies"]:
        kind = notice_for(r, handled=s["handled"], booked_at=s["booked_at"],
                          notified=s["notified"], confirmed=confirmed, now=now)
        if kind and r["ref"] not in refs:
            refs.add(r["ref"])
            out.append((r, kind))
    return out


def notice_subject(h, ref):
    """Every word is load-bearing. IMAP SUBJECT search is a case-insensitive SUBSTRING match, and
    other lookups key off subjects:
      - no "booked": receipts() searches "Booked", so "Not booked [h]" would mark the batch
        handled and silently swallow the very confirm the notice asks for;
      - no "Bookkeeping preview": the confirm scan would read the notice itself as a preview
        (email_docs reads attachments on notice threads ON PURPOSE, via its own marker);
      - no "bookkeeping": send_reminder dedupes on "bookkeeping" + the month;
      - ASCII only, so the ref lookup never depends on how a server decodes RFC 2047.
    `[h]` is what makes a `confirm` reply to the notice book that batch."""
    return f"{NOTICE_SUBJECT} [{h}] - ref {ref}"


def _when(dt):
    try:
        from zoneinfo import ZoneInfo
        return dt.astimezone(ZoneInfo("America/New_York")).strftime("%b %-d at %-I:%M %p ET")
    except Exception:
        return dt.astimezone(timezone.utc).strftime("%b %d %H:%M UTC")


def render_notice(r, kind, booked_at):
    """-> (subject, text, html). Stdlib only; the first line of both bodies is the heading,
    which QUOTE_RE treats as the start of a quote, so a blank reply to a notice books nothing."""
    import html as htmllib
    said = r["said"] if len(r["said"]) <= 140 else r["said"][:137] + "..."
    if kind == "already":
        lead = (f"That batch was already booked on {_when(booked_at[r['hash']])}, before your "
                f"reply (“{said}”). Nothing new was booked.")
        act = "To change a booked row, edit it in the month tab by hand."
    elif kind == "edit":
        lead = (f"Your reply asked for a change (“{said}”). Changes in replies aren't "
                f"supported, so nothing was booked.")
        act = "Make the change in the sheet's Import tab, then reply confirm to this email."
    elif said:
        lead = (f"Your reply didn't book anything. I read it as “{said}”, and only the "
                f"word confirm books.")
        act = "To book this batch, reply confirm to this email."
    else:
        lead = ("Your reply came through empty: I couldn't read any text above the quoted "
                "preview, so nothing was booked.")
        act = "To book this batch, reply confirm to this email."
    foot = f"ref {r['ref']} · batch [{r['hash']}]"
    text = f"{NOTICE_SUBJECT}\n\n{lead}\n\n{act}\n\n{foot}\n"
    e = htmllib.escape
    html = (
        "<div style=\"font-family:-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;"
        "color:#1f2328;background:#ffffff;font-size:15px;line-height:1.5;max-width:680px;"
        "margin:0 auto;padding:16px 18px 22px\">"
        f'<h2 style="font-size:19px;font-weight:600;margin:0 0 6px">{e(NOTICE_SUBJECT)}</h2>'
        f'<p style="margin:0 0 12px">{e(lead)}</p>'
        f'<p style="margin:0 0 16px;font-weight:600">{e(act)}</p>'
        f'<p style="margin:0;color:#8b949e;font-size:12px">{e(foot)}</p></div>')
    return notice_subject(r["hash"], r["ref"]), text, html


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


def receipt_needs():
    """-> (needs, inbox url), or (None, None) if the check fails. The receipt is what marks a
    batch handled, so nothing about "still needed" may ever stop it from sending."""
    try:
        import still_needed
        from import_relay import drive_service, load_drive_config
        from preview_email import inbox_url
        cfg = load_drive_config()
        needs, _months = still_needed.collect(drive_service(), cfg)
        return needs, inbox_url(cfg)
    except Exception as e:
        print(f"  ! Couldn't check what's still needed ({str(e)[:120]}) — receipt goes without it.")
        return None, None


def render_summary(props, booked, missed, output, ok, needs=None, url=None):
    """-> (n_booked, text, html) receipt, styled like the preview so it reads the same way.
    `needs` is still_needed's list ([] = nothing missing, None = the check failed)."""
    from preview_email import (FONT, INK, MUTED, FAINT, LINE, MONO,
                               amount_of, category_of, esc, needs_block, rows_table)

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

    if needs:
        lines.append("Still needed (these months aren't fully booked until they arrive):")
        for label, items in needs:
            lines.append(f"  {label}")
            lines += [f"    - {t}" for t in items]
        lines.append("")
    elif needs is None:
        lines += ["(Couldn't check what's still needed this time.)", ""]
    needs_html = needs_block(needs, url) if needs else (
        f'<p style="margin:16px 0 0;font-size:12.5px;color:{FAINT}">Couldn\'t check what\'s '
        f'still needed this time.</p>' if needs is None else "")

    text = f"{head_text}\n\n" + "\n".join(lines) + f"\n\n--- promote output ---\n{output}\n"
    html = (
        f'<div style="font-family:{FONT};color:{INK};background:#ffffff;font-size:15px;'
        f'line-height:1.5;max-width:680px;margin:0 auto;padding:16px 18px 22px">'
        f'<h2 style="font-size:19px;font-weight:600;margin:0 0 6px">'
        f'{"Booked" if ok else "Booking failed"}</h2>'
        f'<p style="margin:0;color:{MUTED};font-size:14px">{headline}</p>'
        f'{"".join(blocks)}{needs_html}'
        f'<h3 style="font-size:12px;font-weight:600;letter-spacing:.4px;text-transform:uppercase;'
        f'color:{MUTED};margin:28px 0 8px;border-top:1px solid {LINE};padding-top:16px">'
        f'Run log</h3>'
        f'<pre style="font-family:{MONO};font-size:11.5px;line-height:1.45;color:{FAINT};'
        f'white-space:pre-wrap;margin:0">{esc(output)}</pre>'
        f'</div>')
    return n_booked, text, html


def send(subject, text, html):
    msg = EmailMessage()
    msg["From"] = USER
    msg["To"] = USER
    msg["Subject"] = subject
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    ctx = ssl.create_default_context()
    with smtplib.SMTP("smtp.gmail.com", 587) as s:
        s.starttls(context=ctx)
        s.login(USER, PW)
        s.send_message(msg)


def send_summary(h, ok, text, html):
    send(f"{BOOKED_SUBJECT} [{h}]" if ok else f"{ERROR_SUBJECT} [{h}]", text, html)


def send_notices(s, dry_run):
    due = due_notices(s, datetime.now(timezone.utc))
    if not due:
        print("No replies need a notice.")
        return
    for r, kind in due:
        subject, text, html = render_notice(r, kind, s["booked_at"])
        if dry_run:
            print(f"  would send: {subject}  ({kind}; read as {r['said']!r})")
            continue
        send(subject, text, html)
        print(f"Notice sent: {subject}  ({kind})")


def main(argv):
    """Default: book confirmed batches (the workflow's ledger step). `--notices`: answer replies
    that didn't book; never books. `--dry-run`: print what both would do; sends and books nothing."""
    unknown = set(argv) - {"--dry-run", "--notices"}
    if unknown:     # a typo'd flag must not fall through to the mode that writes the ledger
        sys.exit(f"Unknown argument(s): {' '.join(sorted(unknown))}")
    dry_run, notices = "--dry-run" in argv, "--notices" in argv
    m = imap_connect()
    try:
        s = scan(m)
    finally:
        logout(m)
    pending = s["found"]

    if dry_run:
        print(f"Would book: {', '.join(pending) or 'nothing'}")
        send_notices(s, dry_run=True)
        return
    if notices:
        send_notices(s, dry_run=False)
        return

    if not pending:
        print("No new 'confirm' replies — nothing to book.")
        return

    print(f"Confirm received for: {', '.join(pending)} — running promote --write ...")
    before, keys = snapshot_bookable()      # must be captured BEFORE promote stamps anything
    code, output = run_promote()
    print(output)
    stamped = promoted_keys() if code == 0 else set()
    needs, url = receipt_needs()
    n_booked, text, html = render_summary(
        before, keys & stamped, keys - stamped, output, ok=(code == 0), needs=needs, url=url)
    # promote books every eligible staged row at once, so it runs once regardless of how many
    # previews were confirmed; each hash still gets a summary so each is marked handled.
    for h in pending:
        send_summary(h, code == 0, text, html)
        print(f"{'Booked' if code == 0 else 'ERROR'} [{h}] — {n_booked} row(s), summary emailed.")


if __name__ == "__main__":
    main(sys.argv[1:])
