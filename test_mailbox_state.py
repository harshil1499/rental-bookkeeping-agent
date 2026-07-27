#!/usr/bin/env python3
"""
test_mailbox_state.py — regression guard on where idempotency looks for its own past mail.

`mailbox_state._folders()` turns a server's LIST response into the set of folders every
"have I already done this?" check searches. Get it wrong and the whole mailbox-as-state design
silently breaks: too narrow and a re-send fires (the 2026-07-23 phantom preview); too narrow in
the other direction (Trash only, no \\All) and it misses mail still sitting in the inbox.

The folder names cannot be hardcoded — Gmail localizes them and encodes them in modified UTF-7,
so the server's own spelling is the only reliable one. These cases pin the parser against the
real LIST shapes it has to survive: standard Gmail, a localized account, a server with no
RFC 6154 special-use flags, and imaplib's tuple form for literal-encoded names.

Stdlib only, no pytest — runs anywhere, and runs in CI so a regression fails the workflow.

    python3 test_mailbox_state.py
"""
import sys

import mailbox_state

Q = chr(34)  # a literal double-quote, so expected folder names read cleanly below


class FakeIMAP:
    """Stands in for an imaplib connection: returns canned LIST lines, records nothing else.
    _folders only calls .list() and reads/writes the ._state_folders memo attribute."""

    def __init__(self, lines):
        self._lines = lines

    def list(self):
        return "OK", self._lines


# Standard Gmail: \All and \Trash are searched, INBOX and the rest are not.
GMAIL = [
    rb'(\HasNoChildren) "/" "INBOX"',
    rb'(\HasChildren \Noselect) "/" "[Gmail]"',
    rb'(\All \HasNoChildren) "/" "[Gmail]/All Mail"',
    rb'(\Drafts \HasNoChildren) "/" "[Gmail]/Drafts"',
    rb'(\HasNoChildren \Important) "/" "[Gmail]/Important"',
    rb'(\HasNoChildren \Sent) "/" "[Gmail]/Sent Mail"',
    rb'(\Flagged \HasNoChildren) "/" "[Gmail]/Starred"',
    rb'(\HasNoChildren \Junk) "/" "[Gmail]/Spam"',
    rb'(\HasNoChildren \Trash) "/" "[Gmail]/Trash"',
]

# Localized account: the display names differ, so discovery must key off the flags, not the text.
LOCALIZED = [
    rb'(\HasNoChildren) "/" "INBOX"',
    rb'(\All \HasNoChildren) "/" "[Gmail]/Todos"',
    rb'(\HasNoChildren \Trash) "/" "[Gmail]/Papelera"',
]

# No RFC 6154 special-use flags: nothing to key off, so it degrades to the old INBOX-only search.
BARE = [
    rb'(\HasNoChildren) "/" "INBOX"',
    rb'(\HasNoChildren) "/" "Archive"',
]

# Unquoted name (legal without a space) plus imaplib's tuple form for a literal-encoded name:
# the tuple must be skipped without crashing, and with \All lost that way, INBOX is prepended so
# the search can never end up narrower than Trash alone.
ODD = [
    rb'(\HasNoChildren) "." INBOX',
    (rb'(\All \HasNoChildren) "/" {12}', rb'[Gmail]/Todos'),
    rb'(\HasNoChildren \Trash) "/" "Deleted Items"',
]

CASES = [
    ("standard gmail", GMAIL, [f'{Q}[Gmail]/All Mail{Q}', f'{Q}[Gmail]/Trash{Q}']),
    ("localized names", LOCALIZED, [f'{Q}[Gmail]/Todos{Q}', f'{Q}[Gmail]/Papelera{Q}']),
    ("no special-use flags", BARE, ["INBOX"]),
    ("literal-encoded + unquoted", ODD, ["INBOX", f'{Q}Deleted Items{Q}']),
]


def main():
    fails = 0
    for name, lines, expect in CASES:
        got = mailbox_state._folders(FakeIMAP(lines))
        ok = got == expect
        fails += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {name:<28} -> {got}"
              + ("" if ok else f"  expected {expect}"))

    # The folder list can't change under one login, and a single run asks for it several times,
    # so it is memoised on the connection. Blank the source after the first call: a re-list would
    # collapse to ["INBOX"], so an unchanged result proves the memo held.
    m = FakeIMAP(GMAIL)
    first = mailbox_state._folders(m)
    m._lines = []
    second = mailbox_state._folders(m)
    ok = first == second == [f'{Q}[Gmail]/All Mail{Q}', f'{Q}[Gmail]/Trash{Q}']
    fails += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {'memoised on connection':<28} -> {second}")

    print()
    if fails:
        print(f"{fails} FAILURE(S) — folder discovery is broken.")
        sys.exit(1)
    print(f"All {len(CASES) + 1} cases passed — folder discovery behaves.")


if __name__ == "__main__":
    main()
