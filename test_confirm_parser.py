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
import os
import sys

# intent() is pure, but the module reads Gmail creds at import time.
os.environ.setdefault("GMAIL_USER", "test@example.com")
os.environ.setdefault("GMAIL_APP_PASSWORD", "unused")

from confirm_and_book import intent  # noqa: E402

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


def main():
    failures = []
    for name, body, expected in CASES:
        got = intent(body)
        ok = got == expected
        if not ok:
            failures.append(name)
        print(f"{'PASS' if ok else 'FAIL'}  {name:<40} -> {got!r} (expected {expected!r})")

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S): {', '.join(failures)}")
        print("Refusing to treat the confirm gate as trustworthy — fix before booking.")
        return 1
    print(f"All {len(CASES)} cases passed — confirm gate behaves.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
