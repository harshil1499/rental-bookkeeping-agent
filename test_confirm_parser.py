#!/usr/bin/env python3
"""
test_confirm_parser.py — regression guard on the one function that decides whether money moves.

`confirm_and_book.intent()` reads an email reply and decides whether it authorizes booking. It
is the ONLY gate between an inbound email and `promote --write`, so a bug here books real dollar
figures without the owner meaning to.

The specific trap this guards: the preview email's own second line reads
"Nothing is booked until you reply 'confirm'". If intent() ever scanned the whole body instead of
stopping at the quoted original, an EMPTY reply (just hitting Send with the original quoted)
would look exactly like a confirmation and auto-book. Some clients quote without ">" markers, so
the cutoff has to recognize several quote styles.

Stdlib only, no pytest — runs anywhere, and runs in CI before the booking step so a regression
fails the workflow instead of booking something wrong.

    python3 test_confirm_parser.py
"""
import base64
import os
import sys
from email import message_from_bytes
from email.header import Header
from email.message import EmailMessage

# intent() is pure, but the module reads Gmail creds at import time.
os.environ.setdefault("GMAIL_USER", "test@example.com")
os.environ.setdefault("GMAIL_APP_PASSWORD", "unused")

from confirm_and_book import HASH_RE, header_text, intent, plain_body  # noqa: E402

# A realistic preview body — note line 2 contains the word "confirm".
PREVIEW = """Bookkeeping preview - 3 row(s) ready to book.
Nothing is booked until you reply 'confirm'.

Sailing Skies / Hawley (#6692)
    1. 7/5/2026      +1,980.10  income   AIRBNB PAYMENTS

Reply to book:
  confirm                  book everything shown below
  hold                     do nothing; keep this open

(ref 988322d1a9)"""


def quoted(body, marker=True):
    """The preview as an email client would quote it underneath a reply."""
    if marker:
        return "\n".join("> " + ln for ln in body.splitlines())
    return body            # some clients quote with no ">" prefix at all


CASES = [
    # (name, reply body, expected intent)
    # --- Unsupported edit syntax must NOT book. The preview advertised these commands for
    # weeks without anything parsing them, and `confirm` matching anywhere in the head meant
    # "confirm except skip 24" booked all 31 rows and dropped the edit silently. The legend no
    # longer offers them; these guard the reply typed from memory. Expected 'edit' = book
    # nothing, leave the preview open.
    ("confirm except, with skip",  "confirm except skip 24", "edit"),
    ("confirm except, recategorize", "confirm except 3 -> Repairs", "edit"),
    ("confirm except, unicode arrow", "confirm except 3 → Repairs", "edit"),
    ("confirm then amount edit",   "confirm, 4 = 120.50", "edit"),
    ("confirm plus skip on line 2", "confirm\nskip 7", "edit"),
    # ...while ordinary human confirms keep working. These must never read as edits.
    ("confirm with thanks",        "confirm, thanks", "confirm"),
    ("confirm all 31",             "confirm all 31 rows", "confirm"),
    ("bare confirm",              "confirm", "confirm"),
    ("capitalized",               "Confirm", "confirm"),
    ("with punctuation",          "confirm!", "confirm"),
    ("in a sentence",             "ok sounds good, confirm", "confirm"),
    ("confirm above gmail quote",
     "confirm\n\nOn Wed, Jul 23, 2026 at 9:00 AM Me <me@x.com> wrote:\n" + quoted(PREVIEW),
     "confirm"),
    ("confirm above Outlook quote",
     "confirm\n\n-----Original Message-----\nFrom: Me\n" + quoted(PREVIEW, marker=False),
     "confirm"),
    ("hold",                      "hold", "hold"),

    # --- the dangerous ones: nothing was typed, the original is just quoted back ---
    ("EMPTY reply, '>' quoted",
     "On Wed, Jul 23, 2026 at 9:00 AM Me <me@x.com> wrote:\n" + quoted(PREVIEW), None),
    ("EMPTY reply, quoted with NO markers", quoted(PREVIEW, marker=False), None),
    ("EMPTY reply, Outlook style",
     "-----Original Message-----\nFrom: Me\nSubject: Bookkeeping preview\n"
     + quoted(PREVIEW, marker=False), None),
    ("EMPTY reply, mobile signature first",
     "Sent from my iPhone\n\n" + quoted(PREVIEW, marker=False), None),

    # --- other non-authorizations ---
    ("declines",                  "not yet, let me look", None),
    ("confirm only inside quote", "let me check first\n\n> reply confirm to book", None),
    ("blank body",                "", None),
    ("whitespace only",           "\n\n   \n", None),
    ("mentions confirmation but doesn't confirm",
     "what does confirmation do again?", None),
]


