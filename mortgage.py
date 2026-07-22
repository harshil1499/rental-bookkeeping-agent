#!/usr/bin/env python3
"""
mortgage.py — parse a monthly mortgage statement PDF into a booking record.

Each statement's "Explanation of Amount Due" gives the split for the payment due that
month: Principal + Interest + Escrow = the total draft that also appears in Relay. Only
**interest** and **escrow** are booked (interest -> Mortgage Interest; escrow -> the
combined Escrow line). Principal is debt paydown, not an expense — never booked.

Property is detected by servicer name (fallback: the total payment amount). Booking month
is the statement's Payment Due Date month, which is verified against each statement's own
payment history to equal the month the draft posts.
"""
import re
from datetime import datetime

import config

# (servicer keywords, approx total payment, sheet, label, display name) — from config.py.
# NOTE: when one servicer holds loans on multiple properties, the servicer keyword identifies
# the *lender* and the payment *amount* disambiguates the *property* (see parse_mortgage_pdf).
# Amount alone is not enough — escrow adjustments drift the total month to month.
PROFILES = config.MORTGAGE_PROFILES


def _money(s):
    return float(s.replace(",", "").replace("$", ""))


def _grab(flat, label):
    m = re.search(label + r'\s*\$?([\d,]+\.\d{2})', flat, re.I)
    return _money(m.group(1)) if m else None


def parse_mortgage_pdf(text):
    """PDF text -> dict, or None if it isn't a recognized mortgage statement."""
    flat = re.sub(r'[ \t]+', ' ', text)
    amount = _grab(flat, r'Amount Due')
    interest = _grab(flat, r'\bInterest\b')          # skips 'Interest Rate 6.375%' (no cents)
    escrow = (_grab(flat, r'Escrow \(for Taxes and Insurance\)')
              or _grab(flat, r'Escrow \(Taxes ?& ?Insurance\)')
              or _grab(flat, r'Escrow \(Taxes and Insurance\)'))
    principal = _grab(flat, r'\bPrincipal\b')         # skips 'Principal Balance ...'
    m = re.search(r'Payment Due Date\s*:?\s*(\d{1,2})[/-](\d{1,2})[/-](\d{4})', flat)
    if not m or interest is None:
        return None
    due = datetime(int(m.group(3)), int(m.group(1)), int(m.group(2)))

    low = text.lower()
    # Candidates: profiles whose servicer keyword appears, or whose amount is close. When one
    # servicer serves multiple properties, pick the nearest amount so a smaller loan's statement
    # can't route to the larger loan's sheet just because both share a servicer keyword.
    cands = [(kws, amt, sheet, label, disp) for kws, amt, sheet, label, disp in PROFILES
             if any(k in low for k in kws) or (amount and abs(amount - amt) < 75)]
    if not cands:
        return None
    if amount is not None:
        _, _, sheet, label, disp = min(cands, key=lambda p: abs(amount - p[1]))
    else:
        _, _, sheet, label, disp = cands[0]
    return {
        "sheet": sheet, "label": label, "servicer": disp, "due": due,
        "month": due.strftime("%B"), "amount": amount, "interest": interest,
        "escrow": escrow, "principal": principal,
    }


def entries_from(parsed):
    """-> list of pre-categorized booking rows (interest + escrow). Principal excluded."""
    date_str = parsed["due"].strftime("%-m/%-d/%Y")
    rows = [{
        "date_obj": parsed["due"], "date_str": date_str,
        "payee": f"{parsed['servicer']} — Mortgage Interest",
        "amount": -parsed["interest"], "type": "Expense",
        "category": "Mortgage Interest", "source": "statement",
        "note": "from statement; principal excluded (not an expense)",
    }]
    if parsed["escrow"]:
        rows.append({
            "date_obj": parsed["due"], "date_str": date_str,
            "payee": f"{parsed['servicer']} — Escrow",
            "amount": -parsed["escrow"], "type": "Expense",
            "category": "Escrow (Taxes + Insurance)", "source": "statement",
            "note": "escrow deposit — split taxes vs insurance at year-end",
        })
    return rows
