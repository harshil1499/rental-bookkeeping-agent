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

# intent() is pure, but the module reads Gmail creds at import time.
os.environ.setdefault("GMAIL_USER", "test@example.com")
os.environ.setdefault("GMAIL_APP_PASSWORD", "unused")

from confirm_and_book import HASH_RE, header_text, intent  # noqa: E402

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
    failures += check_subjects()

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S): {', '.join(failures)}")
        print("Refusing to treat the confirm gate as trustworthy — fix before booking.")
        return 1
    print(f"All {len(CASES) + len(SUBJECT_CASES) + 1} cases passed — confirm gate behaves.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