# --- Subject decoding ------------------------------------------------------------------------
# intent() is only reached if the subject gates pass first, and those read a header straight off
# the wire. The preview subject contains an em dash, so senders RFC 2047-encode it — and clients
# disagree on how much: Python encodes the offending word, other composers encode the whole line.
# Read raw, the whole-line form has no leading "Re:" and no bracketed hash, so a real confirm is
# dropped before intent() ever sees it. That is exactly what happened on 2026-08-02: a valid
# reply sat in the inbox through four polls while every run reported "nothing to book".
SUBJECT = "Re: Bookkeeping preview — 31 to book [280faa2e21]"

SUBJECT_CASES = [
    ("plain ascii subject",
     "Re: Bookkeeping preview - 31 to book [280faa2e21]"),
    ("em dash encoded word-only (python)",
     "Re: Bookkeeping preview =?utf-8?b?4oCU?= 31 to book [280faa2e21]"),
    ("whole line q-encoded",
     str(Header(SUBJECT, "utf-8").encode())),
    ("whole line b-encoded",
     "=?utf-8?b?" + base64.b64encode(SUBJECT.encode()).decode() + "?="),
    ("folded across two lines",
     "=?utf-8?q?Re=3A_Bookkeeping_preview_=E2=80=94_31?=\n =?utf-8?q?_to_book_=5B280faa2e21=5D?="),
]


# --- Whole messages: which part of the reply intent() is handed -----------------------------
# iOS Mail answers an HTML preview with an HTML-ONLY reply (no text/plain part). Reading only
# text/plain turned a real "Confirm" into an empty body on 2026-10-02 and September sat unbooked.
# The opposite failure is worse: raw HTML is one long line, so a BLANK single-part HTML reply put
# the quoted preview's "reply confirm" on line one and booked. Both shapes are tested here.
PREVIEW_HTML = (
    '<div style="font-family:-apple-system,sans-serif"><h2>Bookkeeping preview</h2>'
    '<p><strong>29</strong> rows ready to book — nothing is booked until you reply '
    '<strong>confirm</strong>.</p><table><tr><td>1</td><td>9/1/2026</td><td>+1224.70</td></tr>'
    '</table></div>')


def apple(typed):
    """iOS Mail's reply HTML, verbatim structure from the 2026-10-02 reply."""
    return ('<html class="apple-mail-supports-explicit-dark-mode"><head><meta charset="utf-8">'
            f'</head><body dir="auto">{typed}<br id="lineBreakAtBeginningOfSignature">'
            '<div dir="ltr">Sent from my iPhone</div><div dir="ltr"><br><blockquote type="cite">'
            'On Oct 1, 2026, at 8:36 PM, me@x.com wrote:<br><br></blockquote></div>'
            f'<blockquote type="cite"><div dir="ltr">﻿{PREVIEW_HTML}</div></blockquote>'
            '</body></html>')


def gmail(typed):
    return (f'<div dir="ltr">{typed}</div><br><div class="gmail_quote gmail_quote_container">'
            '<div dir="ltr" class="gmail_attr">On Thu, Oct 1, 2026 at 8:36 PM &lt;me@x.com&gt; '
            f'wrote:<br></div><blockquote class="gmail_quote">{PREVIEW_HTML}</blockquote></div>')


def outlook(typed):
    return (f'<html><body><div>{typed}</div><div id="appendonsend"></div><hr>'
            '<div id="divRplyFwdMsg"><b>From:</b> me@x.com<br><b>Subject:</b> Bookkeeping preview'
            f'</div>{PREVIEW_HTML}</body></html>')


def bare(typed):
    """An unknown client that pastes the original with no quote wrapper at all."""
    return f'<html><body><div>{typed}</div>{PREVIEW_HTML}</body></html>'


def mime(html=None, plain=None, single=False):
    m = EmailMessage()
    if single:
        m.set_content(html, subtype="html")
        return m
    m.make_alternative()
    if plain is not None:
        m.add_alternative(plain)
    if html is not None:
        m.add_alternative(html, subtype="html")
    return m


