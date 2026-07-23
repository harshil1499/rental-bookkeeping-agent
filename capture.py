#!/usr/bin/env python3
"""
capture.py — multi-source expense/income capture into the property P&L sheets.

The deterministic writer. Natural-language parsing ("add one fifty for the cleaner")
is done upstream (by an assistant); this script takes a *structured* entry, validates
it against the property's chart of accounts, and appends one row to the correct month
tab so the Summary's full-column SUMIFs roll it up automatically.

Property aliases, sheet names, and credentials come from config.py (gitignored; see
config.example.py). No property-identifying data lives in this file.

Examples
--------
Dry run (shows exactly what would be written, touches nothing):
    python3 capture.py --property lakehouse --amount 150 \
        --payee "Cleaner (Venmo)" --category "Cleaning and Maintenance" --dry-run

Book it for real (writes to the live sheet):
    python3 capture.py --property lakehouse --amount 150 \
        --payee "Cleaner (Venmo)" --category "Cleaning and Maintenance"

Income:
    python3 capture.py --property cabin --amount 417.81 --payee "STR PAYOUT" --income
"""
import argparse
import sys
import warnings
from datetime import date as _date, datetime

warnings.filterwarnings("ignore")

import gspread
from google.oauth2.service_account import Credentials

import config

CREDS_FILE = config.CREDS_FILE
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# ---- Property -> Google Sheet (from config.py) ----
PROPERTY_SHEETS = config.PROPERTY_SHEETS
NOT_YET = config.NOT_YET

# ---- Valid categories (the chart of accounts, unioned across all property sheets) ----
# A category is valid if it matches the destination sheet's Summary label. Some sheets use
# "Insurance - I don't use this"; others use plain "Insurance" plus "Legal and Other
# Professional Fees" and "Taxes".
EXPENSE_CATEGORIES = {
    "Advertising", "Auto and Travel", "Cleaning and Maintenance", "Commissions",
    "Insurance - I don't use this", "Insurance", "Legal and Other Professional Fees",
    "Software", "Management Fees", "Mortgage Interest", "Permits & Fees", "Repairs",
    "Supplies", "Escrow (Taxes + Insurance)", "Taxes", "Utilities",
    "Depreciation Expense or Depletion", "Other",
}
INCOME_CATEGORIES = {"Rental Income", "Other"}

# Keyword -> category hints (used only to *suggest*; the assistant normally passes
# --category explicitly). Longest/most-specific matches should be listed first.
CATEGORY_HINTS = [
    ("mortgage interest", "Mortgage Interest"),
    (("escrow", "property tax", "prop tax"), "Escrow (Taxes + Insurance)"),
    (("permit", "license", "registration", "occupancy tax"), "Permits & Fees"),
    (("ownerrez", "pricelabs", "software", "subscription", "saas", "hospitable"), "Software"),
    (("management fee", "pm fee", "property manager"), "Management Fees"),
    (("clean", "turnover", "maid", "laundry", "snow plow", "lawn", "weed", "landscap"), "Cleaning and Maintenance"),
    (("repair", "fix", "plumber", "hvac", "handyman", "dishwasher", "appliance"), "Repairs"),
    (("water", "electric", "elec", "gas", "internet", "wifi", "garbage", "trash", "sewer",
      "utility", "utilities", *config.UTILITY_VENDOR_LINES), "Utilities"),
    (("supplies", "paper towel", "detergent", "toiletries", "soap", "propane", "consumable", "restock"), "Supplies"),
    (("advertis", "marketing"), "Advertising"),
    (("travel", "mileage", "airfare", "flight"), "Auto and Travel"),
    (("commission",), "Commissions"),
]
INCOME_HINTS = ("airbnb", "vrbo", "booking", "payout", "rent", "guest")


