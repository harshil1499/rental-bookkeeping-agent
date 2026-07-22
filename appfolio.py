#!/usr/bin/env python3
"""
appfolio.py — parse an AppFolio Owner Portal "Transactions" PDF into booking records.

This is the LTR analogue of mortgage.py, for a professionally-managed long-term rental
whose PM uses AppFolio. For that property the PM's owner statement — not the bank feed — is
the source of truth for income and operating expenses. The bank feed only carries the
mortgage draft plus owner transfers (see import_relay.py). Property/PM specifics come from
config.py (APPFOLIO_* settings).

Why the statement, not Relay: money reaches the property through many channels
(Chase→vendor, Chase→Relay→PM, Chase→PM), and the owner funds a reserve the PM spends
from. Booking off bank debits would mis-time deductions and double-count (one economic
event shows on two ledgers). So we classify by *what the PM actually did*:

    Contribution (owner -> PM reserve)      -> Transfer (exclude)   [your own cash moving]
    Owner Disbursement (PM -> owner/Relay)  -> Transfer (exclude)   [surplus already earned]
    Cash Out -> Management LLC              -> Management Fees
    Cash Out -> maintenance vendor          -> Cleaning and Maintenance / Repairs (by desc)
    Cash In  -> rent                        -> Rental Income
    Cash In  -> security deposit            -> REVIEW (returnable = exclude; forfeited = income)

The export is a running year-to-date ledger, so it's safe to re-import: import_relay
rewrites the Import tab each run and promote.py stamps promoted rows, so re-runs don't
double-book. Cash basis — the statement's "Unpaid Bills" section is intentionally ignored
(an accrued bill books only when it's actually paid, as a later Cash Out).
"""
import re
from datetime import datetime

import config

# Which sheet this statement feeds, detected by the property address on the rows (config.py).
PROPERTY_MATCH = config.APPFOLIO_PROPERTY_MATCH

TXN_TYPES = ("Contribution", "Cash Out", "Cash In", "Owner Disbursement")

# Description/party keyword -> (category, type, note). First match wins; order matters.
# type "Expense"/"Income" book normally; "Review" is flagged for attention (never auto-booked).
RULES = [
    # PM management fee (party is the management company itself)
    (("management fee",), ("Management Fees", "Expense", "")),
    # Make-ready / turnover / inspection-driven repairs
    (("turn ", "turnover", "make ready", "make-ready", "fail list", "condition report",
      "repair", "replace", "install", "hvac", "plumb", "appliance", "furnace", "roof"),
     ("Repairs", "Expense", "")),
    # Routine grounds/upkeep
    (("lawn", "mow", "grass", "landscap", "snow", "clean", "pest", "gutter", "tree"),
     ("Cleaning and Maintenance", "Expense", "")),
]


def _money(s):
    return float(s.replace(",", "").replace("$", "").replace("+", ""))


def _find_summary(flat):
    """Pull the statement's own Total Cash In / Cash Out checksums, if present."""
    def grab(label):
        m = re.search(label + r'\s*-?\$?([\d,]+\.\d{2})', flat, re.I)
        return _money(m.group(1)) if m else None
    return {"total_cash_in": grab(r'Total Cash In'), "cash_out": grab(r'Cash Out')}


def _strip_boilerplate(text):
    """Remove page headers/footers that AppFolio's print-to-PDF injects at page breaks,
    so they can't bleed into a transaction's payee/description text."""
    # DOTALL throughout: fitz and pypdf wrap the footer across newlines differently, so the
    # '.' must cross lines to catch either. Non-greedy stops at the first anchor each time.
    text = re.sub(r'\d{1,2}/\d{1,2}/\d{2,4},\s*\d{1,2}:\d{2}\s*[AP]M.*?Transactions', " ", text, flags=re.S)
    text = re.sub(r'AppFolio\s+Owner\s+Portal\s*\|\s*Transactions', " ", text, flags=re.S)
    text = re.sub(r'https?://\S+(\s+\d+/\d+)?', " ", text)  # URL + trailing "2/3" page marker
    text = re.sub(config.APPFOLIO_BOILERPLATE_RE, " ", text, flags=re.S)
    text = re.sub(r'Date\s+Type\s+Party\s+Property\s+Description\s+Amount\s+Balance', " ", text)
    return text


def _split_records(text):
    """Yield (date, type, body) tuples, one per ledger transaction.

    AppFolio's print-to-PDF wraps every field onto its own line and sometimes drops the
    space between date+type and between amount+balance, so we anchor on 'DATE TYPE' and
    take everything up to the next such anchor as the record body.
    """
    anchor = re.compile(r'(\d{1,2}/\d{1,2}/\d{4})\s*(' + "|".join(TXN_TYPES) + r')')
    hits = list(anchor.finditer(text))
    for i, m in enumerate(hits):
        end = hits[i + 1].start() if i + 1 < len(hits) else len(text)
        body = text[m.end():end]
        yield m.group(1), m.group(2), body