STYLE_BAIT = '<html><head><style>.confirm{color:red}</style></head><body><br>'
MIME_CASES = [
    # (name, message, expected intent)
    ("iPhone confirm, html-only multipart", mime(apple("Confirm")), "confirm"),
    ("iPhone confirm, single-part html",    mime(apple("Confirm"), single=True), "confirm"),
    ("iPhone hold, html-only",              mime(apple("hold")), "hold"),
    ("iPhone edit syntax, html-only",       mime(apple("confirm except skip 4")), "edit"),
    ("Gmail confirm, html-only",            mime(gmail("confirm")), "confirm"),
    ("Outlook confirm, html-only",          mime(outlook("Confirm")), "confirm"),
    ("plain part wins over html",           mime(gmail("hold"), plain="confirm"), "confirm"),
    ("empty plain part falls back to html", mime(apple("Confirm"), plain="  \n"), "confirm"),
    ("confirm with &nbsp;",                 mime(apple("Confirm&nbsp;")), "confirm"),
    # --- the dangerous ones: nothing typed, the preview (which says "confirm") quoted below ---
    ("EMPTY iPhone reply, single-part html", mime(apple(""), single=True), None),
    ("EMPTY iPhone reply, html-only",       mime(apple("")), None),
    ("EMPTY Gmail reply, html-only",        mime(gmail("<br>")), None),
    ("EMPTY Outlook reply, html-only",      mime(outlook("<br>")), None),
    ("EMPTY reply, unwrapped quote",        mime(bare("<br>")), None),
    ("EMPTY reply, unwrapped, single-part", mime(bare(""), single=True), None),
    # Reply-with-selection quotes one line and no attribution, so only the structural cutoff
    # (blockquote / gmail_quote) stands between that line and intent().
    ("EMPTY reply quoting a selected line",
     mime('<html><body><br><blockquote type="cite">nothing is booked until you reply confirm'
          '</blockquote></body></html>'), None),
    ("EMPTY Gmail reply quoting a selection",
     mime('<div><br></div><div class="gmail_quote">reply confirm to book</div>'), None),
    ("'confirm' only in a <style> block",   mime(STYLE_BAIT + "</body></html>"), None),
    ("no body parts at all",                mime(), None),
]


def check_messages():
    failures = []
    for name, msg, expected in MIME_CASES:
        got = intent(plain_body(msg))
        ok = got == expected
        if not ok:
            failures.append(name)
        print(f"{'PASS' if ok else 'FAIL'}  {name:<40} -> {got!r} (expected {expected!r})")
    return failures


def check_subjects():
    """Every wire form of the same subject must clear both gates and yield the same hash."""
    failures = []
    for name, wire in SUBJECT_CASES:
        msg = message_from_bytes(f"Subject: {wire}\r\n\r\nconfirm\r\n".encode())
        text = header_text(msg, "Subject")
        is_reply = text.lower().lstrip().startswith("re:")
        found = HASH_RE.search(text)
        ok = is_reply and found and found.group(1) == "280faa2e21"
        if not ok:
            failures.append(name)
        detail = f"re:={is_reply} hash={found.group(1) if found else None}"
        print(f"{'PASS' if ok else 'FAIL'}  {name:<40} -> {detail}")

    # A missing header must not crash the scan, and must not look like a reply.
    empty = header_text(message_from_bytes(b"\r\n\r\nbody\r\n"), "Subject")
    ok = empty == "" and not empty.lower().startswith("re:")
    failures += [] if ok else ["absent subject header"]
    print(f"{'PASS' if ok else 'FAIL'}  {'absent subject header':<40} -> {empty!r}")
    return failures


def main():
    failures = []
    for name, body, expected in CASES:
        got = intent(body)
        ok = got == expected
        if not ok:
            failures.append(name)
        print(f"{'PASS' if ok else 'FAIL'}  {name:<40} -> {got!r} (expected {expected!r})")

    print()
    failures += check_messages()

    print()
    failures += check_subjects()

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S): {', '.join(failures)}")
        print("Refusing to treat the confirm gate as trustworthy — fix before booking.")
        return 1
    total = len(CASES) + len(MIME_CASES) + len(SUBJECT_CASES) + 1
    print(f"All {total} cases passed — confirm gate behaves.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
