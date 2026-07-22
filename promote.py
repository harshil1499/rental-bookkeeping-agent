#!/usr/bin/env python3
"""
promote.py — Phase 1b final step: promote reviewed `Import` rows into the month registers.

Promote-by-default. Every eligible NEW row is booked into its month tab at the right target;
rows that shouldn't be booked are auto-skipped; only genuinely ambiguous rows are flagged for
you. You confirm the whole batch once — no per-row ticking.

Row handling
------------
- Income                    -> the month tab's income block (fills an empty slot, else appends)
- Mortgage Interest         -> accumulate into the 'Mortgage Interest' line
- Escrow (Taxes + Insurance)-> accumulate into the 'Escrow (Taxes + Insurance)' line
- Utilities                 -> accumulate into Electricity / Water / Internet / Gas / Garbage
                              line by payee (unknown utility -> new Variable row)
- Other expense categories  -> a new Variable Expenses row (category preserved)
- superseded / Transfer /
  maybe-entered / Skip='x'   -> skipped (with reason)
- 'Other' / uncategorized   -> asked (not booked) until you set a category in the Import tab

Note: the Summary totals SUMIF by category, so placement only affects readability, never the
totals — but we match your named-line structure anyway.

Opt-out: put an 'x' in column H ('Skip') of the Import tab on any row to hold it back.

Usage
-----
    python3 promote.py                     # dry-run plan for both properties (writes nothing)
    python3 promote.py -p lakehouse        # dry-run, one property
    python3 promote.py --write             # execute (writes into the registers)
"""
import argparse
import sys
import warnings
from datetime import datetime

warnings.filterwarnings("ignore")

import config
from capture import (open_sheet, parse_money, find_named_line, find_income_block_end,
                     last_content_row, EXPENSE_CATEGORIES)

SHEETS = config.SHEETS
IMPORT_TAB = "Import"
SKIP_COL = 7      # H (0-indexed) — user opt-out
PROMOTED_COL = 8  # I — idempotency stamp

MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]


def utility_line(payee):
    p = payee.lower()
    for vendor, line in config.UTILITY_VENDOR_LINES.items():  # specific local vendors (config)
        if vendor in p:
            return line
    if "water" in p:
        return "Water"
    if "elec" in p:
        return "Electricity"
    if "internet" in p or "wifi" in p:
        return "Internet"
    if "gas" in p:
        return "Gas"
    if "garbage" in p or "trash" in p:
        return "Garbage Pickup"
    return None


def resolve(r):
    """-> (action, target). action in {income, accumulate, variable, skip, ask}."""
    if r["skip"]:
        return ("skip", "you marked Skip")
    if r["promoted"]:
        return ("skip", "already promoted")
    if r["status"] == "superseded":
        return ("skip", "superseded raw draft")
    if r["status"] == "hold":
        return ("skip", "held — mortgage draft awaiting its statement to split")
    if r["status"] == "maybe-entered":
        return ("skip", "already in register")
    cat, typ, payee = r["category"], r["type"], r["payee"]
    if cat == "Transfer (exclude)":
        return ("skip", "transfer — not a P&L item")
    if typ == "Income":
        return ("income", None)
    if cat == "Mortgage Interest":
        return ("accumulate", "Mortgage Interest")
    if cat == "Escrow (Taxes + Insurance)":
        return ("accumulate", "Escrow (Taxes + Insurance)")
    if cat == "Utilities":
        line = utility_line(payee)
        return ("accumulate", line) if line else ("variable", None)
    if cat in EXPENSE_CATEGORIES and cat != "Other":
        return ("variable", None)
    return ("ask", "uncategorized ('Other') — set a category in the Import tab first")