def resolve_property(raw):
    key = raw.strip().lower()
    if key in PROPERTY_SHEETS:
        return PROPERTY_SHEETS[key]
    for k, sheet in PROPERTY_SHEETS.items():
        if k in key:
            return sheet
    for k in NOT_YET:
        if k in key:
            sys.exit(f"'{NOT_YET[k]}' isn't wired up yet (needs its account # + sheet).")
    sys.exit(f"Unknown property '{raw}'. Use: {config.PROPERTY_HELP}.")


def suggest_category(text, income=False):
    t = (text or "").lower()
    if income:
        return "Rental Income"
    for keys, cat in CATEGORY_HINTS:
        keys = (keys,) if isinstance(keys, str) else keys
        if any(k in t for k in keys):
            return cat
    return None


def month_tab(d):
    return d.strftime("%B")


def open_sheet(sheet_name):
    creds = Credentials.from_service_account_file(CREDS_FILE, scopes=SCOPES)
    return gspread.authorize(creds).open(sheet_name)


def find_income_block_end(rows):
    """Row index (1-based) of the 'Fixed Expenses' header, or None."""
    for i, r in enumerate(rows, 1):
        if len(r) > 1 and str(r[1]).strip().lower() == "fixed expenses":
            return i
    return None


def last_content_row(rows):
    """Last 1-based row with a Payee (col B) or Amount (col C) — ignores category-only
    ghost rows (which have only col E populated)."""
    last = 1
    for i, r in enumerate(rows, 1):
        b = str(r[1]).strip() if len(r) > 1 else ""
        c = str(r[2]).strip() if len(r) > 2 else ""
        if b or c:
            last = i
    return last


def parse_money(v):
    """'$1,126.00' / '976' / '' -> float."""
    s = str(v or "").replace("$", "").replace(",", "").strip()
    return float(s) if s else 0.0


def find_named_line(rows, label):
    """1-based row whose Payee (col B) matches `label` (exact case-insensitive preferred,
    else a unique startswith). Used to accumulate into an existing line like 'Cleaning'."""
    ll = label.strip().lower()
    exact = [i for i, r in enumerate(rows, 1) if len(r) > 1 and str(r[1]).strip().lower() == ll]
    if exact:
        return exact[0]
    starts = [i for i, r in enumerate(rows, 1) if len(r) > 1 and str(r[1]).strip().lower().startswith(ll)]
    return starts[0] if len(starts) == 1 else None


