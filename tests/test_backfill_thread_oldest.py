#!/usr/bin/env python3
"""Tests for backfill.py thread re-reads passing --oldest <floor>.

Neiro's 2026-11-18 share (neiro-slack-system 6980acd): reading a
thread without --oldest returns one page (up to 100 messages), so a
long thread can drop replies newer than the page. Passing the floor
as --oldest returns only post-floor replies. The floor is exclusive,
matching admit()'s ts <= floor skip.

Plain stdlib script, no pytest (same style as the other suites).
Run:
  python3 tests/test_backfill_thread_oldest.py
Exit 0 when every check passes; each check prints PASS/FAIL.
"""
import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import backfill

CHECKS = [0, 0]  # [passed, failed]


def check(name, cond):
    if cond:
        CHECKS[0] += 1
        print(f"PASS {name}")
    else:
        CHECKS[1] += 1
        print(f"FAIL {name}")


FLOOR = 1794963000.0
PARENT_TS = "1794963100.000000"
REPLY_TS = "1794963100.500000"


def run_main_with_stub(argv, floor_time=None):
    """Run backfill.main() with stubbed slackcli in a temp dir.

    Returns (calls, inbox_records, summary). calls is the list of
    slackcli arg lists; the channel read returns one parent with
    reply_count, the thread read returns the parent plus one reply.
    """
    tmp = tempfile.mkdtemp()
    events = os.path.join(tmp, "events")
    os.makedirs(events)
    backfill.EVENTS = events
    backfill.INBOX = os.path.join(events, "inbox.jsonl")
    backfill.SEEN_PATH = os.path.join(events, "seen_event_ids.json")
    with open(backfill.SEEN_PATH, "w", encoding="utf-8") as f:
        json.dump([], f)

    calls = []

    def fake_run_slackcli(args, timeout=60):
        calls.append(list(args))
        if "--thread-ts" in args:
            return {"messages": [
                {"ts": PARENT_TS, "text": "parent",
                 "user": "U0TEST", "thread_ts": PARENT_TS},
                {"ts": REPLY_TS, "text": "new reply",
                 "user": "U0TEST", "thread_ts": PARENT_TS},
            ]}
        return {"messages": [
            {"ts": PARENT_TS, "text": "parent",
             "user": "U0TEST", "reply_count": 2},
        ]}

    orig_run = backfill.run_slackcli
    orig_argv = sys.argv
    orig_time = backfill.time.time
    backfill.run_slackcli = fake_run_slackcli
    sys.argv = argv
    if floor_time is not None:
        backfill.time.time = lambda: floor_time
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = backfill.main()
        summary = json.loads(buf.getvalue())
    finally:
        backfill.run_slackcli = orig_run
        sys.argv = orig_argv
        backfill.time.time = orig_time
    records = []
    if os.path.exists(backfill.INBOX):
        with open(backfill.INBOX, encoding="utf-8") as f:
            records = [json.loads(line) for line in f if line.strip()]
    return calls, records, summary, rc


# Explicit --floor-epoch: the thread read must pass it as --oldest.
calls, records, summary, rc = run_main_with_stub(
    ["backfill.py", "--channel", "C0EXAMPLE001",
     "--floor-epoch", str(FLOOR)])
thread_calls = [c for c in calls if "--thread-ts" in c]
check("thread read is called once", len(thread_calls) == 1)
check("thread read passes --oldest with the floor",
      len(thread_calls) == 1
      and "--oldest" in thread_calls[0]
      and thread_calls[0][thread_calls[0].index("--oldest") + 1]
      == str(FLOOR))
check("main exits 0", rc == 0)
check("parent and reply are both admitted",
      summary.get("recovered") == 2)
check("inbox holds the reply ts",
      any(r.get("ts") == REPLY_TS for r in records))

# --window-minutes: the thread read must pass now-minus-window.
NOW = 1794964000.0
calls, records, summary, rc = run_main_with_stub(
    ["backfill.py", "--channel", "C0EXAMPLE001",
     "--window-minutes", "15"], floor_time=NOW)
thread_calls = [c for c in calls if "--thread-ts" in c]
check("window floor is passed as --oldest",
      len(thread_calls) == 1
      and "--oldest" in thread_calls[0]
      and thread_calls[0][thread_calls[0].index("--oldest") + 1]
      == str(NOW - 900))

print(f"\n{CHECKS[0]} passed, {CHECKS[1]} failed")
sys.exit(1 if CHECKS[1] else 0)