def read_import(sheet):
    """-> (list of row dicts with their 1-based Import-tab row index) or None if no Import tab."""
    try:
        ws = sheet.worksheet(IMPORT_TAB)
    except Exception:
        return None
    grid = ws.get("A1:I400")
    rows = []
    for i, g in enumerate(grid, 1):
        g = (g + [""] * 9)[:9]
        date_s, payee, amount, typ, cat, status, note, skip, promoted = g
        if i == 1 or not (str(payee).strip() or str(amount).strip()):
            continue  # header or blank spacer
        try:
            amt = parse_money(amount)
        except ValueError:
            continue
        rows.append({
            "irow": i, "date": str(date_s).strip(), "payee": str(payee).strip(),
            "amount": amt, "type": str(typ).strip(), "category": str(cat).strip(),
            "status": str(status).strip(), "skip": bool(str(skip).strip()),
            "promoted": bool(str(promoted).strip()),
        })
    return ws, rows


def month_of(date_str):
    for fmt in ("%m/%d/%Y", "%-m/%-d/%Y"):
        try:
            return MONTHS[datetime.strptime(date_str, fmt).month - 1]
        except ValueError:
            continue
    return None


class TabWriter:
    """Buffers writes into one month register tab; matches the sheet's income/named-line layout."""

    def __init__(self, sheet, month):
        self.ws = sheet.worksheet(month)
        self.grid = self.ws.get("A1:E250")
        self.rows = [(g + [""] * 5)[:5] for g in self.grid]
        end = find_income_block_end(self.rows) or 2
        self.income_slots = [i for i in range(2, end)
                             if len(self.rows[i - 1]) > 3
                             and str(self.rows[i - 1][3]).strip() == "Rental Income"
                             and not str(self.rows[i - 1][2]).strip()]
        self.append_ptr = last_content_row(self.rows) + 1
        self.accum = {}       # line label -> amount to add
        self.updates = []     # (a1_range, [[...]])

    def add_income(self, r):
        amt = abs(r["amount"])  # register stores magnitudes; income counted by col D
        if self.income_slots:
            row = self.income_slots.pop(0)
            self.updates.append((f"A{row}:C{row}", [[r["date"], r["payee"], amt]]))
        else:
            self.updates.append((f"A{self.append_ptr}:E{self.append_ptr}",
                                 [[r["date"], r["payee"], amt, "Rental Income", ""]]))
            self.append_ptr += 1

    def add_variable(self, r):
        # expenses are stored POSITIVE in the register (Summary subtracts them)
        self.updates.append((f"A{self.append_ptr}:E{self.append_ptr}",
                             [[r["date"], r["payee"], abs(r["amount"]), "", r["category"]]]))
        self.append_ptr += 1

    def add_accumulate(self, line_label, r):
        self.accum[line_label] = self.accum.get(line_label, 0.0) + abs(r["amount"])

    def flush(self):
        for label, delta in self.accum.items():
            row = find_named_line(self.rows, label)
            if not row:  # named line missing — fall back to a variable row
                self.updates.append((f"A{self.append_ptr}:E{self.append_ptr}",
                                     [["", label, round(delta, 2), "", label]]))
                self.append_ptr += 1
                continue
            current = parse_money(self.rows[row - 1][2] if len(self.rows[row - 1]) > 2 else "")
            self.updates.append((f"C{row}", [[round(current + delta, 2)]]))
        if self.updates:
            self.ws.batch_update([{"range": r, "values": v} for r, v in self.updates],
                                 value_input_option="USER_ENTERED")
        return len(self.updates)