def main():
    ap = argparse.ArgumentParser(description="Capture one income/expense entry into a property P&L.")
    ap.add_argument("--property", "-p", required=True, help=config.PROPERTY_HELP)
    ap.add_argument("--amount", "-a", required=True, type=float, help="positive dollar amount")
    ap.add_argument("--payee", "-n", required=True, help="payee / description")
    ap.add_argument("--category", "-c", help="exact category; omit to auto-suggest from payee")
    ap.add_argument("--income", action="store_true", help="book as Rental Income instead of an expense")
    ap.add_argument("--accumulate-line", help="add to an existing named line's total instead of "
                                              "making a new row (e.g. 'Cleaning'). No new row is created.")
    ap.add_argument("--date", "-d", help="YYYY-MM-DD (default: today)")
    ap.add_argument("--no-date", action="store_true", help="leave the Date cell blank (match existing lines)")
    ap.add_argument("--dry-run", action="store_true", help="show what would be written; touch nothing")
    args = ap.parse_args()

    if args.amount <= 0:
        sys.exit("Amount must be a positive number (expenses are stored positive; the Summary nets them).")

    d = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else _date.today()
    sheet_name = resolve_property(args.property)
    tab = month_tab(d)

    # Category
    if args.category:
        category = args.category
    else:
        category = suggest_category(args.payee, income=args.income)
        if not category:
            sys.exit(f"Couldn't auto-categorize '{args.payee}'. Re-run with --category \"<one of your categories>\".")

    valid = INCOME_CATEGORIES if args.income else EXPENSE_CATEGORIES
    if category not in valid:
        sys.exit(f"'{category}' isn't a valid {'income' if args.income else 'expense'} category.\n"
                 f"Valid: {sorted(valid)}")

    # ---- Accumulate mode: bump an existing named line (e.g. Cleaning), no new row ----
    if args.accumulate_line:
        ss = open_sheet(sheet_name)
        ws = ss.worksheet(tab)
        rows = ws.get_all_values()  # not get() — get() returned ghost rows on these sheets
        line_row = find_named_line(rows, args.accumulate_line)
        if not line_row:
            sys.exit(f"No line named '{args.accumulate_line}' found in {sheet_name} / {tab}. "
                     f"Book it as a variable row instead (drop --accumulate-line).")
        current = parse_money(rows[line_row - 1][2] if len(rows[line_row - 1]) > 2 else "")
        new_total = round(current + args.amount, 2)
        label = str(rows[line_row - 1][1]).strip()
        print(f"\n  ACCUMULATE into '{label}' ({category})")
        print(f"  Property: {args.property}  ({sheet_name})   Month tab: {tab}")
        print(f"  ${current:,.2f} + ${args.amount:,.2f}  =  ${new_total:,.2f}   (row {line_row})")
        if args.dry_run:
            print(f"  [DRY RUN] Would set {tab}!C{line_row} = {new_total} — nothing changed.\n")
            return
        ws.update(f"C{line_row}", [[new_total]], value_input_option="USER_ENTERED")
        print(f"  ✓ {label} total is now ${new_total:,.2f} ({tab}!C{line_row}).\n")
        return

    date_cell = "" if args.no_date else d.strftime("%-m/%-d/%Y")

    # Build the row: A=Date, B=Payee, C=Amount, D=Revenue Cat, E=Expense Cat
    if args.income:
        row = [date_cell, args.payee, args.amount, "Rental Income", ""]
    else:
        row = [date_cell, args.payee, args.amount, "", category]

    kind = "INCOME" if args.income else "EXPENSE"
    print(f"\n  {kind}: ${args.amount:,.2f}  →  {category}")
    print(f"  Property: {args.property}  ({sheet_name})")
    print(f"  Month tab: {tab}   Payee: {args.payee}   Date: {date_cell or '(blank)'}")

    if args.dry_run:
        ss = open_sheet(sheet_name)
        ws = ss.worksheet(tab)
        rows = ws.get_all_values()  # not get() — get() returned ghost rows on these sheets
        if args.income:
            end = find_income_block_end(rows)
            target = None
            for i in range(1, (end or len(rows) + 1)):
                r = rows[i - 1] if i - 1 < len(rows) else []
                dcat = str(r[3]).strip() if len(r) > 3 else ""
                camt = str(r[2]).strip() if len(r) > 2 else ""
                if dcat == "Rental Income" and not camt:
                    target = i
                    break
            target = target or (last_content_row(rows) + 1)
        else:
            target = last_content_row(rows) + 1
        print(f"  [DRY RUN] Would write to {tab}!A{target}:E{target} — nothing changed.\n")
        return

    # ---- Real write ----
    ss = open_sheet(sheet_name)
    ws = ss.worksheet(tab)
    rows = ws.get_all_values()  # not get() — get() returned ghost rows on these sheets

    if args.income:
        end = find_income_block_end(rows)
        target = None
        for i in range(2, (end or len(rows) + 1)):
            r = rows[i - 1] if i - 1 < len(rows) else []
            dcat = str(r[3]).strip() if len(r) > 3 else ""
            camt = str(r[2]).strip() if len(r) > 2 else ""
            if dcat == "Rental Income" and not camt:
                target = i
                break
        target = target or (last_content_row(rows) + 1)
    else:
        target = last_content_row(rows) + 1

    ws.update(f"A{target}:E{target}", [row], value_input_option="USER_ENTERED")
    print(f"  ✓ Booked to {tab}!A{target}:E{target}. Summary will roll it up automatically.\n")


if __name__ == "__main__":
    main()
