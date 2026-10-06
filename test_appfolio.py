#!/usr/bin/env python3
"""
test_appfolio.py — AppFolio parsing and export selection.

Part 1: a Cash Out is negative even when no category rule matches it.

From 8/10 every run logged `Cash Out 1661.26 != statement 5721.26 (Δ -4060.00) ⚠ CHECK`. Nothing
was missing: a $450 leasing fee and a $1,580 deposit transfer matched no rule, became Review
rows, and the sign was keyed on the category ("Expense" -> negative), so both came out positive
and the reconcile missed by twice their total. A check that always fails hides the day it
fails for a real reason, so this guards the sign and the reconcile together.

Part 2: exactly one export per property is staged, and it's the right one. AppFolio always
names its export "AppFolio Owner Portal _ Transactions.pdf"; until 2026-10-02 sources deduped by
filename, so a new export lost to an old same-name copy, and a renamed one was staged ALONGSIDE
the old one (overlapping YTD rows, twice). Runs sources.merge + appfolio.pick_best together.

Stubs config with placeholder values (the real config.py is private and absent in CI here).
Stdlib only.

    python3 test_appfolio.py
"""
import sys
import types

cfg = types.ModuleType("config")
cfg.APPFOLIO_PROPERTY_MATCH = [("123 main street", "2026 Test P&L", "Test Property")]
cfg.APPFOLIO_ADDRESS_RE = r"123\s+Main\s+Street"
cfg.APPFOLIO_PM_NAME = "Test PM"
cfg.APPFOLIO_BOILERPLATE_RE = r"Test PM LLC\..*?Terms of Service"
sys.modules["config"] = cfg

import appfolio  # noqa: E402
import sources  # noqa: E402

# Same shape as the real print-to-PDF text: summary first, then one field per line.
STATEMENT = """AppFolio Owner Portal
Transactions
Total Cash In
2,000.00
Cash Out
-2,155.00
Date Type Party Property Description Amount Balance
7/10/2026Cash Out
Acme Maintenance
123 Main Street
Lawn care
-75.00
1,925.00
8/1/2026Cash In
Jane Tenant
123 Main Street
Rent Income
2,000.00
3,925.00
8/4/2026Cash Out
Test Management
123 Main Street
Tenant SD transfer
-1,580.00
2,345.00
8/7/2026Cash Out
Test Management
123 Main Street
Leasing Fee
-500.00
1,845.00
"""

failures = []


def check(name, ok, detail=""):
    if not ok:
        failures.append(name)
    print(f"{'PASS' if ok else 'FAIL'}  {name:<52} {detail}")


parsed = appfolio.parse_appfolio_pdf(STATEMENT)
rows = {r["payee"].split(" — ")[-1]: r for r in parsed["rows"]}
check("unrecognized Cash Out (leasing fee) is negative", rows["Leasing Fee"]["amount"] == -500.0,
      repr(rows["Leasing Fee"]["amount"]))
check("...and still held for review, never auto-booked", rows["Leasing Fee"]["type"] == "Review")
check("unrecognized Cash Out (SD transfer) is negative",
      rows["Tenant SD transfer"]["amount"] == -1580.0, repr(rows["Tenant SD transfer"]["amount"]))
check("recognized Cash Out stays a negative expense",
      rows["Lawn care"]["amount"] == -75.0 and rows["Lawn care"]["type"] == "Expense")
check("Cash In stays positive income", rows["Rent Income"]["amount"] == 2000.0
      and rows["Rent Income"]["type"] == "Income")
ok, msg = appfolio.reconcile(parsed)
check("reconcile passes when every Cash Out is counted", ok, msg)

