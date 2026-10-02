#!/usr/bin/env python3
"""
sources.py — which documents count, decided in one place.

Documents arrive three ways: the Drive inbox, Drive's Done/ folder, and attachments on email
replies. Until 2026-10-02 every consumer deduped them BY FILENAME, which is wrong for PDFs:
AppFolio always names its export "AppFolio Owner Portal _ Transactions.pdf", so a new export
was silently shadowed by an old copy with the same name, and because previews were keyed on the
set of filenames, a same-name replacement didn't even trigger a new preview.

Rules:
  - CSVs: one per filename, inbox > Done > email. Two copies of a Relay CSV would stage its rows
    twice, and the filename (account # + month) is the CSV's identity.
  - PDFs: every distinct copy. Mortgage statements dedupe downstream by (property, month);
    AppFolio exports are running year-to-date ledgers, so the consumer picks ONE per property
    (appfolio.pick_best) — never stage two, their rows overlap.
  - Identity is content, not name: a Drive file is its id + modifiedTime (a re-upload or an edit
    is new), an emailed attachment is a hash of its text.

Stdlib only, so it is testable in CI before secrets exist.
"""
import hashlib

ORIGIN_RANK = {"inbox": 0, "done": 1, "email": 2}   # lower wins a CSV filename collision


def fingerprint(origin, item):
    if origin == "email":
        return "email:" + hashlib.sha1((item.get("text") or "").encode()).hexdigest()[:12]
    return f"drive:{item['id']}:{item.get('modifiedTime', '')}"


def merge(inbox, done, emailed):
    """inbox/done: Drive metas {name, kind, id, modifiedTime, ...}; emailed: {name, kind, text}.
    -> [doc] with {name, kind, origin, file_id, text (None for Drive until downloaded),
    movable, fp, meta}. CSVs first, then PDFs, each in inbox > Done > email order."""
    tagged = ([("inbox", d) for d in inbox] + [("done", d) for d in done]
              + [("email", d) for d in emailed])
    csvs, pdfs, seen = {}, [], set()
    for origin, d in tagged:
        doc = {"name": d["name"], "kind": d["kind"], "origin": origin,
               "file_id": d.get("id"), "text": d.get("text") if origin == "email" else None,
               "movable": origin == "inbox", "fp": fingerprint(origin, d), "meta": d}
        if d["kind"] == "csv":
            have = csvs.get(d["name"])
            if have is None or ORIGIN_RANK[origin] < ORIGIN_RANK[have["origin"]]:
                csvs[d["name"]] = doc
        elif d["kind"] == "pdf" and doc["fp"] not in seen:
            seen.add(doc["fp"])
            pdfs.append(doc)
    return list(csvs.values()) + pdfs


def fileset(docs):
    """-> sorted identities of what has ARRIVED (inbox + email; Done/ is already-processed),
    for the preview's "is this a new set?" hash. Content-based, so replacing a file with a
    same-name newer copy counts as new."""
    return sorted(f"{d['name']}|{d['fp']}" for d in docs if d["origin"] != "done")
