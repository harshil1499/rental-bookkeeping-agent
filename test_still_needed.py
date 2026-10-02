#!/usr/bin/env python3
"""
test_still_needed.py — guards the one definition of "still needed" and the mid-month nudge.

The old "Still needed" missed two real gaps at once (2026-10-02): Hideaway Hollow's August
mortgage statement (a draft waiting on a statement — it only checked the reverse) and Indy's
AppFolio export stopping at Aug 7 (it only asked "is any AppFolio file present?"). The fixture
below IS that situation, so those two must surface and nothing else may.

The nudge sends real email, so: once per month at most, never before the 15th's cutoff, never
when nothing is missing, and a subject that collides with no other mailbox lookup.

Stdlib only; runs in CI before secrets are materialized.

    python3 test_still_needed.py
"""
import ast
import contextlib
import io
import os
import sys
import types
from datetime import date

import still_needed as sn

failures = []


def check(name, ok, detail=""):
    if not ok:
        failures.append(name)
    print(f"{'PASS' if ok else 'FAIL'}  {name:<58} {detail}")


SS, HH, INDY = "2026 SS P&L", "2026 HH P&L", "2026 3135 E Minnesota P&L"
LABELS = {SS: "Sailing Skies", HH: "Hideaway Hollow", INDY: "3135 E Minnesota"}
ym = lambda *ms: {(2026, m) for m in ms}  # noqa: E731

# What had arrived on 2026-10-02 (Drive inbox + emailed files), per the CI smoke test.
REAL = {
    SS:   {"csv_ym": ym(7, 8, 9), "stmt_ym": ym(7, 8, 9, 10)},
    HH:   {"csv_ym": ym(7, 8, 9), "stmt_ym": ym(7, 9, 10)},                 # no August statement
    INDY: {"csv_ym": ym(7, 8, 9), "stmt_ym": ym(7, 8, 9, 10),
           "appfolio_through": date(2026, 8, 7)},                            # export ends Aug 7
}


def need(by_prop, today, through=None):
    return dict(sn.missing(by_prop, sn.closed_months(today, through=through), labels=LABELS,
                           appfolio_sheet=INDY, today=today))


# --- 1. Which months count -------------------------------------------------------------------
check("closed months on Oct 2", sn.closed_months(date(2026, 10, 2)) == [(2026, 7), (2026, 8), (2026, 9)])
check("closed months capped by `through`",
      sn.closed_months(date(2026, 10, 2), through=(2026, 8)) == [(2026, 7), (2026, 8)])
check("nothing closed before the first tracked month ends", sn.closed_months(date(2026, 7, 20)) == [])
check("year rollover", sn.closed_months(date(2027, 1, 5))[-1] == (2026, 12))
check("nudge covers August on Oct 2", sn.nudge_through(date(2026, 10, 2)) == (2026, 8))
check("nudge covers August on Oct 14", sn.nudge_through(date(2026, 10, 14)) == (2026, 8))
check("nudge covers September from Oct 15", sn.nudge_through(date(2026, 10, 15)) == (2026, 9))
check("nudge across a year boundary", sn.nudge_through(date(2027, 1, 20)) == (2026, 12)
      and sn.nudge_through(date(2027, 1, 3)) == (2026, 11))

# --- 2. The real situation: both blind spots surface, nothing else does ----------------------
got = need(REAL, date(2026, 10, 2), through=(2026, 8))
check("HH: draft waiting on a statement surfaces",
      got.get("Hideaway Hollow") == ["Mortgage statement for August"], repr(got.get("Hideaway Hollow")))
check("Indy: AppFolio export ending Aug 7 leaves August open",
      got.get("3135 E Minnesota") == ["AppFolio export covering all of August (the latest one ends Aug 7)"],
      repr(got.get("3135 E Minnesota")))
check("Sailing Skies: complete -> not listed", "Sailing Skies" not in got)
got = need(REAL, date(2026, 10, 15))
check("through September, Indy's AppFolio gap spans both months",
      got.get("3135 E Minnesota") == ["AppFolio export covering all of August, September "
                                      "(the latest one ends Aug 7)"], repr(got.get("3135 E Minnesota")))
check("October's early statements never create an item (month not closed)",
      all("October" not in t for items in got.values() for t in items))

# --- 3. Edges -------------------------------------------------------------------------------
af = lambda d: {**REAL, INDY: {**REAL[INDY], "appfolio_through": d}}  # noqa: E731
check("AppFolio txn ON the last day doesn't prove the month complete",
      "3135 E Minnesota" in need(af(date(2026, 8, 31)), date(2026, 10, 2), through=(2026, 8)))
check("AppFolio txn the day after does",
      "3135 E Minnesota" not in need(af(date(2026, 9, 1)), date(2026, 10, 2), through=(2026, 8)))