# --- Part 1b: owner payouts wrapped across lines ------------------------------------------
# The 10/2 full-YTD export printed "Owner\nDisbursement". Unmatched, each payout merged into the
# Cash Out above it: the $100 gate repair read as -$2,587.19, the $325 tub repair as -$1,360.14.
WRAPPED = """AppFolio Owner Portal
Transactions
Total Cash In
2,000.00
Cash Out
-425.00
Owner Disbursements
-3,947.33
Date Type Party Property Description Amount Balance
8/7/2026Cash Out
Acme Maintenance
123 Main Street
Fix the backyard gate
-100.00
4,500.00
8/11/2026Owner
Disbursement
Owner Trust
123 Main Street
Owner payout
-2,587.19
1,912.81
9/9/2026Cash Out
Acme Maintenance
123 Main Street
Tub/Shower Valve & Stem Repair
-325.00
1,587.81
9/22/2026 Owner
Disbursement
Owner Trust
123 Main Street
Owner payout
-1,360.14
227.67
"""
p2 = appfolio.parse_appfolio_pdf(WRAPPED)
by_desc = {r["payee"].split(" — ")[-1]: r for r in p2["rows"]}
check("wrapped payout rows are found (4 rows, not 2)", len(p2["rows"]) == 4, f"{len(p2['rows'])} rows")
check("gate repair keeps its own $100", by_desc.get("Fix the backyard gate", {}).get("amount") == -100.0,
      repr(by_desc.get("Fix the backyard gate", {}).get("amount")))
check("tub repair keeps its own $325",
      by_desc.get("Tub/Shower Valve & Stem Repair", {}).get("amount") == -325.0)
payouts = [r for r in p2["rows"] if r["_txtype"] == "Owner Disbursement"]
check("payouts are transfers, never expenses",
      len(payouts) == 2 and all(r["type"] == "Transfer" for r in payouts))
ok2, msg2 = appfolio.reconcile(p2)
check("export with payouts reconciles", ok2, msg2)

# The real 10/2 export's wording, verbatim shape: payouts typed just "Disbursement".
REAL_WORDING = WRAPPED.replace("Owner\nDisbursement", "Disbursement").replace(
    "9/22/2026 Owner\nDisbursement", "9/22/2026 Disbursement")
p3 = appfolio.parse_appfolio_pdf(REAL_WORDING)
pay3 = [r for r in p3["rows"] if r["_txtype"] == "Owner Disbursement"]
check("'Disbursement' rows are payouts (transfers)", len(pay3) == 2 and all(r["type"] == "Transfer" for r in pay3),
      f"{len(pay3)} payout rows")
check("...and the export reconciles", appfolio.reconcile(p3)[0], appfolio.reconcile(p3)[1])

# Categories on the 10/2 export's real descriptions. "Street" leaks into row text when the
# address wraps; "tree" (a grounds keyword) used to match inside it.
cat = lambda party, desc: appfolio._classify("Cash Out", party, desc)[0]  # noqa: E731
check("utility bill with 'Street' in the text -> Utilities",
      cat("Hoosier Homes 3135 East Minnesota", "Citizens Energy group Service -15.01 2,084.99 "
          "Maintenance Street period: 07/9/26") == "Utilities")
check("plain Citizens Energy bill -> Utilities",
      cat("Hoosier Homes Maintenance", "Citizens Energy group Service period: 07/9/26 - 08/3/26") == "Utilities")
check("'Street' alone no longer means grounds upkeep",
      cat("Hoosier Homes", "Something on Main Street") == "Other")
check("'Fix the backyard gate' -> Repairs", cat("Hoosier Homes Maintenance", "2549-1 - Fix the backyard gate") == "Repairs")
check("tree trimming still grounds upkeep", cat("Acme", "Tree trimming") == "Cleaning and Maintenance")
check("mowing still grounds upkeep", cat("Acme", "Mowing bills 05/11") == "Cleaning and Maintenance")
check("water heater install is a repair, not a utility", cat("Acme", "Water heater install") == "Repairs")

# --- Part 2: which export gets staged -------------------------------------------------------
NAME = "AppFolio Owner Portal _ Transactions.pdf"


def export(last_day, *, truncated=False):
    """A placeholder YTD export whose latest transaction is 8/<last_day>. `truncated` drops the
    early rows but keeps the full-YTD summary, like the real cut-off print of 8/10."""
    recs = [("7/10/2026", "Acme Maintenance", "Lawn care", "-75.00")]
    recs += [(f"8/{d}/2026", "Acme Maintenance", f"Grass cutting {d}", "-10.00")
             for d in range(1, last_day + 1)]
    total = 75 + 10 * last_day
    if truncated:
        recs = recs[-2:]
    body = "".join(f"{d}Cash Out\n{p}\n123 Main Street\n{desc}\n{amt}\n1,000.00\n"
                   for d, p, desc, amt in recs)
    return (f"AppFolio Owner Portal\nTransactions\nTotal Cash In\n0.00\nCash Out\n-{total:,.2f}\n"
            f"Date Type Party Property Description Amount Balance\n{body}")


