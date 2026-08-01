#!/usr/bin/env python3
"""
test_sheets_retry.py — regression guard on the Sheets 429 retry.

`capture._install_sheets_retry()` wraps gspread's single HTTP funnel so a rate-limited call
waits and retries instead of killing the run. It exists because the 2026-08-01 monthly close
(ops run 30707153877) staged all three Import tabs successfully and then died on a 429 in
preview_email — correct books, no email, and a failure alert that dedups to one a day.

Two properties are load-bearing and neither is obvious from reading the wrapper:

  1. **Only 429 retries.** Every other APIError must still raise on the first call. A retry
     that swallowed a 403 would turn "the service account lost access to a sheet" into three
     silent minutes followed by the same crash — or worse, mask a genuine write failure.
  2. **The waits are tens of seconds.** The Sheets quota bucket refills per MINUTE, so a
     millisecond backoff would burn all three retries inside the same exhausted window and
     fail anyway. If someone "optimizes" these to (1, 2, 4) the guard is gone and nothing
     else would notice.

Needs no secrets — config is stubbed below — so this runs in CI alongside the other guards,
before the real config.py is materialized.

    python3 test_sheets_retry.py
"""
import sys
import types

# Stub config before importing capture: capture reads a handful of names at module level, and
# the real config.py is gitignored (and absent in CI at this point in the workflow). Nothing
# here is exercised by the retry path — open_sheet is never called.
_stub = types.ModuleType("config")
_stub.CREDS_FILE = "unused-by-this-test.json"
_stub.PROPERTY_SHEETS = {}
_stub.NOT_YET = {}
_stub.UTILITY_VENDOR_LINES = ()
_stub.PROPERTY_HELP = ""
sys.modules.setdefault("config", _stub)

import gspread.exceptions as gs_exc
from gspread.http_client import HTTPClient

import capture

OK = "OK-RESPONSE"


class FakeResponse:
    """The shape APIError parses: a .json() error envelope plus .status_code."""

    text = ""

    def __init__(self, code, message):
        self.status_code = code
        self._code = code
        self._message = message

    def json(self):
        return {"error": {"code": self._code, "message": self._message, "status": "ERROR"}}


def api_error(code, message):
    return gs_exc.APIError(FakeResponse(code, message))


RATE_LIMIT = api_error(429, "Quota exceeded for quota metric 'Read requests' and limit "
                            "'Read requests per minute per user'")
PERMISSION = api_error(403, "The caller does not have permission")
NOT_FOUND = api_error(404, "Requested entity was not found")


def drive(script):
    """Install the wrapper over a scripted fake transport.

    -> (result, error, n_calls, waits). `script` is consumed one entry per underlying call;
    an exception is raised, anything else is returned. Entries past the end return OK.
    """
    calls = {"n": 0}

    def fake_request(self, *args, **kwargs):
        i = calls["n"]
        calls["n"] += 1
        step = script[i] if i < len(script) else OK
        if isinstance(step, BaseException):
            raise step
        return step

    original, real_sleep = HTTPClient.request, capture.time.sleep
    waits = []
    capture.time.sleep = waits.append          # instant, and records what it WOULD have waited
    HTTPClient.request = fake_request          # unmarked, so the installer re-wraps it
    try:
        capture._install_sheets_retry()
        try:
            return HTTPClient.request(None), None, calls["n"], waits
        except Exception as e:
            return None, e, calls["n"], waits
    finally:
        HTTPClient.request = original
        capture.time.sleep = real_sleep


def main():
    n_retries = len(capture.RATE_LIMIT_WAITS)
    expected_waits = list(capture.RATE_LIMIT_WAITS)
    fails = 0

    # (label, script, expect_result, expect_error_code, expect_calls, expect_waits)
    cases = [
        ("clean call, no retry",
         [OK], OK, None, 1, []),
        ("429 once, then succeeds",
         [RATE_LIMIT, OK], OK, None, 2, expected_waits[:1]),
        ("429 twice, then succeeds",
         [RATE_LIMIT] * 2 + [OK], OK, None, 3, expected_waits[:2]),
        ("429 on every retry, last try wins",
         [RATE_LIMIT] * n_retries + [OK], OK, None, n_retries + 1, expected_waits),
        ("429 forever, surfaces the 429",
         [RATE_LIMIT] * (n_retries + 1), None, 429, n_retries + 1, expected_waits),
        ("403 raises immediately, no retry",
         [PERMISSION], None, 403, 1, []),
        ("404 raises immediately, no retry",
         [NOT_FOUND], None, 404, 1, []),
    ]

    for label, script, want_result, want_code, want_calls, want_waits in cases:
        result, error, n_calls, waits = drive(script)
        got_code = getattr(error, "code", None)
        ok = (result == want_result and got_code == want_code
              and n_calls == want_calls and waits == want_waits)
        fails += not ok
        detail = f"{n_calls} call(s), waits {waits}"
        if error is not None:
            detail += f", raised {type(error).__name__}[{got_code}]"
        print(f"{'PASS' if ok else 'FAIL'}  {label:<38} -> {detail}")

    # The waits must stay minute-scale or the retry is theatre: Sheets refills its read bucket
    # per minute, so three fast retries all land inside the same exhausted window.
    slow_enough = sum(capture.RATE_LIMIT_WAITS) >= 60 and max(capture.RATE_LIMIT_WAITS) >= 45
    fails += not slow_enough
    print(f"{'PASS' if slow_enough else 'FAIL'}  {'waits outlast a 60s quota window':<38} "
          f"-> {capture.RATE_LIMIT_WAITS} (total {sum(capture.RATE_LIMIT_WAITS)}s)")

    # Re-installing on import from several modules must not stack wrappers (each layer would
    # multiply the retries, and import_relay, promote, preview_email and confirm_and_book all
    # import capture).
    before = HTTPClient.request
    capture._install_sheets_retry()
    idempotent = HTTPClient.request is before
    fails += not idempotent
    print(f"{'PASS' if idempotent else 'FAIL'}  {'re-install is a no-op':<38} "
          f"-> wrapper {'unchanged' if idempotent else 'STACKED'}")

    print()
    if fails:
        print(f"{fails} FAILURE(S) — the Sheets 429 retry is not behaving.")
        sys.exit(1)
    print(f"All {len(cases) + 2} cases passed — rate-limited calls retry, real errors don't.")


if __name__ == "__main__":
    main()
