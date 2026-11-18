#!/usr/bin/env python3
"""Tests for auto_join_channels.py: which conversations get joined.

The filter is the whole safety story of the auto-join: public,
non-archived channels the bot is not in yet -- and nothing else.
Private channels, archived channels, DMs and channels the bot
already belongs to must never be returned.

Plain stdlib script, no pytest (same style as the other suites).
Run:
  python3 tests/test_auto_join_channels.py
Exit 0 when every check passes; each check prints PASS/FAIL.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import auto_join_channels as aj

CHECKS = [0, 0]  # [passed, failed]


def check(name, cond):
    if cond:
        CHECKS[0] += 1
        print(f"PASS {name}")
    else:
        CHECKS[1] += 1
        print(f"FAIL {name}")


def ids(convs):
    return [c["id"] for c in aj.joinable(convs)]


NEW_PUBLIC = {"id": "C0EXAMPLE101", "name": "new", "is_private": False,
              "is_archived": False, "is_member": False}
JOINED = {"id": "C0EXAMPLE102", "name": "old", "is_private": False,
          "is_archived": False, "is_member": True}
PRIVATE = {"id": "C0EXAMPLE103", "name": "priv", "is_private": True,
           "is_archived": False, "is_member": False}
ARCHIVED = {"id": "C0EXAMPLE104", "name": "arch", "is_private": False,
            "is_archived": True, "is_member": False}
DM = {"id": "D0DM0000001", "is_member": False}
MPIM = {"id": "G0GROUP0001", "is_private": True, "is_member": False}

check("new public channel is joinable", ids([NEW_PUBLIC]) == ["C0EXAMPLE101"])
check("already-member channel is skipped", ids([JOINED]) == [])
check("private channel is never self-joined", ids([PRIVATE]) == [])
check("archived channel is skipped", ids([ARCHIVED]) == [])
check("DM is skipped", ids([DM]) == [])
check("group DM is skipped", ids([MPIM]) == [])
check("mixed list keeps only the new public channel",
      ids([JOINED, PRIVATE, NEW_PUBLIC, ARCHIVED, DM]) == ["C0EXAMPLE101"])
check("empty list joins nothing", ids([]) == [])
check("None joins nothing", ids(None) == [])
check("missing flags default to joinable for a plain public channel",
      ids([{"id": "C0EXAMPLE105"}]) == ["C0EXAMPLE105"])

# backfill_channel: parses the summary, never raises, and always
# passes --window-minutes 15 --channel <id> so only the just-joined
# channel is scanned, with a time-window floor (Neiro's 2026-11-18
# fix) instead of the global newest-seen floor.
from types import SimpleNamespace


def fake_runner_factory(stdout="{\"recovered\": 2, \"checked\": 5, \"errors\": []}",
                        returncode=0, calls=None):
    def fake(argv, **kwargs):
        if calls is not None:
            calls.append(argv)
        return SimpleNamespace(returncode=returncode, stdout=stdout,
                               stderr="")
    return fake


calls = []
summary = aj.backfill_channel("C0EXAMPLE101",
                              runner=fake_runner_factory(calls=calls))
check("backfill_channel returns the parsed summary",
      summary == {"recovered": 2, "checked": 5, "errors": []})
check("backfill_channel runs backfill.py --channel <id>",
      len(calls) == 1 and calls[0][-2:] == ["--channel", "C0EXAMPLE101"]
      and calls[0][1].endswith("backfill.py"))
check("backfill_channel passes --window-minutes 15 for the gap",
      "--window-minutes" in calls[0]
      and calls[0][calls[0].index("--window-minutes") + 1] == "15")
check("backfill_channel returns None on nonzero exit",
      aj.backfill_channel("C0EXAMPLE101",
                          runner=fake_runner_factory(returncode=1)) is None)


def raising_runner(argv, **kwargs):
    raise RuntimeError("boom")


check("backfill_channel returns None when the run raises",
      aj.backfill_channel("C0EXAMPLE101", runner=raising_runner) is None)
check("backfill_channel returns a marker dict on unparsable output",
      aj.backfill_channel(
          "C0EXAMPLE101",
          runner=fake_runner_factory(stdout="no json here")).get(
              "errors") == ["unparsable"])

# Removed-channel guard (adopted 2026-11-18 from Yama's auto-join
# share): a channel the bot was removed from is never auto-rejoined,
# and a manual re-invite clears the record.
REMOVED = {"id": "C0EXAMPLE107", "name": "gone", "is_private": False,
           "is_archived": False, "is_member": False}
check("removed channel is never auto-rejoined",
      [c["id"] for c in aj.joinable([NEW_PUBLIC, REMOVED],
                                    removed={"C0EXAMPLE107"})]
      == ["C0EXAMPLE101"])
check("joinable without a removed set keeps the old behaviour",
      ids([NEW_PUBLIC]) == ["C0EXAMPLE101"])

prev = {"member": ["C0EXAMPLE102", "C0EXAMPLE107"], "removed": []}
now = [dict(JOINED), dict(REMOVED)]
check("update_removed records a channel the bot left",
      aj.update_removed(now, prev)["removed"] == ["C0EXAMPLE107"])
check("update_removed keeps the current member list",
      aj.update_removed(now, prev)["member"] == ["C0EXAMPLE102"])
check("update_removed clears the record on manual re-invite",
      aj.update_removed(
          [dict(JOINED), dict(REMOVED, is_member=True)],
          {"member": ["C0EXAMPLE102"], "removed": ["C0EXAMPLE107"]}
      )["removed"] == [])
check("update_removed ignores a channel that vanished from listing",
      aj.update_removed([dict(JOINED)],
                        {"member": ["C0EXAMPLE102", "C0EXAMPLE106"],
                         "removed": []})["removed"] == [])
check("update_removed ignores an archived former channel",
      aj.update_removed([dict(JOINED),
                         dict(REMOVED, is_archived=True)],
                        {"member": ["C0EXAMPLE102", "C0EXAMPLE107"],
                         "removed": []})["removed"] == [])

import tempfile
with tempfile.TemporaryDirectory() as d:
    p = os.path.join(d, "state.json")
    check("load_state on a missing file is empty",
          aj.load_state(p) == {"member": [], "removed": []})
    aj.save_state({"member": ["C0EXAMPLE102"], "removed": ["C0EXAMPLE107"]}, p)
    check("save/load round-trips the state",
          aj.load_state(p) == {"member": ["C0EXAMPLE102"],
                               "removed": ["C0EXAMPLE107"]})
    with open(p, "w", encoding="utf-8") as f:
        f.write("{not json")
    check("load_state on corrupt state is empty, not a crash",
          aj.load_state(p) == {"member": [], "removed": []})

# Baseline self-heal (adopted 2026-11-18 from Neiro's auto-join.py):
# a member channel with no inbox baseline gets the gap backfill;
# one with a baseline, a non-member, or an archived/private/DM
# channel does not.
with tempfile.TemporaryDirectory() as d:
    p = os.path.join(d, "inbox.jsonl")
    with open(p, "w", encoding="utf-8") as f:
        f.write('{"channel": "C0EXAMPLE102", "ts": "1.0"}\n')
        f.write('not json\n')
        f.write('\n')
        f.write('{"channel": "C0EXAMPLE109", "ts": "2.0"}\n')
    check("channels_with_baseline collects channel ids",
          aj.channels_with_baseline(p) == {"C0EXAMPLE102", "C0EXAMPLE109"})
    check("channels_with_baseline on a missing file is empty",
          aj.channels_with_baseline(os.path.join(d, "nope.jsonl")) == set())

NOBASE = {"id": "C0EXAMPLE108", "name": "hand", "is_private": False,
          "is_archived": False, "is_member": True}
check("member channel without a baseline needs a seed",
      [c["id"] for c in aj.needs_baseline_seed(
          [JOINED, NOBASE], {"C0EXAMPLE102"})] == ["C0EXAMPLE108"])
check("member channel with a baseline needs no seed",
      aj.needs_baseline_seed([JOINED], {"C0EXAMPLE102"}) == [])
check("non-member channel without a baseline is a join, not a heal",
      aj.needs_baseline_seed([NEW_PUBLIC], set()) == [])
check("archived member channel without a baseline is not healed",
      aj.needs_baseline_seed([dict(NOBASE, is_archived=True)], set())
      == [])
check("needs_baseline_seed on empty input is empty",
      aj.needs_baseline_seed([], set()) == [])

print(f"\n{CHECKS[0]} passed, {CHECKS[1]} failed")
sys.exit(1 if CHECKS[1] else 0)
