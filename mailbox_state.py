#!/usr/bin/env python3
"""
mailbox.py — the mailbox as this pipeline's state store, addressed durably.

Every step here remembers what it has already done by looking for its own past email: a
preview, a booked receipt, a monthly reminder, a failure alert. The subject carries a hash,
finding it means "already handled", and that is the entire idempotency design.

It only works if the search can still SEE the message, and `SELECT INBOX` cannot. Archiving
drops a message out of INBOX; deleting moves it to Trash. Either one erases the record, and
the next scheduled run does the thing again.

That is not hypothetical — it happened on 2026-07-23. The day's test previews were deleted
from the mailbox, and the next poll re-sent an identical "0 to book" preview at 10:42pm
because nothing was left to prove it had already gone out.

So a state lookup runs over the account's \\All folder (inbox + archived + sent) and its
\\Trash folder, which between them cover everywhere a past message can be. Both are
discovered from the server's LIST response rather than hardcoded: Gmail localizes these
names ("[Gmail]/All Mail" is "[Gmail]/Papelera" on a Spanish account) and encodes them in
modified UTF-7, so the bytes the server hands back are the only reliable spelling.

Deliberately NOT used for reading the `confirm` reply. That is the one path that writes to
the ledger, and it stays INBOX-only so that deleting a preview withdraws it.

Stdlib only, on purpose: confirm_and_book.py imports this module, and its parser tests run
in CI before config.py has been materialized from secrets.
"""
import re

# `(\HasNoChildren \All) "/" "[Gmail]/All Mail"` — flags, delimiter, then the name, which is
# quoted whenever it contains a space (i.e. essentially always, on Gmail).
_LIST_RE = re.compile(rb'^\((?P<flags>[^)]*)\)\s+(?:"[^"]*"|NIL)\s+(?P<name>"(?:[^"\\]|\\.)*"|\S+)\s*$')


def q(s):
    """Quote an IMAP search term. Required for anything containing a space or '@' — an
    unquoted multi-word term is parsed as separate tokens and the server answers BAD."""
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _folders(m):
    """-> [mailbox name, ready to pass to SELECT] for the \\All and \\Trash special-use
    folders, in that order. Falls back to ['INBOX'] on a server that doesn't advertise
    RFC 6154 flags, which is exactly the behaviour everything had before.

    Memoised on the connection: a single run asks for state several times, and the folder
    list cannot change underneath one login.
    """
    cached = getattr(m, "_state_folders", None)
    if cached is not None:
        return cached

    found = {}
    try:
        typ, data = m.list()
        if typ == "OK":
            for line in data or []:
                if not isinstance(line, bytes):
                    continue  # LIST can return tuples for literal-encoded names; skip those
                hit = _LIST_RE.match(line.strip())
                if not hit:
                    continue
                flags = hit.group("flags").decode("ascii", "replace").split()
                name = hit.group("name").decode("ascii", "replace")
                if not name.startswith('"'):
                    name = q(name)
                for want in ("\\All", "\\Trash"):
                    if want in flags:
                        found.setdefault(want, name)
    except Exception as e:
        print(f"  ! Couldn't list mailbox folders ({str(e)[:100]}) — using INBOX only.")

    out = [found[k] for k in ("\\All", "\\Trash") if k in found]
    if "\\All" not in found:
        # Never search LESS than the old code did. Without \All we'd be looking in Trash alone
        # and would miss every message still sitting in the inbox — a worse bug than the one
        # this module exists to fix. (imaplib hands back a tuple, not bytes, when the server
        # sends a folder name as a literal; that line is skipped, and this is the safety net.)
        out.insert(0, "INBOX")
    m._state_folders = out
    return out


def _each(m, criteria):
    """Yield (folder, [message numbers]) for every state folder that has a match.

    Message numbers are only meaningful inside the folder they came from, so they are always
    paired with it — anything that goes on to FETCH must have that folder selected.

    Folders are opened read-only: a state lookup must not mark mail as read.
    """
    for folder in _folders(m):
        try:
            typ, _ = m.select(folder, readonly=True)
            if typ != "OK":
                continue
            typ, data = m.search(None, *criteria)
        except Exception as e:
            print(f"  ! Search failed in {folder} ({str(e)[:100]}) — skipping it.")
            continue
        if typ == "OK" and data and data[0]:
            yield folder, data[0].split()


def _restore(m):
    """Leave INBOX selected and writable. Callers share one connection and all of them
    operate on INBOX, so the state lookups clean up after themselves rather than making
    every caller remember to re-select."""
    try:
        m.select("INBOX")
    except Exception:
        pass


def exists(m, *criteria):
    """True if any message in the account matches — read, archived, sent, or deleted."""
    try:
        for _folder, nums in _each(m, criteria):
            if nums:
                return True
        return False
    finally:
        _restore(m)


def fetch(m, criteria, spec):
    """Yield the raw FETCH payload of `spec` for every match, across all state folders."""
    try:
        for folder, nums in _each(m, criteria):
            for num in nums:
                try:
                    typ, raw = m.fetch(num, spec)
                except Exception:
                    continue
                if typ == "OK" and raw and raw[0]:
                    yield raw[0][1]
    finally:
        _restore(m)
