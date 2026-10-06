#!/usr/bin/env python3
"""
still_needed.py — which documents for CLOSED months haven't arrived yet.

One definition of "still needed", used by four emails so they can never disagree:
the preview, the booking receipt, the 1st-of-month reminder (`--json`), and the mid-month
nudge (`--nudge`).

Every month from TRACK_FROM through last month, every property needs:
  - its Relay CSV for the month (keyed by the YYYY-MM in the filename — keep original names),
  - its mortgage statement for the month (keyed by the statement's due date),
  - for the AppFolio property (Indy), an AppFolio export that runs PAST the month. The export
    prints no date range, so the only proof a month is complete is a transaction dated after it.
What has "arrived" is everything the pipeline can read: Drive inbox + Done/ + emailed files.

Why this replaced the preview's old "Still needed": it only looked one way (a statement
waiting on its CSV) and treated "any AppFolio file present" as "AppFolio done". So Hideaway
Hollow's missing August statement (a draft waiting on a statement) and Indy's AppFolio export
stopping at Aug 7 were both invisible, and no email would ever have mentioned them.

The nudge: once the 15th of the following month has passed, if anything through that month is
still missing, send one email "Still needed for <Month YYYY>". At most one per month
(mailbox-as-state on that subject); silent when nothing is missing. With the reminder on the
1st, that is the whole cadence.

    python still_needed.py             # print what's missing (read-only)
    python still_needed.py --json      # same, as JSON (the reminder workflow reads this)
    python still_needed.py --nudge     # send the mid-month nudge if one is due
    python still_needed.py --nudge --dry-run

Pure logic is stdlib-only and importable without config (test_still_needed.py runs in CI before
secrets exist); anything touching Drive, Sheets or Gmail imports inside the function.
"""
import json
import os
import sys
from datetime import date, datetime

TRACK_FROM = (2026, 7)      # first month the pipeline closed; earlier months were entered by hand
NUDGE_DAY = 15
NUDGE_SUBJECT = "Still needed for"   # + " August 2026". No "booked"/"bookkeeping" — see subject()
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December"]


def _prev(ym):
    y, m = ym
    return (y - 1, 12) if m == 1 else (y, m - 1)


def closed_months(today, track_from=TRACK_FROM, through=None):
    """-> [(year, month)] from track_from through last month (or `through`, if earlier)."""
    last = _prev((today.year, today.month))
    if through and through < last:
        last = through
    out, ym = [], track_from
    while ym <= last:
        out.append(ym)
        ym = (ym[0] + 1, 1) if ym[1] == 12 else (ym[0], ym[1] + 1)
    return out


def nudge_through(today):
    """-> the latest month whose nudge date (the 15th of the month after) has passed."""
    last = _prev((today.year, today.month))
    return last if today.day >= NUDGE_DAY else _prev(last)


def month_name(ym, today=None):
    """'August', or 'August 2025' when the year isn't the current one."""
    name = MONTHS[ym[1] - 1]
    return name if today is None or ym[0] == today.year else f"{name} {ym[0]}"


def subject(ym):
    """The nudge subject. IMAP SUBJECT search is a case-insensitive substring match and other
    lookups key off subjects, so: no "booked" (receipt search marks batches handled), no
    "bookkeeping" (reminder dedupe), no "[hash]", no "Didn't book". ASCII only."""
    return f"{NUDGE_SUBJECT} {MONTHS[ym[1] - 1]} {ym[0]}"


def missing(by_prop, months, *, labels, appfolio_sheet, today):
    """-> [(property label, [item text])] for documents not yet arrived. Pure."""
    out = []
    names = lambda yms: ", ".join(month_name(ym, today) for ym in yms)  # noqa: E731
    for sheet, label in labels.items():
        b = by_prop.get(sheet) or {}
        items = []
        csv = [ym for ym in months if ym not in b.get("csv_ym", set())]
        if csv:
            items.append(f"Relay CSV for {names(csv)}")
        stmt = [ym for ym in months if ym not in b.get("stmt_ym", set())]
        if stmt:
            plural = "" if len(stmt) == 1 else "s"
            items.append(f"Mortgage statement{plural} for {names(stmt)}")
        if sheet == appfolio_sheet:
            through = b.get("appfolio_through")
            af = [ym for ym in months if not (through and (through.year, through.month) > ym)]
            if af:
                bad = b.get("appfolio_unreconciled_through")
                if bad:   # a newer export exists but doesn't count — say why, or it reads as ignored
                    tail = (f" (the newest one, through {bad:%b} {bad.day}, doesn't add up to its "
                            f"own totals, usually a cut-off print; re-export with every row shown)")
                elif through:
                    tail = f" (the latest one ends {through:%b} {through.day})"
                else:
                    tail = ""
                items.append(f"AppFolio export covering all of {names(af)}{tail}")
        if items:
            out.append((label, items))
    return out


