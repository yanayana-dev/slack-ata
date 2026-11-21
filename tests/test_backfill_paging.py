#!/usr/bin/env python3
"""Tests for backfill.py read_history_pages (channel history paging).

Neiro's 2026-11-21 proposal, adopted the same day: a single
conversations read returns only --limit messages, so a long outage
with more than that many posts past the cutoff strands the middle
ones forever (the newest page advances max_ts past them). Pages walk
backwards with --latest set to the oldest ts of the previous page
(--latest is inclusive, so the boundary message repeats; admit()
dedupes it by seen key). The cap bounds the read and is reported,
never silent.

Plain stdlib script, no pytest (same style as the other suites).
Run:
  python3 tests/test_backfill_paging.py
Exit 0 when every check passes; each check prints PASS/FAIL.
"""
import os
import sys

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


def msg(ts):
    return {"ts": str(ts), "text": f"m{ts}"}


def make_pages(pages):
    """Stub run_slackcli serving canned pages in --latest order.

    pages: list of (messages, has_more); the stub answers the first
    call with pages[0], then each call carrying --latest <oldest ts of
    the previous page> with the next page. Calls are recorded.
    """
    calls = []

    def fake(args, timeout=60):
        calls.append(list(args))
        idx = 0
        if "--latest" in args:
            latest = args[args.index("--latest") + 1]
            for i, (msgs, _) in enumerate(pages):
                if msgs and msgs[0]["ts"] == latest:
                    idx = i + 1
                    break
            else:
                return {"messages": [], "has_more": False}
        if idx >= len(pages):
            return {"messages": [], "has_more": False}
        msgs, more = pages[idx]
        return {"messages": list(msgs), "has_more": more}

    return fake, calls


FLOOR = 1000.0
orig = backfill.run_slackcli

try:
    # One page, no more: single read, nothing capped.
    fake, calls = make_pages([([msg(1001), msg(1002)], False)])
    backfill.run_slackcli = fake
    msgs, capped = backfill.read_history_pages("C1", "100", FLOOR)
    check("single page returns its messages",
          [m["ts"] for m in msgs] == ["1001", "1002"])
    check("single page is not capped", capped is False)
    check("single page reads once", len(calls) == 1)

    # Two pages: the second call carries --latest = oldest ts of page 1.
    fake, calls = make_pages([
        ([msg(1001), msg(1002)], True),
        ([msg(999), msg(1001)], False),
    ])
    backfill.run_slackcli = fake
    msgs, capped = backfill.read_history_pages("C1", "100", FLOOR)
    check("two pages are combined",
          [m["ts"] for m in msgs] == ["1001", "1002", "999", "1001"])
    check("two pages are not capped", capped is False)
    check("second page passes --latest of the first page's oldest ts",
          "--latest" in calls[1] and calls[1][calls[1].index("--latest") + 1] == "1001")

    # Cap: has_more still set after MAX_HISTORY_PAGES pages.
    fake, calls = make_pages([
        ([msg(1001), msg(1002)], True),
        ([msg(1003), msg(1004)], True),
        ([msg(1005), msg(1006)], True),
        ([msg(1007), msg(1008)], False),
    ])
    backfill.run_slackcli = fake
    msgs, capped = backfill.read_history_pages("C1", "100", FLOOR)
    check("page cap stops after MAX_HISTORY_PAGES reads",
          len(calls) == backfill.MAX_HISTORY_PAGES)
    check("page cap is reported", capped is True)

    # Early stop: the page's newest message is at/below the floor, so
    # older pages can only be older -- no further read.
    fake, calls = make_pages([
        ([msg(900), msg(950)], True),
        ([msg(800), msg(850)], False),
    ])
    backfill.run_slackcli = fake
    msgs, capped = backfill.read_history_pages("C1", "100", FLOOR)
    check("page at/below the floor stops paging",
          len(calls) == 1 and capped is False)

    # Empty page stops paging.
    fake, calls = make_pages([([], False)])
    backfill.run_slackcli = fake
    msgs, capped = backfill.read_history_pages("C1", "100", FLOOR)
    check("empty page returns nothing and is not capped",
          msgs == [] and capped is False)

    # A slackcli failure propagates so the caller can record the error.
    def failing(args, timeout=60):
        raise RuntimeError("slackcli down")
    backfill.run_slackcli = failing
    raised = False
    try:
        backfill.read_history_pages("C1", "100", FLOOR)
    except RuntimeError:
        raised = True
    check("slackcli failure propagates", raised)
finally:
    backfill.run_slackcli = orig

print(f"\n{CHECKS[0]} passed, {CHECKS[1]} failed")
sys.exit(1 if CHECKS[1] else 0)
