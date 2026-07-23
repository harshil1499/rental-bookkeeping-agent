#!/usr/bin/env python3
"""
import_relay.py — Phase 1b: Relay CSV -> categorized, reconciled staging.

Source: a Google Drive **inbox** folder (default). Drop any property's Relay export in;
routing is by the #account number in the filename, so one folder serves all properties.
The CSV never writes into your hand-maintained register — it's parsed, categorized against
your chart of accounts, reconciled against each month tab, and written to an 'Import'
staging tab for review. On a real run, each processed file is moved to the 'Done/' subfolder
so it's never imported twice.

Property routing is by the bank-account #number in the CSV filename (see ACCOUNT_SHEETS in
config.py). Because it keys on the account number, two properties can never cross-contaminate.

Usage
-----
Preview from the Drive inbox (default; touches nothing, does NOT move files):
    python3 import_relay.py
Process for real (writes staging tabs, then moves files to Done/):
    python3 import_relay.py --write
Use the local ./data folder instead of Drive (testing):
    python3 import_relay.py --local
"""
import argparse
import csv
import glob
import io
import json
import os
import re
import sys
import warnings
from datetime import datetime, date

warnings.filterwarnings("ignore")

import fitz  # pymupdf, for mortgage-statement PDFs
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

import mortgage
import appfolio
import config
from capture import open_sheet, suggest_category, parse_money  # shared normalized path

CREDS_FILE = config.CREDS_FILE
DRIVE_CONFIG = config.DRIVE_CONFIG
FOLDER_MIME = "application/vnd.google-apps.folder"
GSHEET_MIME = "application/vnd.google-apps.spreadsheet"
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]

# Bank account number -> (destination sheet, label). From config.py.
ACCOUNT_SHEETS = config.ACCOUNT_SHEETS

# Loan servicers in the bank feed: the raw draft is combined principal+interest. The real
# split comes from the mortgage-statement PDFs (see mortgage.py); a bare draft with no
# matching statement is flagged so it's never booked at the full amount.
MORTGAGE_PAYEES = config.MORTGAGE_PAYEES

# Owner draws / internal movements between the owner, bank, and a property manager — not P&L
# items. For a PM-managed property, funds moving to/from the PM reserve are transfers, not
# expenses; the real income/expenses come off the PM's owner statement (see appfolio.py).
TRANSFER_PAYEES = config.TRANSFER_PAYEES

STAGING_TAB = "Import"
STAGING_HEADER = ["Date", "Payee", "Amount", "Type", "Suggested Category", "Status", "Note"]


def sort_key(r):
    """Income before Expense, then by date."""
    return (0 if r["type"] == "Income" else 1, r["date_obj"])


_MONTHS = ["January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December"]
MONTH_ORDER = {m: i for i, m in enumerate(_MONTHS)}


def month_blocks(rows):
    """Rows grouped into chronological month blocks; income before expenses within each."""
    by = {}
    for r in rows:
        by.setdefault(r["month"], []).append(r)
    return [sorted(by[m], key=sort_key)
            for m in sorted(by, key=lambda x: MONTH_ORDER.get(x, 99))]


# ---------------- Drive I/O ----------------