def _classify(txtype, party, desc):
    """-> (category, booking_type, note). booking_type in {Expense, Income, Transfer, Review}."""
    d = (party + " " + desc).lower()
    if txtype == "Contribution":
        return "Transfer (exclude)", "Transfer", "owner contribution — reserve funding, not a P&L item"
    if txtype == "Owner Disbursement":
        return "Transfer (exclude)", "Transfer", "PM surplus disbursed to owner — already-earned income; not re-booked"
    if txtype == "Cash In":
        if "deposit" in d:
            return ("Other", "Review",
                    "security deposit — returnable (mark Skip) OR forfeited=income (set Type=Income). "
                    "You flagged this tenant's as forfeited.")
        return "Rental Income", "Income", ""
    # Cash Out (and any unclassified debit): categorize by description
    for keys, (cat, typ, note) in RULES:
        if any(k in d for k in keys):
            return cat, typ, note
    return "Other", "Review", "uncategorized PM payment — set a category in the Import tab"


def parse_appfolio_pdf(text):
    """PDF text -> dict {sheet,label,rows,summary} or None if not an AppFolio statement."""
    low = text.lower()
    if "appfolio" not in low and "owner portal" not in low:
        return None
    profile = next(((sheet, label) for kw, sheet, label in PROPERTY_MATCH if kw in low), None)
    if not profile:
        return None
    sheet, label = profile
    flat = re.sub(r'[ \t]+', ' ', text)
    text = _strip_boilerplate(text)

    rows = []
    for date_s, txtype, body in _split_records(text):
        try:
            d = datetime.strptime(date_s, "%m/%d/%Y")
        except ValueError:
            continue
        nums = re.findall(r'-?[\d,]+\.\d{2}', body)
        if not nums:
            continue
        amount = _money(nums[-2]) if len(nums) >= 2 else _money(nums[-1])  # [amount, balance]
        # Body text minus the trailing amount/balance, collapsed to one line.
        clean = re.sub(r'\s+', " ", body).strip()
        clean = re.sub(r'(-?[\d,]+\.\d{2}\s*){1,2}$', "", clean).strip()
        # Party = tokens before the property address; description = everything after the
        # first address occurrence (the address can repeat, e.g. "Tenant, <addr>: <desc>").
        parts = re.split(config.APPFOLIO_ADDRESS_RE, clean, flags=re.I)
        party = parts[0].strip(" -:") if parts else ""
        desc = " ".join(p.strip() for p in parts[1:]).strip(" -:") if len(parts) > 1 else clean
        cat, btype, note = _classify(txtype, party, desc)
        signed = -abs(amount) if btype == "Expense" else abs(amount)
        rows.append({
            "date_obj": d, "date_str": d.strftime("%-m/%-d/%Y"),
            "payee": (party or config.APPFOLIO_PM_NAME) + (f" — {desc}" if desc else ""),
            "amount": signed, "type": btype, "category": cat,
            "source": "appfolio",
            "status": "review" if btype == "Review" else "NEW",
            "note": note,
            "_txtype": txtype, "_raw_amount": amount,
        })
    if not rows:
        return None
    return {"sheet": sheet, "label": label, "rows": rows, "summary": _find_summary(flat)}


def entries_from(parsed):
    """Booking rows for import_relay (strip internal keys)."""
    keep = ("date_obj", "date_str", "payee", "amount", "type", "category", "source", "status", "note")
    return [{k: r[k] for k in keep} for r in parsed["rows"]]


def reconcile(parsed):
    """Compare classified totals to the statement's own Cash In/Out checksums.
    -> (ok, message). Cash Out is the reliable check (contributions dominate Cash In)."""
    s = parsed["summary"]
    cash_out = sum(-r["amount"] for r in parsed["rows"]
                   if r["_txtype"] == "Cash Out")  # magnitudes
    msgs = []
    ok = True
    if s.get("cash_out") is not None:
        want = abs(s["cash_out"])
        if abs(cash_out - want) > 0.01:
            ok = False
            msgs.append(f"Cash Out {cash_out:.2f} != statement {want:.2f} (Δ {cash_out - want:+.2f})")
        else:
            msgs.append(f"Cash Out reconciles: {cash_out:.2f}")
    return ok, "; ".join(msgs)


if __name__ == "__main__":
    import sys
    try:
        from pypdf import PdfReader
    except ImportError:
        sys.exit("pip install pypdf")
    if len(sys.argv) < 2:
        sys.exit("usage: python3 appfolio.py <statement.pdf>")
    text = "\n".join(p.extract_text() for p in PdfReader(sys.argv[1]).pages)
    parsed = parse_appfolio_pdf(text)
    if not parsed:
        sys.exit("Not a recognized AppFolio statement.")
    print(f"Sheet: {parsed['sheet']}  ({parsed['label']})")
    ok, msg = reconcile(parsed)
    print(f"Reconcile: {'OK' if ok else 'MISMATCH'} — {msg}\n")
    print(f"{'Date':<11} {'Amount':>10} {'BookType':<9} {'Category':<26} Payee")
    print("-" * 100)
    for r in sorted(parsed["rows"], key=lambda x: x["date_obj"]):
        print(f"{r['date_str']:<11} {r['amount']:>+10.2f} {r['type']:<9} {r['category']:<26} {r['payee']}")
        if r["note"]:
            print(f"{'':<11} {'':>10} {'':<9} {'':<26} [{r['note']}]")