def collect(svc=None, cfg=None, today=None, through=None):
    """-> (needs, months checked). Reads Drive inbox + Done/ + emailed files."""
    import config
    import inbox_status
    today = today or local_today()
    months = closed_months(today, through=through)
    scan = inbox_status.scan(svc, cfg, include_done=True)
    return missing(scan["by_prop"], months, labels=config.INBOX_PROPS,
                   appfolio_sheet=config.INBOX_APPFOLIO_SHEET, today=today), months


def local_today():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York")).date()
    except Exception:
        return date.today()


# --- the mid-month nudge ---------------------------------------------------------------------

def already_nudged(subj):
    import imaplib
    import mailbox_state
    m = imaplib.IMAP4_SSL("imap.gmail.com")
    try:
        m.login(os.environ["GMAIL_USER"], os.environ["GMAIL_APP_PASSWORD"])
        return mailbox_state.exists(m, "SUBJECT", mailbox_state.q(subj))
    finally:
        try:
            m.logout()
        except Exception:
            pass


def render_nudge(needs, ym, url, today):
    from preview_email import FONT, INK, MUTED, FAINT, esc, needs_block
    through = month_name(ym, today)
    lead = (f"These documents for months through {through} haven't arrived, so those months "
            f"aren't fully booked.")
    act = ("Drop them in the Relay Imports inbox or reply to this email with them attached. "
           "You'll get a preview once they're in.")
    lines = [f"Still needed — through {through}", "", lead, ""]
    for label, items in needs:
        lines.append(label)
        lines += [f"  - {t}" for t in items]
        lines.append("")
    lines += [act] + ([f"Inbox: {url}"] if url else [])
    html = (
        f'<div style="font-family:{FONT};color:{INK};background:#ffffff;font-size:15px;'
        f'line-height:1.5;max-width:680px;margin:0 auto;padding:16px 18px 22px">'
        f'<h2 style="font-size:19px;font-weight:600;margin:0 0 6px">Still needed</h2>'
        f'<p style="margin:0;color:{MUTED};font-size:14px">{esc(lead)}</p>'
        f'{needs_block(needs, url)}'
        f'<p style="margin:16px 0 0;font-size:14px">{esc(act)}</p>'
        f'<p style="margin:18px 0 0;font-size:11.5px;color:{FAINT}">'
        f'Sent once a month, on or after the {NUDGE_DAY}th, only when something is missing.</p>'
        f'</div>')
    return "\n".join(lines) + "\n", html


def nudge(dry_run=False, today=None):
    today = today or local_today()
    ym = nudge_through(today)
    if ym < TRACK_FROM:
        print("No closed month is past its nudge date yet.")
        return
    subj = subject(ym)
    if not dry_run and already_nudged(subj):
        print(f"'{subj}' already in mailbox — skipping.")
        return
    from import_relay import drive_service, load_drive_config
    from preview_email import inbox_url, send
    cfg, svc = load_drive_config(), drive_service()
    needs, _months = collect(svc, cfg, today=today, through=ym)
    if not needs:
        print(f"Nothing missing through {month_name(ym)} — no nudge.")
        return
    n = sum(len(items) for _l, items in needs)
    if dry_run:
        print(f"Would send '{subj}' — {n} item(s):")
        print_needs(needs)
        return
    text, html = render_nudge(needs, ym, inbox_url(cfg), today)
    send(subj, text, html)
    print(f"Nudge sent: '{subj}' — {n} item(s).")


def waiting_previews():
    """-> previews still waiting on a confirm, for the reminder; None if the mailbox check fails
    (the reminder then just omits the line)."""
    try:
        from datetime import timezone
        import confirm_and_book as cb
        m = cb.imap_connect()
        try:
            s = cb.scan(m)
        finally:
            cb.logout(m)
        return cb.waiting_previews(s, datetime.now(timezone.utc))
    except Exception as e:
        print(f"  ! Couldn't check for previews waiting on a confirm ({str(e)[:100]}).")
        return None


def print_needs(needs):
    if not needs:
        print("  Nothing missing.")
    for label, items in needs:
        print(f"  {label}")
        for t in items:
            print(f"    - {t}")


def main(argv):
    unknown = set(argv) - {"--json", "--nudge", "--dry-run"}
    if unknown:
        sys.exit(f"Unknown argument(s): {' '.join(sorted(unknown))}")
    if "--nudge" in argv:
        nudge(dry_run="--dry-run" in argv)
        return
    today = local_today()
    if "--json" in argv:
        # Other modules log to stdout (e.g. "12 document(s) picked up from email"); keep the JSON
        # the reminder parses the only thing on it.
        import contextlib
        with contextlib.redirect_stdout(sys.stderr):
            needs, months = collect(today=today)
            waiting = waiting_previews()
        print(json.dumps({"through": month_name(months[-1], today) if months else None,
                          "needs": needs, "waiting": waiting}))
        return
    needs, months = collect(today=today)
    through = month_name(months[-1], today) if months else None
    print(f"Still needed through {through}:" if through else "No closed months tracked yet.")
    print_needs(needs)


if __name__ == "__main__":
    main(sys.argv[1:])