def drive_service():
    creds = Credentials.from_service_account_file(CREDS_FILE, scopes=DRIVE_SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def load_drive_config():
    if not os.path.exists(DRIVE_CONFIG):
        sys.exit(f"No {DRIVE_CONFIG}. Run:  python3 drive_setup.py")
    with open(DRIVE_CONFIG) as f:
        return json.load(f)


def list_inbox_files(svc, inbox_id):
    """Non-folder files in the inbox, tagged 'csv' or 'pdf' (others ignored)."""
    q = f"'{inbox_id}' in parents and trashed = false and mimeType != '{FOLDER_MIME}'"
    res = svc.files().list(q=q, fields="files(id, name, mimeType)", spaces="drive").execute()
    out = []
    for f in res.get("files", []):
        name = f["name"].lower()
        if name.endswith(".csv") or f["mimeType"] in ("text/csv", GSHEET_MIME):
            out.append({**f, "kind": "csv"})
        elif name.endswith(".pdf") or f["mimeType"] == "application/pdf":
            out.append({**f, "kind": "pdf"})
    return out


def download_csv_text(svc, file_meta):
    if file_meta["mimeType"] == GSHEET_MIME:
        data = svc.files().export_media(fileId=file_meta["id"], mimeType="text/csv").execute()
    else:
        data = svc.files().get_media(fileId=file_meta["id"]).execute()
    return data.decode("utf-8-sig") if isinstance(data, bytes) else data


def download_pdf_text(svc, file_id):
    data = svc.files().get_media(fileId=file_id).execute()
    doc = fitz.open(stream=data, filetype="pdf")
    return "\n".join(page.get_text() for page in doc)


def move_to_done(svc, file_id, done_id):
    # A same-name file may already sit in Done (a prior re-add). That's harmless: sources are
    # deduped by filename at read time, so a duplicate in Done is never processed twice. We
    # don't trash it — the service account can't trash user-owned files (403).
    meta = svc.files().get(fileId=file_id, fields="parents").execute()
    prev = ",".join(meta.get("parents", []))
    svc.files().update(fileId=file_id, addParents=done_id, removeParents=prev, fields="id").execute()


# ---------------- Parse / categorize ----------------

def account_of(name):
    m = re.search(r"#(\d+)", name)
    return m.group(1) if m else None


def parse_relay_text(text):
    """CSV text -> list of normalized dicts."""
    out = []
    for row in csv.DictReader(io.StringIO(text)):
        amt_raw = (row.get("Amount") or "").strip()
        if not amt_raw:
            continue
        try:
            amount = float(amt_raw.replace("$", "").replace(",", "").replace("+", "").strip())
        except ValueError:
            continue
        date_str = (row.get("Date") or "").strip()
        try:
            date_obj = datetime.strptime(date_str, "%m/%d/%Y")
        except ValueError:
            continue
        payee = (row.get("Payee") or row.get("Description") or "").strip()
        status = (row.get("Status") or "").strip().upper()
        out.append({
            "date_obj": date_obj, "date_str": date_str, "payee": payee, "amount": amount,
            "type": "Income" if amount > 0 else "Expense",
            "note": "" if status in ("", "SETTLED") else f"status: {status.title()}",
        })
    return out


def categorize(txn):
    payee_l = txn["payee"].lower()
    if any(t in payee_l for t in TRANSFER_PAYEES):
        return "Transfer (exclude)", "owner transfer — not a P&L item; drop when promoting"
    if any(m in payee_l for m in MORTGAGE_PAYEES):
        return "Mortgage Interest", "raw bank draft — use the statement's interest split, not this full amount"
    if txn["amount"] > 0:
        return "Rental Income", ""
    cat = suggest_category(txn["payee"], income=False)
    if not cat:
        return "Other", "uncategorized — set category before promoting"
    return cat, ""


def csv_entries(txns):
    """Relay transactions -> pre-categorized booking rows."""
    out = []
    for t in txns:
        cat, note = categorize(t)
        out.append({**t, "category": cat, "source": "relay",
                    "note": "; ".join(n for n in (t["note"], note) if n)})
    return out


def suppress_superseded(rows):
    """Where a mortgage *statement* split exists for a property+month, mark the raw Relay
    P&I draft for that month superseded so the full amount can't be promoted alongside it."""
    covered = {r["month"] for r in rows
               if r.get("source") == "statement" and r["category"] == "Mortgage Interest"}
    for r in rows:
        if (r.get("source") == "relay" and r["month"] in covered
                and any(m in r["payee"].lower() for m in MORTGAGE_PAYEES)):
            r["category"] = "Mortgage draft (P&I)"
            r["status"] = "superseded"
            r["note"] = "covered by the statement's interest+escrow split — do NOT promote"


def hold_unstatemented_drafts(rows):
    """A raw Relay mortgage draft with no statement to split it must NOT be booked at the
    full P&I amount — that would overstate Mortgage Interest and skip the principal exclusion.
    Hold it (promote skips 'hold') until the servicer statement lands and supersedes it.
    Covers any month whose mortgage statement hasn't been supplied yet."""
    covered = {r["month"] for r in rows
               if r.get("source") == "statement" and r["category"] == "Mortgage Interest"}
    for r in rows:
        if (r.get("source") == "relay" and r["category"] == "Mortgage Interest"
                and r["month"] not in covered and r.get("status") != "superseded"):
            r["category"] = "Mortgage draft (P&I)"
            r["status"] = "hold"
            r["note"] = ("no statement yet to split interest/escrow — add the servicer "
                         "statement to the inbox, then re-import to book it")


def existing_amounts(sheet, month):
    try:
        ws = sheet.worksheet(month)
    except Exception:
        return set()
    amts = set()
    for r in ws.get("C1:C300"):
        if not (r and str(r[0]).strip()):
            continue
        try:
            amts.add(round(abs(parse_money(r[0])), 2))
        except ValueError:
            continue
    return amts


def reconcile_status(txn, existing):
    return "maybe-entered" if round(abs(txn["amount"]), 2) in existing else "NEW"


def finalize(sheet_name, label, sheet, entries, recon_cache):
    """Attach month + reconciliation status to pre-categorized entries."""
    rows = []
    for e in entries:
        month = e["date_obj"].strftime("%B")
        key = (sheet_name, month)
        if key not in recon_cache:
            recon_cache[key] = existing_amounts(sheet, month)
        rows.append({**e, "month": month, "status": reconcile_status(e, recon_cache[key])})
    return {"sheet_name": sheet_name, "label": label, "sheet": sheet, "rows": rows}


# ---------------- Output ----------------

def print_preview(group):
    print(f"\n=== {group['sheet_name']}  ({group['label']}) ===")
    print(f"{'Month':<6} {'Date':<11} {'Amount':>10} {'Type':<8} {'Category':<24} {'Status':<13} Payee")
    print("-" * 100)
    counts = {}
    for bi, block in enumerate(month_blocks(group["rows"])):
        if bi > 0:
            print()  # blank line between months
        for r in block:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
            note = f"  [{r['note']}]" if r["note"] else ""
            print(f"{r['month']:<6} {r['date_str']:<11} {r['amount']:>+10.2f} {r['type']:<8} "
                  f"{r['category']:<24} {r['status']:<13} {r['payee']}{note}")
    tally = ", ".join(f"{n} {s}" for s, n in sorted(counts.items(), key=lambda x: -x[1]))
    print(f"  -> {tally}  ({len(group['rows'])} total)")


def read_existing_stamps(sheet):
    """Preserve per-row Skip (col H) and Promoted (col I) marks across a tab rebuild.

    write_staging deletes and recreates the Import tab, which would otherwise wipe both the
    user's manual Skip opt-outs AND promote.py's '✓ promoted' idempotency stamps. Losing the
    promoted stamp is the dangerous one: a re-import after a promote would make booked rows
    look un-booked, leaving only amount-reconciliation to prevent a double-book. So we snapshot
    those marks keyed by (date, payee, amount) — stable across deterministic re-imports — and
    re-apply them after the rebuild. Returns {} if there's no existing Import tab.
    """
    try:
        ws = sheet.worksheet(STAGING_TAB)
    except Exception:
        return {}
    stamps = {}
    for g in ws.get_all_values()[1:]:  # get_all_values (not get); skip header
        date_s, payee, amount, _t, _c, _s, _n, skip, promoted = (g + [""] * 9)[:9]
        if not (str(payee).strip() or str(amount).strip()):
            continue  # blank spacer
        if not (str(skip).strip() or str(promoted).strip()):
            continue  # nothing to preserve on this row
        try:
            amt = round(abs(parse_money(amount)), 2)
        except ValueError:
            continue
        stamps[(str(date_s).strip(), str(payee).strip(), amt)] = \
            (str(skip).strip(), str(promoted).strip())
    return stamps


def write_staging(group):
    sheet = group["sheet"]
    old_stamps = read_existing_stamps(sheet)  # snapshot before the tab is rebuilt
    blank = [""] * len(STAGING_HEADER)
    body = [STAGING_HEADER]
    hi_updates = []  # (row_number, skip, promoted) marks to re-apply after rebuild
    for bi, block in enumerate(month_blocks(group["rows"])):
        if bi > 0:
            body.append(blank)  # spacer row between months
        for r in block:
            body.append([r["date_str"], r["payee"], r["amount"], r["type"],
                         r["category"], r["status"], r["note"]])
            keep = old_stamps.get((str(r["date_str"]).strip(), str(r["payee"]).strip(),
                                   round(abs(r["amount"]), 2)))
            if keep:
                hi_updates.append((len(body), keep[0], keep[1]))  # len(body) == this row's number
    try:
        sheet.del_worksheet(sheet.worksheet(STAGING_TAB))
    except Exception:
        pass
    ws = sheet.add_worksheet(title=STAGING_TAB, rows=len(body) + 10, cols=9)  # +Skip(H)/Promoted(I)
    ws.update(f"A1:G{len(body)}", body, value_input_option="USER_ENTERED")
    ws.update("H1:I1", [["Skip", "Promoted"]], value_input_option="USER_ENTERED")
    if hi_updates:  # restore preserved Skip/Promoted marks
        ws.batch_update([{"range": f"H{row}:I{row}", "values": [[skip, promoted]]}
                         for row, skip, promoted in hi_updates],
                        value_input_option="USER_ENTERED")
    kept = f"  (preserved {len(hi_updates)} Skip/Promoted mark(s))" if hi_updates else ""
    print(f"  ✓ Wrote {len(group['rows'])} rows to '{group['sheet_name']}' -> tab '{STAGING_TAB}'.{kept}")


# ---------------- Main ----------------

def gather_sources(args):
    """-> (sources, svc, cfg). sources = list of {name, kind, text, file_id?}."""
    if args.local:
        files = args.files or (sorted(glob.glob(os.path.join(config.LOCAL_DATA_DIR, "Relay*.csv")))
                               + sorted(glob.glob(os.path.join(config.LOCAL_DATA_DIR, "*.pdf"))))
        if not files:
            sys.exit("No local CSVs/PDFs found in ./data/.")
        srcs = []
        for p in files:
            if p.lower().endswith(".pdf"):
                with open(p, "rb") as f:
                    doc = fitz.open(stream=f.read(), filetype="pdf")
                text = "\n".join(page.get_text() for page in doc)
                srcs.append({"name": os.path.basename(p), "kind": "pdf",
                             "text": text, "file_id": None, "movable": False})
            else:
                with open(p, encoding="utf-8-sig") as f:
                    srcs.append({"name": os.path.basename(p), "kind": "csv",
                                 "text": f.read(), "file_id": None, "movable": False})
        return srcs, None, None

    # The Import view is built from the full known dataset (inbox + Done); only inbox files
    # are movable. Dedupe by filename — inbox copy wins over an already-filed Done copy.
    cfg = load_drive_config()
    svc = drive_service()
    picked = {}  # name -> (meta, movable)
    for m in list_inbox_files(svc, cfg["done_folder_id"]):
        picked[m["name"]] = (m, False)
    for m in list_inbox_files(svc, cfg["inbox_folder_id"]):
        picked[m["name"]] = (m, True)
    if not picked:
        sys.exit("Drive inbox is empty — drop Relay CSVs / mortgage PDFs into 'Relay Imports'.")
    srcs = []
    for name, (m, movable) in picked.items():
        text = download_pdf_text(svc, m["id"]) if m["kind"] == "pdf" else download_csv_text(svc, m)
        srcs.append({"name": name, "kind": m["kind"], "text": text,
                     "file_id": m["id"], "movable": movable})
    return srcs, svc, cfg


def main():
    ap = argparse.ArgumentParser(description="Relay CSV -> categorized, reconciled staging.")
    ap.add_argument("files", nargs="*", help="(with --local) specific CSV paths")
    ap.add_argument("--write", action="store_true",
                    help="write 'Import' staging tabs, then move Drive files to Done/")
    ap.add_argument("--rebuild", action="store_true",
                    help="write the 'Import' tabs but move no files (default already reads inbox + Done/)")
    ap.add_argument("--local", action="store_true", help="read ./data instead of the Drive inbox")
    args = ap.parse_args()

    sources, svc, cfg = gather_sources(args)
    sheet_cache, recon_cache = {}, {}
    groups, processed = {}, []   # processed = (name, file_id) for movable (inbox) sources
    draft_months = {}            # sheet_name -> {months with a Relay mortgage draft on record}
    seen_stmt = set()            # (sheet, month) statements already staged — dedupe

    def group_for(sheet_name, label):
        sheet = sheet_cache.get(sheet_name) or open_sheet(sheet_name)
        sheet_cache[sheet_name] = sheet
        return groups.setdefault(sheet_name, {"sheet_name": sheet_name, "label": label,
                                              "sheet": sheet, "rows": []}), sheet

    # Pass 1 — Relay CSVs (inbox + Done): stage rows, and learn which months have a
    # mortgage draft on record (across the whole dataset, not just this batch).
    for s in (x for x in sources if x["kind"] == "csv"):
        acct = account_of(s["name"])
        if acct not in ACCOUNT_SHEETS:
            print(f"  ! Skipping '{s['name']}' — no known Relay account # in the filename.")
            continue
        sheet_name, label = ACCOUNT_SHEETS[acct]
        entries = csv_entries(parse_relay_text(s["text"]))
        g, sheet = group_for(sheet_name, label)
        g["rows"].extend(finalize(sheet_name, label, sheet, entries, recon_cache)["rows"])
        for e in entries:
            if any(m in e["payee"].lower() for m in MORTGAGE_PAYEES):
                draft_months.setdefault(sheet_name, set()).add(e["date_obj"].strftime("%B"))
        if s["movable"]:
            processed.append((s["name"], s["file_id"]))

    # Pass 2 — statement PDFs. Try the mortgage parser first; if it isn't a mortgage statement,
    # try the AppFolio owner-statement parser (Indy LTR). AppFolio rows stage directly — they
    # carry their own income/expense detail and don't gate on a Relay draft the way mortgages do.
    for s in (x for x in sources if x["kind"] == "pdf"):
        parsed = mortgage.parse_mortgage_pdf(s["text"])
        if not parsed:
            af = appfolio.parse_appfolio_pdf(s["text"])
            if af:
                ok, msg = appfolio.reconcile(af)
                g, sheet = group_for(af["sheet"], af["label"])
                g["rows"].extend(finalize(af["sheet"], af["label"], sheet,
                                          appfolio.entries_from(af), recon_cache)["rows"])
                flag = "" if ok else "  ⚠ CHECK"
                print(f"  · AppFolio '{s['name']}' → {af['label']}: {msg}{flag}")
                if s["movable"]:
                    processed.append((s["name"], s["file_id"]))
            else:
                print(f"  ! Skipping '{s['name']}' — not a recognized mortgage or AppFolio statement.")
            continue
        key = (parsed["sheet"], parsed["month"])
        if parsed["month"] not in draft_months.get(parsed["sheet"], set()):
            print(f"  · Holding '{s['name']}' — {parsed['month']} has no matching bank draft; "
                  f"left in inbox until its Relay CSV arrives.")
            continue
        if key not in seen_stmt:  # skip a duplicate statement for the same month
            seen_stmt.add(key)
            g, sheet = group_for(parsed["sheet"], parsed["label"])
            g["rows"].extend(finalize(parsed["sheet"], parsed["label"], sheet,
                                      mortgage.entries_from(parsed), recon_cache)["rows"])
        if s["movable"]:
            processed.append((s["name"], s["file_id"]))

    if not groups:
        sys.exit("Nothing to process.")

    for g in groups.values():
        suppress_superseded(g["rows"])
        hold_unstatemented_drafts(g["rows"])
        print_preview(g)

    if not (args.write or args.rebuild):
        where = "./data" if args.local else "the Drive inbox"
        print(f"\n(Preview only — nothing written, no files moved. Re-run with --write to "
              f"create the 'Import' tabs and clear {where}.)")
        return

    print("\nWriting staging tabs...")
    for g in groups.values():
        write_staging(g)

    if args.rebuild:
        print("(Rebuild — Import tabs re-rendered; no files moved.)")
        return

    if svc and cfg:
        print("Moving processed inbox files to Done/ ...")
        for name, fid in processed:
            if not fid:
                continue
            try:
                move_to_done(svc, fid, cfg["done_folder_id"])
                print(f"  ✓ {name} -> Done/")
            except Exception as e:
                print(f"  ! couldn't move {name}: {str(e)[:90]}")


if __name__ == "__main__":
    main()
