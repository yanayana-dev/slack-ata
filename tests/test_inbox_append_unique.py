#!/usr/bin/env python3
"""Tests for inbox_state.append_record_unique (burst fix, 2026-11-21).

The socket listener and backfill.py both admit channel messages to
events/inbox.jsonl. Their seen-set check-then-append raced whenever
both ran over the same burst (VM-replacement recovery makes them
overlap), landing the same (channel, ts) in the inbox twice under
different event_ids. append_record_unique makes the presence check
and the append atomic under one flock, shared by both writers.

Plain stdlib script, no pytest (house style). Run:
  python3 tests/test_inbox_append_unique.py
Exit 0 when every check passes; each check prints PASS/FAIL.
"""
import json
import multiprocessing as mp
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import backfill  # noqa: E402
import inbox_state  # noqa: E402
import listener  # noqa: E402

CHECKS = [0, 0]  # [passed, failed]


def check(name, cond, detail=""):
    CHECKS[0 if cond else 1] += 1
    print(f"{'PASS' if cond else 'FAIL'}  {name}"
          + (f"  ({detail})" if detail and not cond else ""))
    return cond


def lines(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def rec(ts, event_id="EvSock1", source=None):
    r = {"event_id": event_id, "received_at": "2026-11-21T12:53:48+00:00",
         "channel": "C0EXAMPLE001", "channel_type": "channel",
         "user": "U0EXAMPLEUSER", "bot_id": None, "app_id": None,
         "text": "burst post", "ts": ts, "thread_ts": None,
         "subtype": None}
    if source:
        r["source"] = source
    return r


def _race_worker(path, record, n, queue):
    appended = 0
    for _ in range(n):
        if inbox_state.append_record_unique(path, record):
            appended += 1
    queue.put(appended)


def main():
    tmp = tempfile.mkdtemp(prefix="inbox-unique-test-")
    path = os.path.join(tmp, "inbox.jsonl")

    # 1. First append lands; the same (channel, ts) under a different
    # delivery (backfill copy, event_id None) does not.
    check("first append returns True",
          inbox_state.append_record_unique(path, rec("1000.000001")) is True)
    dup = rec("1000.000001", event_id=None, source="backfill")
    check("same channel:ts from the other writer returns False",
          inbox_state.append_record_unique(path, dup) is False)
    check("inbox still holds exactly one line", len(lines(path)) == 1)

    # 2. A different ts is a different message.
    check("different ts appends",
          inbox_state.append_record_unique(path, rec("1000.000002")) is True)
    check("inbox holds two lines", len(lines(path)) == 2)

    # 3. Records without a ts cannot be keyed: always appended
    # (append-only fallback, never silently dropped).
    no_ts = rec("1000.000003")
    del no_ts["ts"]
    check("ts-less record appends",
          inbox_state.append_record_unique(path, dict(no_ts)) is True)
    check("second ts-less record also appends",
          inbox_state.append_record_unique(path, dict(no_ts)) is True)
    check("inbox holds four lines", len(lines(path)) == 4)

    # 4. Process race: 6 processes x 5 attempts at the same record
    # must produce exactly one append and one line. This is the
    # listener-vs-backfill overlap from 2026-11-21, mechanized.
    race_path = os.path.join(tmp, "race.jsonl")
    queue = mp.Queue()
    procs = [mp.Process(target=_race_worker,
                        args=(race_path, rec("2000.000001"), 5, queue))
             for _ in range(6)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(30)
    total_appended = sum(queue.get() for _ in procs)
    check("concurrent writers append exactly once", total_appended == 1,
          f"got {total_appended}")
    check("raced inbox holds exactly one line", len(lines(race_path)) == 1)

    # 5. Both real writers share the discipline on the same file:
    # whichever of listener.append_inbox / backfill.append_inbox runs
    # second is a no-op for an already-present (channel, ts).
    shared = os.path.join(tmp, "shared.jsonl")
    old_inbox_path, old_backfill_inbox = listener.INBOX_PATH, backfill.INBOX
    listener.INBOX_PATH = shared
    backfill.INBOX = shared
    try:
        check("listener append lands",
              listener.append_inbox(rec("3000.000001")) is True)
        check("backfill append of the same message is a no-op",
              backfill.append_inbox(
                  rec("3000.000001", event_id=None, source="backfill")) is False)
        check("shared inbox holds one line", len(lines(shared)) == 1)
        check("backfill append lands for a new message",
              backfill.append_inbox(
                  rec("3000.000002", event_id=None, source="backfill")) is True)
        check("listener append of that message is a no-op",
              listener.append_inbox(rec("3000.000002")) is False)
        check("shared inbox holds two lines", len(lines(shared)) == 2)
    finally:
        listener.INBOX_PATH = old_inbox_path
        backfill.INBOX = old_backfill_inbox

    print(f"{CHECKS[0]} passed, {CHECKS[1]} failed")
    return 0 if CHECKS[1] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
