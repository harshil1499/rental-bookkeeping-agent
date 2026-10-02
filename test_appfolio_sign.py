#!/usr/bin/env python3
"""
test_appfolio_sign.py — a Cash Out is negative even when no category rule matches it.

From 8/10 every run logged `Cash Out 1661.26 != statement 5721.26 (Δ -4060.00) ⚠ CHECK`. Nothing
was missing: a $450 leasing fee and a $1,580 deposit transfer matched no rule, became Review
rows, and the sign was keyed on the category ("Expense" -> negative), so both came out positive
and the reconcile missed by twice their total. A check that always fails hides the day it
fails for a real reason, so this guards the sign and the reconcile together.

Stubs config with placeholder values (the real config.py is private and absent in CI here).
Stdlib only.

    python3 test_appfolio_sign.py
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

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {', '.join(failures)}")
    sys.exit(1)
print("All AppFolio sign cases passed — a Cash Out is negative whatever its category.")