def process_sheet(sheet_name, do_write):
    sheet = open_sheet(sheet_name)
    res = read_import(sheet)
    if not res:
        print(f"\n=== {sheet_name} === no Import tab — run import_relay.py first.")
        return
    import_ws, rows = res

    # Lazy cache of month-tab grids, to guard accumulate against non-empty lines.
    tab_cache = {}
    def current_line_value(month, label):
        if month not in tab_cache:
            try:
                tab_cache[month] = [(g + [""] * 5)[:5] for g in sheet.worksheet(month).get("A1:E250")]
            except Exception:
                tab_cache[month] = []
        grid = tab_cache[month]
        row = find_named_line(grid, label)
        if not row:
            return None  # line missing — flush() will fall back to a variable row
        cell = grid[row - 1][2] if len(grid[row - 1]) > 2 else ""
        return parse_money(cell) if str(cell).strip() else 0.0

    plan = {"income": [], "accumulate": [], "variable": [], "skip": [], "ask": []}
    for r in rows:
        action, target = resolve(r)
        r["_month"] = month_of(r["date"]) if action in ("income", "accumulate", "variable") else None
        if action in ("income", "accumulate", "variable") and not r["_month"]:
            plan["ask"].append((r, "no readable date"))
        elif action == "skip":
            plan["skip"].append((r, target))
        elif action == "ask":
            plan["ask"].append((r, target))
        elif action == "accumulate":
            cur = current_line_value(r["_month"], target)
            if cur:  # non-empty line — never add on top of an existing value; flag instead
                plan["ask"].append((r, f"'{target}' line in {r['_month']} already holds {cur:.2f} "
                                       f"— won't add on top; check/clear it first"))
            else:
                plan["accumulate"].append((r, target))
        else:
            plan[action].append((r, target))

    n_book = len(plan["income"]) + len(plan["accumulate"]) + len(plan["variable"])
    print(f"\n=== {sheet_name} ===")
    print(f"  Promote: {n_book}   Skip: {len(plan['skip'])}   Needs attention: {len(plan['ask'])}")
    for r, tgt in plan["income"]:
        print(f"    + income   {r['_month']:<9} {r['amount']:>9.2f}  {r['payee']}")
    for r, tgt in plan["accumulate"]:
        print(f"    + {tgt:<22} {r['_month']:<9} {r['amount']:>9.2f}  {r['payee']}")
    for r, tgt in plan["variable"]:
        print(f"    + variable/{r['category']:<12} {r['_month']:<9} {r['amount']:>9.2f}  {r['payee']}")
    if plan["ask"]:
        print("  NEEDS ATTENTION (not promoted):")
        for r, why in plan["ask"]:
            print(f"    ? {r['amount']:>9.2f}  {r['payee']:<28} {why}")

    if not do_write:
        return

    # Execute: group bookings by (month tab), write, then stamp promoted rows.
    writers, promoted_irows = {}, []
    def wr(month):
        if month not in writers:
            writers[month] = TabWriter(sheet, month)
        return writers[month]

    for r, _ in plan["income"]:
        wr(r["_month"]).add_income(r); promoted_irows.append(r["irow"])
    for r, tgt in plan["accumulate"]:
        wr(r["_month"]).add_accumulate(tgt, r); promoted_irows.append(r["irow"])
    for r, _ in plan["variable"]:
        wr(r["_month"]).add_variable(r); promoted_irows.append(r["irow"])

    total = sum(w.flush() for w in writers.values())
    if promoted_irows:
        if import_ws.col_count <= PROMOTED_COL:  # ensure column I exists before stamping
            import_ws.add_cols(PROMOTED_COL + 1 - import_ws.col_count)
        stamp = [{"range": f"I{i}", "values": [["✓ promoted"]]} for i in promoted_irows]
        import_ws.batch_update(stamp, value_input_option="USER_ENTERED")
    print(f"  ✓ Wrote {total} updates across {len(writers)} month tab(s); "
          f"stamped {len(promoted_irows)} Import rows as promoted.")


def main():
    ap = argparse.ArgumentParser(description="Promote reviewed Import rows into the month registers.")
    ap.add_argument("--property", "-p", help="only this property (default: both)")
    ap.add_argument("--write", action="store_true", help="execute (default: dry-run plan)")
    args = ap.parse_args()

    from capture import resolve_property
    sheets = [resolve_property(args.property)] if args.property else SHEETS
    for s in sheets:
        process_sheet(s, args.write)

    if not args.write:
        print("\n(Dry run — nothing written. Re-run with --write to book these into the registers.)")


if __name__ == "__main__":
    main()
