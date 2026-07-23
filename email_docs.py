#!/usr/bin/env python3
"""
email_docs.py — documents attached to an email reply, as a pipeline input source.

A second way in, alongside the Drive inbox: reply to the monthly reminder (or to a preview)
with the month's Relay CSVs / mortgage statements / AppFolio export attached, and they feed the
same staging -> preview -> confirm flow. The Drive drop stays the primary path; this is additive.

Why the attachments are re-read from Gmail on every run instead of being copied into Drive:
the pipeline's service account has NO Drive storage quota (verified — "Service Accounts do not
have storage quota. Leverage shared drives, or use OAuth delegation instead"), so it physically
cannot create a file in the inbox folder, and Shared Drives need Google Workspace. Gmail already
stores the attachments durably, so the mailbox itself is the store.

Consequence, by design: an emailed file stays in the dataset permanently — there is no "move to
Done" for it. Harmless, because sources are deduped by filename and promote is idempotent
(preserved promoted stamps + amount reconciliation), exactly like held statements that sit in the
Drive inbox waiting for their CSV.

Only attachments on a message FROM the owner carrying one of our own subjects are read, so a
random email with a spreadsheet attached can never inject rows.

Auth: Gmail app password in GMAIL_USER / GMAIL_APP_PASSWORD. If those aren't set (a local run),
this yields nothing and the Drive path works exactly as before.
"""
import email as emaillib
import imaplib
import os
import warnings

warnings.filterwarnings("ignore")

import fitz  # pymupdf, to read attached statement PDFs

USER = os.environ.get("GMAIL_USER", "")
PW = os.environ.get("GMAIL_APP_PASSWORD", "")

# Only messages carrying one of our own subjects are considered.
SUBJECT_MARKERS = ("Bookkeeping preview", "Rental bookkeeping")

_CACHE = None  # per-process memo; a single poll asks for these more than once


def available():
    return bool(USER and PW)


def _q(s):
    """Quote an IMAP search term — unquoted multi-word terms are rejected as BAD."""
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _attachments(msg):
    """-> (filename, kind, text) for each CSV/PDF attachment; other parts are ignored."""
    for part in msg.walk():
        name = part.get_filename()
        if not name:
            continue
        low = name.lower()
        if low.endswith(".csv"):
            kind = "csv"
        elif low.endswith(".pdf"):
            kind = "pdf"
        else:
            continue  # signatures, inline images, etc.
        try:
            data = part.get_payload(decode=True)
        except Exception:
            continue
        if not data:
            continue
        if kind == "pdf":
            try:
                doc = fitz.open(stream=data, filetype="pdf")
                text = "\n".join(p.get_text() for p in doc)
            except Exception:
                continue
        else:
            text = data.decode("utf-8-sig", "replace")
        yield name, kind, text


def fetch():
    """-> list of pipeline sources {name, kind, text, file_id, movable}, deduped by filename.

    Never raises: if Gmail is unreachable or the credentials are wrong, this warns and returns
    nothing so the Drive path keeps working rather than taking the whole run down.
    """
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    if not available():
        _CACHE = []
        return _CACHE

    found, m = {}, None
    try:
        m = imaplib.IMAP4_SSL("imap.gmail.com")
        m.login(USER, PW)
        m.select("INBOX")
        seen = set()
        for marker in SUBJECT_MARKERS:
            typ, data = m.search(None, "FROM", _q(USER), "SUBJECT", _q(marker))
            if typ != "OK" or not data or not data[0]:
                continue
            for num in data[0].split():
                if num in seen:
                    continue
                seen.add(num)
                typ, raw = m.fetch(num, "(RFC822)")
                if typ != "OK" or not raw or not raw[0]:
                    continue
                msg = emaillib.message_from_bytes(raw[0][1])
                if USER.lower() not in str(msg.get("From", "")).lower():
                    continue  # only the owner's own mail
                for name, kind, text in _attachments(msg):
                    found.setdefault(name, {"name": name, "kind": kind, "text": text,
                                            "file_id": None, "movable": False})
    except Exception as e:
        print(f"  ! Couldn't read emailed attachments ({str(e)[:120]}) — "
              f"continuing with the Drive inbox only.")
        found = {}
    finally:
        if m is not None:
            try:
                m.logout()
            except Exception:
                pass

    _CACHE = list(found.values())
    if _CACHE:
        print(f"  · {len(_CACHE)} document(s) picked up from email: "
              f"{', '.join(sorted(s['name'] for s in _CACHE))}")
    return _CACHE


def names():
    """Just the filenames — used where only 'what has arrived' matters."""
    return sorted(s["name"] for s in fetch())