check("no AppFolio file at all -> every month, no date tail",
      need(af(None), date(2026, 10, 2), through=(2026, 8))["3135 E Minnesota"]
      == ["AppFolio export covering all of July, August"])
got = need({}, date(2026, 9, 3))
check("nothing arrived -> every doc for every property",
      got == {"Sailing Skies": ["Relay CSV for July, August", "Mortgage statements for July, August"],
              "Hideaway Hollow": ["Relay CSV for July, August", "Mortgage statements for July, August"],
              "3135 E Minnesota": ["Relay CSV for July, August", "Mortgage statements for July, August",
                                   "AppFolio export covering all of July, August"]}, repr(got))
check("prior-year months carry the year",
      sn.month_name((2026, 12), date(2027, 1, 5)) == "December 2026"
      and sn.month_name((2027, 1), date(2027, 2, 1)) == "January")

# --- 4. Subjects: no collisions with any other lookup ----------------------------------------
subj = sn.subject((2026, 8))
low = subj.lower()
check("nudge subject reads right", subj == "Still needed for August 2026", repr(subj))
for needle, why in (("booked", "receipt search marks batches handled"),
                    ("bookkeeping", "reminder dedupe / preview / docs"),
                    ("didn't book", "notice lookups"), ("[", "hash lookups")):
    check(f"nudge subject has no {needle!r} ({why})", needle not in low)
check("nudge subject is ASCII", subj.isascii())
OTHERS = ["Bookkeeping preview — 29 to book [d5696b9c9b]", "Booked [d5696b9c9b]",
          "Bookkeeping ERROR [d5696b9c9b]", "Didn't book [d5696b9c9b] - ref 1a2b3c4d",
          "Rental bookkeeping due (2026-10)"]
check("no other subject trips the nudge's own dedupe search",
      all(sn.NUDGE_SUBJECT.lower() not in o.lower() for o in OTHERS))

src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "email_docs.py")).read()
markers = next(ast.literal_eval(n.value) for n in ast.walk(ast.parse(src))
               if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "SUBJECT_MARKERS")
check("email_docs reads replies to the nudge (files attached)",
      any(mk.lower() in subj.lower() for mk in markers), repr(markers))

# --- 5. The nudge flow: once a month, only when something is missing ------------------------
sent, collected = [], []
stub_relay = types.ModuleType("import_relay")
stub_relay.drive_service = lambda: "svc"
stub_relay.load_drive_config = lambda: {}
stub_preview = types.ModuleType("preview_email")
stub_preview.inbox_url = lambda cfg: "https://drive.example/inbox"
stub_preview.send = lambda s, t, h: sent.append((s, t, h))
stub_preview.FONT = stub_preview.INK = stub_preview.MUTED = stub_preview.FAINT = ""
stub_preview.esc = lambda s: s
stub_preview.needs_block = lambda needs, url: "".join(t for _l, items in needs for t in items)
sys.modules.update(import_relay=stub_relay, preview_email=stub_preview)


def run_nudge(today, *, nudged=False, needs=(), dry_run=False):
    sent.clear(), collected.clear()
    sn.already_nudged = lambda s: nudged
    sn.collect = lambda svc, cfg, today=None, through=None: (collected.append(through) or list(needs), [])
    with contextlib.redirect_stdout(io.StringIO()):
        sn.nudge(dry_run=dry_run, today=today)
    return [s for s, _t, _h in sent]


GAP = [("Hideaway Hollow", ["Mortgage statement for August"])]
check("due + missing -> one nudge for August",
      run_nudge(date(2026, 10, 2), needs=GAP) == ["Still needed for August 2026"])
check("...covering only months through August", collected == [(2026, 8)], repr(collected))
check("nudge body lists the item", "Mortgage statement for August" in sent[0][1] if sent else False)
check("already sent this month -> silence, and no Drive reads",
      run_nudge(date(2026, 10, 9), nudged=True, needs=GAP) == [] and collected == [])
check("nothing missing -> silence", run_nudge(date(2026, 10, 2), needs=[]) == [])
check("before the first tracked month's nudge date -> silence, no reads",
      run_nudge(date(2026, 8, 10), needs=GAP) == [] and collected == [])
check("July's nudge is due from Aug 15",
      run_nudge(date(2026, 8, 15), needs=GAP) == ["Still needed for July 2026"])
check("from the 15th it's September's nudge",
      run_nudge(date(2026, 10, 15), needs=GAP) == ["Still needed for September 2026"])
check("dry run sends nothing", run_nudge(date(2026, 10, 2), needs=GAP, dry_run=True) == [])

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {', '.join(failures)}")
    sys.exit(1)
print("All still-needed cases passed — gaps surface, nudges stay rare.")
