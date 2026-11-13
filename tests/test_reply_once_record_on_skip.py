#!/usr/bin/env python3
"""Tests for reply_once.py record-on-skip (record-reply path).

Adopted 2026-11-12 from Yama's self-review proposal in #slack-へようこそ
(their record-reply subcommand): when a pre-send check finds the reply
already in the thread but the ledger has no entry for it, the reply is
recorded (with its reply ts) instead of only skipping the send, so the
ledger stays the single source of truth for dedupe.

Plain stdlib script, no pytest (same style as test_reply_once_recovery.py).
Run: python3 tests/test_reply_once_record_on_skip.py
Exit 0 when every check passes; each check prints PASS/FAIL.

The module's thread-read functions are stubbed and the record files are
redirected to a temp dir, so no real Slack call is made and the real
events/ directory is never touched.
"""
import importlib.util
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SPEC = importlib.util.spec_from_file_location(
    "reply_once", os.path.join(ROOT, "reply_once.py"))
ro = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ro)

CHECKS = [0, 0]  # [passed, failed]


def check(name, cond, detail=""):
    CHECKS[0 if cond else 1] += 1
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not cond else ""))
    return cond


MSG = "test reply text"
ARGS = ["--channel", "C0TEST", "--thread-ts", "111.1",
        "--target-ts", "222.2", "--message", MSG]


def setup(tmp, name):
    sub = os.path.join(tmp, name)
    os.makedirs(sub, exist_ok=True)
    ro.REPLIED = os.path.join(sub, "replied.jsonl")
    ro.AMBIGUOUS = os.path.join(sub, "ambiguous_posts.jsonl")
    ro.EVENTS = sub


def read_jsonl(path):
    try:
        with open(path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        return []


def test_skip_records_thread_found_reply(tmp):
    """already-replied via thread, no ledger entry -> record written."""
    setup(tmp, "skip_records")
    ro.already_replied = lambda *a, **k: "thread"
    ro.thread_yanayana_replies = lambda *a, **k: [
        {"ts": "333.3", "text": MSG},
        {"ts": "334.4", "text": "other reply"},
    ]
    rc = ro.main(list(ARGS))
    recs = read_jsonl(ro.REPLIED)
    check("skip + thread-found -> exit 0", rc == 0, f"rc={rc}")
    check("skip + thread-found -> one record written",
          len(recs) == 1, f"recs={recs}")
    check("skip + thread-found -> reply_ts recorded",
          len(recs) == 1 and recs[0].get("reply_ts") == "333.3", f"recs={recs}")
    check("skip + thread-found -> target_ts recorded",
          len(recs) == 1 and recs[0].get("target_ts") == "222.2", f"recs={recs}")


def test_skip_no_duplicate_record(tmp):
    """already-replied via ledger -> no second record written."""
    setup(tmp, "skip_record_exists")
    ro.already_replied = lambda *a, **k: "record"
    ro.thread_yanayana_replies = lambda *a, **k: [
        {"ts": "333.3", "text": MSG},
    ]
    # Pre-seed the ledger entry the "record" path found.
    ro.record_reply("C0TEST", "111.1", "222.2", "333.3")
    rc = ro.main(list(ARGS))
    recs = read_jsonl(ro.REPLIED)
    check("skip + record exists -> exit 0", rc == 0, f"rc={rc}")
    check("skip + record exists -> still exactly one record",
          len(recs) == 1, f"recs={recs}")


def test_skip_thread_read_failure_still_skips(tmp):
    """Thread re-read for the ts fails -> still skips send, exit 0."""
    setup(tmp, "skip_read_fail")
    ro.already_replied = lambda *a, **k: "thread"
    ro.thread_yanayana_replies = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("read failed"))
    rc = ro.main(list(ARGS))
    check("skip + re-read fails -> exit 0 (no blind send)",
          rc == 0, f"rc={rc}")
    check("skip + re-read fails -> no record written (nothing proven)",
          read_jsonl(ro.REPLIED) == [])


def main():
    with tempfile.TemporaryDirectory() as tmp:
        test_skip_records_thread_found_reply(tmp)
        test_skip_no_duplicate_record(tmp)
        test_skip_thread_read_failure_still_skips(tmp)
    print(f"\n{CHECKS[0]} passed, {CHECKS[1]} failed")
    return 1 if CHECKS[1] else 0


if __name__ == "__main__":
    sys.exit(main())
