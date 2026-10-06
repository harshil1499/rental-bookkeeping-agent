#!/usr/bin/env python3
"""
test_notices.py — guards the "Didn't book" notices and the confirm path they open.

A notice answers a reply of yours that didn't book. Three ways this goes wrong, all silent:
  1. A notice's subject collides with another subject lookup. IMAP SUBJECT search is a
     case-insensitive substring match: "Not booked [h]" would match the "Booked" receipt search,
     mark the batch handled, and swallow the confirm the notice asks for.
  2. A blank reply to a notice books. The notice says "reply confirm"; if the quote cutoff misses
     its heading, hitting Send with the notice quoted is a confirmation.
  3. Noise: notices for hold, for file deliveries, for the reply that caused a booking, for old
     mail on deploy, or the same reply answered twice. Each one costs trust in the real ones.

scan() runs here end to end against an in-memory IMAP fake, so the real search strings, gates
and state lookups are what get tested. Stdlib only, no pytest; runs in CI before anything books.

    python3 test_notices.py
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from email import message_from_bytes
from email.message import EmailMessage
from email.utils import format_datetime

os.environ.setdefault("GMAIL_USER", "owner@example.com")
os.environ.setdefault("GMAIL_APP_PASSWORD", "unused")

import confirm_and_book as cb  # noqa: E402

OWNER = os.environ["GMAIL_USER"]
NOW = datetime(2026, 10, 2, 14, 0, tzinfo=timezone.utc)
H, OLD = "d5696b9c9b", "280faa2e21"
PREVIEW = f"Bookkeeping preview — 29 to book [{H}]"
failures = []


def check(name, ok, detail=""):
    if not ok:
        failures.append(name)
    print(f"{'PASS' if ok else 'FAIL'}  {name:<52} {detail}")


# --- an in-memory IMAP server, just enough of RFC 3501 for scan() and mailbox_state ---------

def unq(tok):
    if tok.startswith('"') and tok.endswith('"'):
        return tok[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return tok


class FakeIMAP:
    def __init__(self, msgs):
        self.msgs = msgs                    # [(folders set, EmailMessage)]
        self.view = []

    def list(self):
        return "OK", [b'(\\HasNoChildren \\All) "/" "[Gmail]/All Mail"',
                      b'(\\HasNoChildren \\Trash) "/" "[Gmail]/Trash"']

    def select(self, folder, readonly=False):
        name = {"INBOX": "INBOX", '"[Gmail]/All Mail"': "ALL", '"[Gmail]/Trash"': "TRASH"}[folder]
        self.view = [msg for folders, msg in self.msgs if name in folders]
        return "OK", [str(len(self.view)).encode()]

    def _match(self, msg, toks):
        k = toks.pop(0).upper()
        if k == "OR":
            a, b = self._match(msg, toks), self._match(msg, toks)
            return a or b
        field = {"FROM": "From", "SUBJECT": "Subject"}[k]
        return unq(toks.pop(0)).lower() in cb.header_text(msg, field).lower()

    def search(self, _charset, *criteria):
        hits = []
        for i, msg in enumerate(self.view, 1):
            toks, ok = list(criteria), True
            while toks:
                ok = self._match(msg, toks) and ok
            if ok:
                hits.append(str(i).encode())
        return "OK", [b" ".join(hits)]

    def fetch(self, num, _spec):
        return "OK", [(b"x", self.view[int(num) - 1].as_bytes())]


def mail(subject, body="", *, sender=OWNER, when=NOW, html=None, files=False, mid=None,
         csv_body=b"date,amount\n"):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = sender, OWNER, subject
    m["Date"] = format_datetime(when)
    m["Message-ID"] = mid or f"<{abs(hash((subject, body, when, html)))}@test>"
    if html is not None:
        m.make_alternative()
        m.add_alternative(html, subtype="html")
    else:
        m.set_content(body)
    if files:
        m.add_attachment(csv_body, maintype="text", subtype="csv",
                         filename="Relay 2026-09-01 #6692.csv")
    return m


INBOX, ALL, TRASH = {"INBOX", "ALL"}, {"ALL"}, {"TRASH"}
ago = lambda **kw: NOW - timedelta(**kw)  # noqa: E731


def run(msgs):
    s = cb.scan(FakeIMAP(msgs), now=NOW)
    return s, [(r["said"], kind) for r, kind in cb.due_notices(s, NOW)]


def quiet(fn):
    import contextlib, io
    with contextlib.redirect_stdout(io.StringIO()):
        return fn()


# --- 1. Subjects: no collisions with any other lookup ----------------------------------------
sub = cb.notice_subject(H, "1a2b3c4d")
for label, subj in (("notice", sub), ("reply to notice", "Re: " + sub)):
    low = subj.lower()
    check(f"{label}: no 'booked' (receipt search)", "booked" not in low, repr(subj))
    check(f"{label}: no 'bookkeeping' (preview/reminder/docs)", "bookkeeping" not in low)
    check(f"{label}: no 'error'", cb.ERROR_SUBJECT.lower() not in low)
    check(f"{label}: ASCII only", subj.isascii())
    check(f"{label}: carries the batch hash", (cb.HASH_RE.search(subj) or [None, None])[1] == H)
    check(f"{label}: carries the ref", (cb.REF_RE.search(subj) or [None, None])[1] == "1a2b3c4d")

# A sent notice must not mark its batch handled — the exact trap a "Not booked" subject sets.
s, _ = quiet(lambda: run([(INBOX, mail(PREVIEW)), (ALL, mail(sub))]))
check("a sent notice does not mark the batch handled", H not in s["handled"])

# --- 2. Blank replies to a notice never book; a confirm on one does --------------------------
reply = {"hash": H, "intent": None, "said": "yes looks good", "ref": "1a2b3c4d",
         "sent": ago(hours=1), "files": False}
booked = {H: ago(hours=2)}
for kind in ("unread", "edit", "already"):
    r = dict(reply, said="" if kind == "unread" else reply["said"])
    _subj, text, html = cb.render_notice(r, kind, booked)
    quoted = "\n".join("> " + ln for ln in text.splitlines())
    check(f"{kind}: blank reply, '>' quoted -> nothing", cb.intent(quoted) is None)
    check(f"{kind}: blank reply, unmarked quote -> nothing", cb.intent(text) is None)
    check(f"{kind}: blank reply, curly apostrophe heading",
          cb.intent(text.replace("Didn't", "Didn’t")) is None)
    for single in (False, True):
        m = EmailMessage()
        page = f"<html><body><div><br></div>{html}</body></html>"
        m.set_content(page, subtype="html") if single else (m.make_alternative(),
                                                            m.add_alternative(page, subtype="html"))
        check(f"{kind}: blank HTML reply, unwrapped quote{' (1-part)' if single else ''}",
              cb.intent(cb.plain_body(m)) is None)
    check(f"{kind}: 'confirm' typed above the notice -> confirm",
          cb.intent("confirm\n\n" + text) == "confirm")

s, _ = quiet(lambda: run([(INBOX, mail(PREVIEW)), (INBOX, mail(sub)),
                          (INBOX, mail("Re: " + sub, "confirm\n\n" + text))]))
check("scan: confirm replying to a notice books its batch", s["found"] == [H], repr(s["found"]))
s, _ = quiet(lambda: run([(INBOX, mail(PREVIEW)), (INBOX, mail(sub)),
                          (INBOX, mail("Re: " + sub, text))]))
check("scan: blank reply to a notice books nothing", s["found"] == [], repr(s["found"]))
s, _ = quiet(lambda: run([(INBOX, mail(PREVIEW)),
                          (INBOX, mail("Re: " + sub, "confirm", sender="someone@else.com"))]))
check("scan: confirm on a notice from someone else -> nothing", s["found"] == [])

# --- 3. Who gets a notice: the rule table, end to end through scan() -------------------------
receipt = mail(f"Booked [{H}]", "Booked 29 rows", when=ago(hours=3))
CASES = [
    # (name, mailbox, expected [(said, kind)])
    ("'yes' on an open preview",
     [(INBOX, mail(PREVIEW)), (INBOX, mail("Re: " + PREVIEW, "yes looks good", when=ago(hours=1)))],
     [("yes looks good", "unread")]),
    ("empty iPhone-style reply (parse failure)",
     [(INBOX, mail(PREVIEW)), (INBOX, mail("Re: " + PREVIEW, html='<div>Sent from my iPhone</div>'
                                            '<blockquote>x</blockquote>', when=ago(hours=1)))],
     [("", "unread")]),
    ("edit syntax",
     [(INBOX, mail(PREVIEW)), (INBOX, mail("Re: " + PREVIEW, "confirm except skip 4",
                                            when=ago(hours=1)))],
     [("confirm except skip 4", "edit")]),
    ("hold -> silence",
     [(INBOX, mail(PREVIEW)), (INBOX, mail("Re: " + PREVIEW, "hold", when=ago(hours=1)))], []),
    ("confirm -> silence (it books)",
     [(INBOX, mail(PREVIEW)), (INBOX, mail("Re: " + PREVIEW, "confirm", when=ago(hours=1)))], []),
    ("'yes' alongside a pending confirm -> silence",
     [(INBOX, mail(PREVIEW)), (INBOX, mail("Re: " + PREVIEW, "yes", when=ago(hours=2))),
      (INBOX, mail("Re: " + PREVIEW, "confirm", when=ago(hours=1)))], []),
    ("files attached, no confirm -> silence",
     [(INBOX, mail(PREVIEW)), (INBOX, mail("Re: " + PREVIEW, "here are the files",
                                            files=True, when=ago(hours=1)))], []),
    ("older than 7 days -> silence",
     [(INBOX, mail(PREVIEW)), (INBOX, mail("Re: " + PREVIEW, "yes", when=ago(days=8)))], []),
    ("someone else's reply -> silence",
     [(INBOX, mail(PREVIEW)), (INBOX, mail("Re: " + PREVIEW, "yes", sender="x@y.com",
                                            when=ago(hours=1)))], []),
    ("already answered (notice archived) -> silence",
     [(INBOX, mail(PREVIEW)), (INBOX, mail("Re: " + PREVIEW, "yes", when=ago(hours=1), mid="<r1@t>")),
      (ALL, mail(cb.notice_subject(H, cb.reply_ref(mail("x", mid="<r1@t>"))), when=ago(minutes=30)))],
     []),
    ("already answered (notice deleted) -> silence",
     [(INBOX, mail(PREVIEW)), (INBOX, mail("Re: " + PREVIEW, "yes", when=ago(hours=1), mid="<r1@t>")),
      (TRASH, mail(cb.notice_subject(H, cb.reply_ref(mail("x", mid="<r1@t>"))), when=ago(minutes=30)))],
     []),
    ("the confirm that caused the booking -> silence",
     [(INBOX, mail(PREVIEW)), (INBOX, mail("Re: " + PREVIEW, "Confirm", when=ago(hours=13))),
      (ALL, receipt)], []),
    ("non-confirm reply BEFORE the booking -> silence",
     [(INBOX, mail(PREVIEW)), (INBOX, mail("Re: " + PREVIEW, "yes", when=ago(hours=13))),
      (ALL, receipt)], []),
    ("confirm AFTER the booking -> already",
     [(INBOX, mail(PREVIEW)), (ALL, receipt),
      (INBOX, mail("Re: " + PREVIEW, "confirm", when=ago(hours=1)))],
     [("confirm", "already")]),
    ("edit AFTER the booking -> already",
     [(INBOX, mail(PREVIEW)), (ALL, receipt),
      (INBOX, mail("Re: " + PREVIEW, "row 3 is wrong, 3 -> Repairs confirm", when=ago(hours=1)))],
     [("row 3 is wrong, 3 -> Repairs confirm", "already")]),
    ("chatter AFTER the booking -> silence",
     [(INBOX, mail(PREVIEW)), (ALL, receipt),
      (INBOX, mail("Re: " + PREVIEW, "thanks!", when=ago(hours=1)))], []),
    ("reply after a booking ERROR -> silence (the error email covers it)",
     [(INBOX, mail(PREVIEW)), (ALL, mail(f"Bookkeeping ERROR [{H}]", "x", when=ago(hours=3))),
      (INBOX, mail("Re: " + PREVIEW, "confirm", when=ago(hours=1)))], []),
    ("receipt archived still counts as booked",
     [(INBOX, mail(PREVIEW)), (TRASH, receipt),
      (INBOX, mail("Re: " + PREVIEW, "confirm", when=ago(hours=1)))],
     [("confirm", "already")]),
    ("blank reply to a notice -> a fresh notice, once",
     [(INBOX, mail(PREVIEW)), (INBOX, mail(sub, when=ago(hours=2))),
      (INBOX, mail("Re: " + sub, "Didn't book\n\nYour reply...", when=ago(hours=1)))],
     [("", "unread")]),
]
for name, msgs, want in CASES:
    _s, got = quiet(lambda: run(msgs))
    check(name, got == want, f"-> {got!r}" if got != want else "")

# An old preview's handled reply stays silent even though it's in INBOX (deploy-day backfill).
_s, got = quiet(lambda: run([(INBOX, mail(f"Bookkeeping preview — 31 to book [{OLD}]")),
                             (INBOX, mail(f"Re: Bookkeeping preview — 31 to book [{OLD}]",
                                          "confirm", when=ago(days=60))),
                             (ALL, mail(f"Booked [{OLD}]", when=ago(days=60)))]))
check("deploy day: July's handled confirm stays silent", got == [], repr(got))

# --- 3b. Stale confirms never book --------------------------------------------------------
# 2026-10-04: Gmail purged July's trashed receipt (Trash empties after 30 days), so batch
# 280faa2e21 read as unhandled and the August confirm still in INBOX re-ran booking. Only the
# promoted stamps kept it at 0 rows. A confirm now counts only while it's under 7 days old.
JULY = f"Bookkeeping preview \u2014 31 to book [{OLD}]"
s, _ = quiet(lambda: run([(INBOX, mail(PREVIEW)), (INBOX, mail("Re: " + PREVIEW, "confirm", when=ago(days=6)))]))
check("confirm 6 days old still books", s["found"] == [H], repr(s["found"]))
s, _ = quiet(lambda: run([(INBOX, mail(PREVIEW)), (INBOX, mail("Re: " + PREVIEW, "confirm", when=ago(days=8)))]))
check("confirm 8 days old is ignored", s["found"] == [], repr(s["found"]))
s, _ = quiet(lambda: run([(INBOX, mail(JULY, when=ago(days=62))),
                          (INBOX, mail("Re: " + JULY, "confirm", when=ago(days=60)))]))   # receipt purged
check("Oct 4 replay: purged receipt + old confirm -> no booking", s["found"] == [], repr(s["found"]))
undated = mail("Re: " + PREVIEW, "confirm")
del undated["Date"]
s, _ = quiet(lambda: run([(INBOX, mail(PREVIEW)), (INBOX, undated)]))
check("undated confirm is ignored (fails closed)", s["found"] == [], repr(s["found"]))
s, _ = quiet(lambda: run([(INBOX, mail(PREVIEW)), (INBOX, mail(sub, when=ago(days=9))),
                          (INBOX, mail("Re: " + sub, "confirm", when=ago(days=8)))]))
check("old confirm on a notice is ignored too", s["found"] == [], repr(s["found"]))

# --- 3c. "Waiting on your confirm" follow-ups ----------------------------------------------
# 2026-10-02: files went in on the reminder thread, the preview they produced sat unanswered, and
# nothing followed up. One follow-up per preview after 24h; never if answered/booked/replaced.
def followups(msgs):
    s = quiet(lambda: cb.scan(FakeIMAP(msgs), now=NOW))
    return [p["hash"] for p in cb.due_followups(s, NOW)], s


PREV_B = f"Bookkeeping preview \u2014 11 to book [{OLD}]"
FU_CASES = [
    ("unanswered 25h -> one follow-up", [(INBOX, mail(PREVIEW, when=ago(hours=25)))], [H]),
    ("unanswered 23h -> not yet", [(INBOX, mail(PREVIEW, when=ago(hours=23)))], []),
    ("answered 'hold' -> silence",
     [(INBOX, mail(PREVIEW, when=ago(hours=30))), (INBOX, mail("Re: " + PREVIEW, "hold", when=ago(hours=29)))], []),
    ("answered 'yes' -> silence (the notice covers it)",
     [(INBOX, mail(PREVIEW, when=ago(hours=30))), (INBOX, mail("Re: " + PREVIEW, "yes", when=ago(hours=29)))], []),
    ("booked -> silence",
     [(INBOX, mail(PREVIEW, when=ago(hours=30))), (ALL, mail(f"Booked [{H}]", when=ago(hours=2)))], []),
    ("replaced by a newer preview -> silence for both (newer is 2h old)",
     [(INBOX, mail(PREVIEW, when=ago(hours=30))), (INBOX, mail(PREV_B, when=ago(hours=2)))], []),
    ("replaced, newer is 26h old -> follow up the newer only",
     [(INBOX, mail(PREVIEW, when=ago(hours=50))), (INBOX, mail(PREV_B, when=ago(hours=26)))], [OLD]),
    ("already followed up (archived) -> silence",
     [(INBOX, mail(PREVIEW, when=ago(hours=50))), (ALL, mail(f"Waiting on your confirm [{H}]", when=ago(hours=20)))], []),
    ("already followed up (deleted) -> silence",
     [(INBOX, mail(PREVIEW, when=ago(hours=50))), (TRASH, mail(f"Waiting on your confirm [{H}]", when=ago(hours=20)))], []),
    ("15 days old -> silence (no backfill)", [(INBOX, mail(PREVIEW, when=ago(days=15)))], []),
    ("preview you deleted -> silence", [(TRASH, mail(PREVIEW, when=ago(hours=30)))], []),
]
for name, msgs, want in FU_CASES:
    got, _s = followups(msgs)
    check(name, got == want, f"-> {got!r}" if got != want else "")

fu = {"hash": H, "subject": PREVIEW, "sent": ago(hours=30),
      "text": "Bookkeeping preview - 2 row(s) ready to book.\nReply 'confirm' to this email to book these rows.",
      "html": "<div><h2>Bookkeeping preview</h2><p>Reply <strong>confirm</strong> to book.</p></div>"}
fsub, ftext, fhtml = cb.render_followup(fu)
check("follow-up subject: no booked/bookkeeping/didn't book, ASCII, has [hash]",
      all(x not in fsub.lower() for x in ("booked", "bookkeeping", "didn't book", "still needed"))
      and fsub.isascii() and (cb.HASH_RE.search(fsub) or [0, 0])[1] == H, repr(fsub))
check("blank reply to a follow-up (unmarked quote) -> nothing", cb.intent(ftext) is None)
check("blank reply to a follow-up ('>' quote) -> nothing",
      cb.intent("\n".join("> " + ln for ln in ftext.splitlines())) is None)
m = EmailMessage(); m.make_alternative(); m.add_alternative(f"<html><body><div><br></div>{fhtml}</body></html>", subtype="html")
check("blank HTML reply to a follow-up -> nothing", cb.intent(cb.plain_body(m)) is None)
check("'confirm' above the follow-up -> confirm", cb.intent("confirm\n\n" + ftext) == "confirm")
s, _ = quiet(lambda: run([(INBOX, mail(PREVIEW, when=ago(hours=30))), (INBOX, mail(fsub, ftext, when=ago(hours=5))),
                          (INBOX, mail("Re: " + fsub, "confirm\n\n" + ftext, when=ago(hours=1)))]))
check("scan: confirm replying to a follow-up books its batch", s["found"] == [H], repr(s["found"]))
_got, s = followups([(INBOX, mail(PREVIEW, when=ago(hours=3)))])
w = cb.waiting_previews(s, NOW)
check("reminder: an unanswered preview is listed as waiting", [x["subject"] for x in w] == [PREVIEW], repr(w))
_got, s = followups([(INBOX, mail(PREVIEW, when=ago(hours=3))), (INBOX, mail("Re: " + PREVIEW, "confirm", when=ago(hours=1)))])
check("reminder: an answered preview isn't", cb.waiting_previews(s, NOW) == [])
import ast  # noqa: E402
_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "email_docs.py")).read()
_markers = next(ast.literal_eval(n.value) for n in ast.walk(ast.parse(_src))
                if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "SUBJECT_MARKERS")
check("email_docs reads replies to follow-ups (files attached)",
      any(mk.lower() in fsub.lower() for mk in _markers), repr(_markers))

# --- 4. Files sent on a notice thread reach the pipeline -------------------------------------
# A reply with files gets no notice, on the promise that the next preview picks them up. That
# only holds if email_docs reads notice threads. Parity is checked without importing email_docs
# (it needs pymupdf, and this file stays stdlib-only)...
import ast  # noqa: E402

src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "email_docs.py")).read()
markers = next(ast.literal_eval(n.value) for n in ast.walk(ast.parse(src))
               if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "SUBJECT_MARKERS")
check("email_docs reads notice threads (marker parity)", cb.NOTICE_SUBJECT in markers, repr(markers))

# ...and end to end through the real reader wherever pymupdf is installed. CI always has it, so
# there a missing import is a failure, not a skip.
try:
    import email_docs
except ImportError as e:
    if os.environ.get("CI"):
        check("email_docs importable in CI", False, str(e))
    else:
        print(f"SKIP  email_docs end-to-end (no pymupdf locally: {e})")
else:
    def docs(msgs):
        fake = FakeIMAP(msgs)
        fake.login = fake.logout = lambda *a: ("OK", [b""])
        email_docs.USER, email_docs.PW, email_docs._CACHE = OWNER, "x", None
        email_docs.imaplib.IMAP4_SSL = lambda *_a, **_k: fake
        return sorted(s["name"] for s in quiet(email_docs.fetch))
    real_ssl = email_docs.imaplib.IMAP4_SSL
    try:
        check("files on a reply to a notice are picked up",
              docs([(ALL, mail(sub)), (ALL, mail("Re: " + sub, "here", files=True))])
              == ["Relay 2026-09-01 #6692.csv"])
        check("files on an archived reply to a notice still count",
              docs([(ALL, mail("Re: " + sub, "here", files=True))]) == ["Relay 2026-09-01 #6692.csv"])
        check("files on a notice reply from someone else are ignored",
              docs([(ALL, mail("Re: " + sub, "here", files=True, sender="x@y.com"))]) == [])
        two = docs([(ALL, mail("Re: " + sub, "v1", files=True)),
                    (ALL, mail("Re: " + PREVIEW, "v2", files=True, csv_body=b"date,amount\n9/1,5\n"))])
        check("two same-name attachments with different content -> both kept",
              two == ["Relay 2026-09-01 #6692.csv"] * 2, repr(two))
        check("files on a preview reply still picked up (no regression)",
              docs([(ALL, mail("Re: " + PREVIEW, "here", files=True))]) == ["Relay 2026-09-01 #6692.csv"])
    finally:
        email_docs.imaplib.IMAP4_SSL, email_docs._CACHE = real_ssl, None

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {', '.join(failures)}")
    sys.exit(1)
print("All notice cases passed — notices fire only when a reply didn't book, and never book.")
