#!/usr/bin/env python3
"""Tests for backfill.py compute_floor (the --window-minutes floor).

Neiro's 2026-11-18 fix (neiro-slack-system 96f8597) showed the gap:
when the bot joins a new channel, posts made during the up-to-5-minute
wait must be recovered. Yanayana's backfill used the newest seen
message *anywhere* as the floor, so a busy other channel could push
the floor above the new channel's gap posts and skip them. The window
floor (now minus N minutes) has no such cross-channel leak.

Plain stdlib script, no pytest (same style as the other suites).
Run:
  python3 tests/test_backfill_window.py
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


NOW = 1794964000.0
# Seen file's newest message is 60s ago, from another channel.
SEEN = ["ts:C0EXAMPLE001:1794963940.0", "ts:C0EXAMPLE001:1794963000.0"]
# A gap post in the just-joined channel, 300s ago -- newer than the
# 15-minute window start, but older than the newest seen message.
GAP_TS = NOW - 300

# Window floor beats the global newest-seen floor: the gap post is
# above the window floor, so it would be admitted.
floor = backfill.compute_floor(SEEN, window_minutes=15, now=NOW)
check("window floor is now minus 15 minutes",
      floor == NOW - 900)
check("gap post is newer than the window floor",
      GAP_TS > floor)
check("gap post is older than the global newest-seen floor",
      GAP_TS < 1794963940.0)

# Explicit --floor-epoch still wins over the window.
check("explicit floor-arg wins over the window",
      backfill.compute_floor(SEEN, floor_arg=1794963500.0,
                             window_minutes=15, now=NOW) == 1794963500.0)

# Without a window, the old behaviour is unchanged: newest seen ts.
check("no window keeps the newest-seen floor",
      backfill.compute_floor(SEEN, now=NOW) == 1794963940.0)

# First run (no seen ts keys) still defaults to now, never replays.
check("empty seen with no window defaults to now",
      backfill.compute_floor([], now=NOW) == NOW)
check("empty seen with a window uses the window",
      backfill.compute_floor([], window_minutes=15, now=NOW) == NOW - 900)

# Non-ts seen keys are ignored when computing the newest-seen floor.
check("non-ts seen keys are ignored",
      backfill.compute_floor(["Ev0XXXX", "ts:C0EXAMPLE001:1794963900.0"],
                             now=NOW) == 1794963900.0)

print(f"\n{CHECKS[0]} passed, {CHECKS[1]} failed")
sys.exit(1 if CHECKS[1] else 0)
