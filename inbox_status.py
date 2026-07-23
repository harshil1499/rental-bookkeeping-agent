#!/usr/bin/env python3
"""
inbox_status.py — read-only snapshot of the Drive 'Relay Imports' inbox, classified by
property. Powers the monthly bookkeeping reminder: shows what's staged and ready to book,
what's held (statement present but its matching Relay draft/CSV isn't), and what's missing.

Writes nothing, moves nothing — safe to run any time (including from a scheduled agent).

    python3 inbox_status.py
"""
import re
import warnings; warnings.filterwarnings("ignore")

import mortgage
import appfolio
import config
import email_docs
from import_relay import (drive_service, load_drive_config, list_inbox_files,
                          download_pdf_text, account_of, ACCOUNT_SHEETS)

# Property sheet -> display label (from config.py).
PROPS = config.INBOX_PROPS
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December"]


def scan(svc=None, cfg=None):
    """Read-only bucketing of the Drive inbox by property.

    Shared by this CLI snapshot and preview_email.py so both describe "what's still needed"
    from the same logic. Returns {by_prop, csv_months, unknown, n_files}, where a statement is
    'held' when its month has no matching Relay CSV on hand.
    """
    cfg = cfg or load_drive_config()
    svc = svc or drive_service()
    # Both input paths count as "arrived": files in the Drive inbox, and documents attached to
    # an email reply. Drive wins a filename collision.
    files = [{"name": f["name"], "kind": f["kind"], "id": f["id"], "text": None}
             for f in list_inbox_files(svc, cfg["inbox_folder_id"])]
    have = {f["name"] for f in files}
    files += [{"name": s["name"], "kind": s["kind"], "id": None, "text": s["text"]}
              for s in email_docs.fetch() if s["name"] not in have]

    by_prop = {p: {"csv_months": set(), "statements": [], "appfolio": False} for p in PROPS}
    csv_months = {}   # sheet -> set of month names present as a Relay CSV
    unknown = []

    for f in files:
        if f["kind"] == "csv":
            acct = account_of(f["name"])
            if acct in ACCOUNT_SHEETS:
                sheet = ACCOUNT_SHEETS[acct][0]
                # month from filename "Relay YYYY-MM-01 #acct.csv" if present
                m = re.search(r"(\d{4})-(\d{2})", f["name"])
                mon = MONTHS[int(m.group(2)) - 1] if m else None
                by_prop[sheet]["csv_months"].add(mon or f["name"])
                csv_months.setdefault(sheet, set()).add(mon)
            else:
                unknown.append(f["name"])
            continue
        # PDF: mortgage statement, AppFolio, or other
        text = f["text"] if f["text"] is not None else download_pdf_text(svc, f["id"])
        mp = mortgage.parse_mortgage_pdf(text)
        if mp:
            by_prop[mp["sheet"]]["statements"].append((mp["month"], mp["amount"], f["name"]))
            continue
        af = appfolio.parse_appfolio_pdf(text)
        if af:
            by_prop[af["sheet"]]["appfolio"] = True
            continue
        unknown.append(f["name"])

    return {"by_prop": by_prop, "csv_months": csv_months,
            "unknown": unknown, "n_files": len(files)}


def main():
    s = scan()
    by_prop, csv_months, unknown, n_files = (
        s["by_prop"], s["csv_months"], s["unknown"], s["n_files"])

    print("=" * 68)
    print(" RENTAL BOOKKEEPING — Drive inbox snapshot")
    print("=" * 68)
    if not n_files:
        print("\n  Inbox is EMPTY. Nothing to book — upload this month's Relay CSVs +\n"
              "  mortgage statements (+ AppFolio export for Indy) when they're ready.\n")
        return

    for sheet, label in PROPS.items():
        b = by_prop[sheet]
        csvs = sorted(x for x in b["csv_months"] if x)
        print(f"\n▸ {label}")
        print(f"    Relay CSVs present : {', '.join(csvs) if csvs else '— none —'}")
        if sheet == config.INBOX_APPFOLIO_SHEET:
            print(f"    AppFolio export    : {'yes' if b['appfolio'] else '— missing —'}")
        if b["statements"]:
            drafted = csv_months.get(sheet, set())
            for mon, amt, name in sorted(b["statements"]):
                ready = mon in drafted
                flag = "READY (has matching CSV)" if ready else "HELD — needs its Relay CSV"
                print(f"    Mortgage stmt {mon:<9} ${amt:<9} → {flag}")
        else:
            print(f"    Mortgage stmt      : — none —")

    if unknown:
        print(f"\n  Unrecognized files (ignored): {', '.join(unknown)}")

    print("\n" + "-" * 68)
    print("  To book: python3 import_relay.py --write  →  review Import tab  →")
    print("           python3 promote.py   (dry-run, then --write)")
    print("  A mortgage statement only books once its month's Relay CSV is present.")
    print("-" * 68 + "\n")


if __name__ == "__main__":
    main()