def drive(fid, name, text, mtime):
    return {"id": fid, "name": name, "kind": "pdf", "modifiedTime": mtime, "_text": text}


def staged(inbox=(), done=(), emailed=()):
    """-> (latest date of the staged export, number of exports staged) for the test property."""
    docs = sources.merge(list(inbox), list(done), list(emailed))
    cands = []
    for d in docs:
        text = d["text"] if d["text"] is not None else d["meta"]["_text"]
        p = appfolio.parse_appfolio_pdf(text)
        if p:
            cands.append((d["name"], p))
    (_n, best), _rest = appfolio.pick_best(cands)
    return appfolio.latest(best).day, len(cands)


old, new = export(7), export(28)
day, n = staged(inbox=[drive("a", NAME, old, "2026-08-10")], emailed=[{"name": NAME, "kind": "pdf", "text": new}])
check("emailed newer same-name export beats the old Drive copy", day == 28, f"staged 8/{day}")
day, n = staged(inbox=[drive("a", NAME, old, "2026-08-10"), drive("b", NAME, new, "2026-10-05")])
check("two same-name uploads -> the newer one, either listing order", day == 28, f"staged 8/{day}")
day, n = staged(inbox=[drive("b", NAME, new, "2026-10-05"), drive("a", NAME, old, "2026-08-10")])
check("...reversed listing order, same answer", day == 28, f"staged 8/{day}")
day, n = staged(inbox=[drive("a", NAME, old, "2026-08-10"), drive("b", "AppFolio Sept.pdf", new, "2026-10-05")])
check("renamed export -> still only the newer one is staged", day == 28, f"staged 8/{day} of {n} candidates")
day, n = staged(inbox=[drive("a", NAME, export(20), "2026-08-10"),
                       drive("b", NAME, export(28, truncated=True), "2026-10-05")])
check("newer but truncated (fails reconcile) loses to older complete", day == 20, f"staged 8/{day}")
day, n = staged(done=[drive("a", NAME, old, "2026-07-22")], emailed=[{"name": NAME, "kind": "pdf", "text": old}])
check("identical copies in Done and email -> parsed, one staged", day == 7)
same = sources.merge([], [], [{"name": NAME, "kind": "pdf", "text": old},
                              {"name": NAME, "kind": "pdf", "text": old}])
check("the same attachment emailed twice counts once", len(same) == 1)

# CSVs keep one-per-name: two copies of a Relay CSV would stage its rows twice.
csv = lambda fid, mt: {"id": fid, "name": "Relay 2026-09-01 #6692.csv", "kind": "csv", "modifiedTime": mt}  # noqa: E731
got = sources.merge([csv("i", "x")], [csv("d", "y")], [{"name": "Relay 2026-09-01 #6692.csv", "kind": "csv", "text": "t"}])
check("CSV name collision -> one copy, the inbox one", [d["origin"] for d in got] == ["inbox"])
got = sources.merge([], [csv("d", "y")], [{"name": "Relay 2026-09-01 #6692.csv", "kind": "csv", "text": "t"}])
check("...Done beats email", [d["origin"] for d in got] == ["done"])

# The preview's "new set?" hash must notice a same-name replacement, and ignore Done/.
a = sources.fileset(sources.merge([drive("a", NAME, old, "2026-08-10")], [], []))
b = sources.fileset(sources.merge([drive("b", NAME, new, "2026-10-05")], [], []))
check("same-name replacement changes the preview fileset", a != b)
c = sources.fileset(sources.merge([drive("a", NAME, old, "2026-08-10")], [drive("z", NAME, old, "x")], []))
check("Done/ files don't change the preview fileset", a == c)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {', '.join(failures)}")
    sys.exit(1)
print("All AppFolio cases passed — Cash Outs are negative, and exactly the right export is staged.")
