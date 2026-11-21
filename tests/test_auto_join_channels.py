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
import json
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
DM = {"id": "D0EXAMPLE004", "is_member": False}
MPIM = {"id": "G0EXAMPLE005", "is_private": True, "is_member": False}

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

# greet: True only when a greeting actually went out in this run.
# channel_send_once.py exits 0 for both "sent" and "already-sent",
# so a bare returncode check records greeted=true for a 2nd+
# greeting that was never sent (Claude Code audit 2026-11-21).
check("greet is True when the greeting was sent",
      aj.greet("C0EXAMPLE101",
               runner=fake_runner_factory(stdout="sent 1795000000.1\n")) is True)
check("greet is False when the greeting was already sent",
      aj.greet("C0EXAMPLE101",
               runner=fake_runner_factory(
                   stdout="already-sent (1795000000.1)\n")) is False)
check("greet is False on a nonzero exit",
      aj.greet("C0EXAMPLE101",
               runner=fake_runner_factory(returncode=2, stdout="")) is False)

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


def raises_state_error(fn):
    try:
        fn()
    except aj.StateError:
        return True
    return False


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
    check("load_state on a corrupt state with no backup raises, "
          "never starts empty",
          raises_state_error(lambda: aj.load_state(p)))

# State backup (2026-11-18, hardened from Yama's .bak proposal):
# save_state keeps the previous good state as .bak, and load_state
# falls back to it when the main file is lost or corrupt, writing the
# recovered state back to the main file at once. A main file that
# parses but lost a key must never overwrite a good backup, and when
# both files are unusable load_state raises instead of starting empty.
with tempfile.TemporaryDirectory() as d:
    p = os.path.join(d, "state.json")
    first = {"member": ["C0EXAMPLE102"], "removed": ["C0EXAMPLE107"]}
    second = {"member": ["C0EXAMPLE102"],
              "removed": ["C0EXAMPLE107", "C0EXAMPLE110"]}
    aj.save_state(first, p)
    check("first save creates no backup yet",
          not os.path.exists(p + ".bak"))
    aj.save_state(second, p)
    check("second save backs up the previous state",
          aj.load_state(p + ".bak") == first)
    os.remove(p)
    check("load_state falls back to the backup when the main file is gone",
          aj.load_state(p) == first)
    check("fallback from the backup writes the state back to the main file",
          os.path.exists(p) and aj.load_state(p) == first)
    aj.save_state(second, p)
    with open(p, "w", encoding="utf-8") as f:
        f.write("{not json")
    check("load_state falls back to the backup when the main file is corrupt",
          aj.load_state(p) == first)
    with open(p, "w", encoding="utf-8") as f:
        f.write("{not json")
    with open(p + ".bak", "w", encoding="utf-8") as f:
        f.write("{not json")
    check("load_state raises when both main and backup are corrupt",
          raises_state_error(lambda: aj.load_state(p)))
    aj.save_state(first, p)
    aj.save_state(second, p)
    with open(p, "w", encoding="utf-8") as f:
        f.write("{not json")
    aj.save_state({"member": ["C0EXAMPLE102"], "removed": ["C0EXAMPLE110"]}, p)
    check("saving over a corrupt main file keeps the good backup",
          aj.load_state(p + ".bak") == first)
    with open(p, "w", encoding="utf-8") as f:
        f.write("{}")
    aj.save_state({"member": ["C0EXAMPLE102"], "removed": ["C0EXAMPLE110"]}, p)
    check("saving over an empty-dict main file keeps the good backup",
          aj.load_state(p + ".bak") == first)
    with open(p, "w", encoding="utf-8") as f:
        f.write('{"member": ["C0EXAMPLE102"]}')
    aj.save_state({"member": ["C0EXAMPLE102"], "removed": ["C0EXAMPLE110"]}, p)
    check("saving over a key-missing main file keeps the good backup",
          aj.load_state(p + ".bak") == first)

# Count check (adopted 2026-11-18 from Neiro's be88d0c, narrowed to
# save time): a valid-but-empty main file must not overwrite a backup
# that still holds channels; when the backup is empty too, saving
# proceeds as normal.
with tempfile.TemporaryDirectory() as d:
    p = os.path.join(d, "state.json")
    first = {"member": ["C0EXAMPLE102"], "removed": ["C0EXAMPLE107"]}
    aj.save_state(first, p)
    aj.save_state({"member": ["C0EXAMPLE102"],
                   "removed": ["C0EXAMPLE107", "C0EXAMPLE110"]}, p)
    with open(p, "w", encoding="utf-8") as f:
        f.write('{"member": [], "removed": []}')
    aj.save_state({"member": ["C0EXAMPLE102"], "removed": ["C0EXAMPLE110"]}, p)
    check("saving over a valid-but-empty main file keeps a non-empty backup",
          aj.load_state(p + ".bak") == first)
with tempfile.TemporaryDirectory() as d:
    p = os.path.join(d, "state.json")
    aj.save_state({"member": [], "removed": []}, p)
    aj.save_state({"member": [], "removed": []}, p)
    check("empty state with an empty backup still saves normally",
          aj.load_state(p) == {"member": [], "removed": []}
          and aj.load_state(p + ".bak") == {"member": [], "removed": []})

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

# Last-status file + transient_retries (adopted 2026-11-19 from
# Yama's self-review share, candidate 10): every run overwrites
# one status file (never on --dry-run), and retries absorbed by a
# successful read are counted separately from final errors.
def flaky_list_fn(failures, result):
    calls = [0]

    def fn():
        calls[0] += 1
        if calls[0] <= failures:
            raise RuntimeError("Remote end closed")
        return result
    return fn


check("retries: first attempt success counts 0",
      aj.list_conversations_with_retries(
          list_fn=flaky_list_fn(0, [NEW_PUBLIC]),
          sleep=lambda s: None) == ([NEW_PUBLIC], 0))
check("retries: two absorbed failures count 2",
      aj.list_conversations_with_retries(
          list_fn=flaky_list_fn(2, [NEW_PUBLIC]),
          sleep=lambda s: None) == ([NEW_PUBLIC], 2))
def _raises(fn):
    try:
        fn()
    except RuntimeError:
        return True
    return False


check("retries: a failing read raises after max attempts",
      _raises(lambda: aj.list_conversations_with_retries(
          list_fn=flaky_list_fn(99, []), max_attempts=3,
          sleep=lambda s: None)))


with tempfile.TemporaryDirectory() as d:
    p = os.path.join(d, "last_status.json")
    rec = aj.status_record("success", 3, 0, 0, 0, 1)
    check("status_record carries the run fields",
          rec["status"] == "success" and rec["checked"] == 3
          and rec["transient_retries"] == 1 and "ended_at" in rec)
    aj.write_status(rec, p)
    check("write_status round-trips the record",
          json.load(open(p, encoding="utf-8")) == rec)
    aj.write_status(aj.status_record("error", 0, 0, 0, 1, 0), p)
    check("write_status overwrites with just the latest run",
          json.load(open(p, encoding="utf-8"))["status"] == "error")

# main() writes the status file: a normal run (one join, no heal),
# a failing list read (error status), and a dry run (no file).
with tempfile.TemporaryDirectory() as d:
    orig = {k: getattr(aj, k) for k in
            ("list_conversations", "load_state", "save_state",
             "join_channel", "greet", "backfill_channel",
             "channels_with_baseline", "STATUS_PATH", "LOG_PATH",
             "write_status")}
    try:
        aj.STATUS_PATH = os.path.join(d, "last_status.json")
        aj.LOG_PATH = os.path.join(d, "auto_join.jsonl")
        _orig_write_status = orig["write_status"]
        # write_status binds its default path at def time, so route
        # the test run's writes at the patched STATUS_PATH.
        aj.write_status = lambda rec: _orig_write_status(
            rec, aj.STATUS_PATH)
        aj.load_state = lambda path=aj.STATE_PATH: {
            "member": ["C0EXAMPLE102"], "removed": []}
        aj.save_state = lambda state, path=aj.STATE_PATH: None
        aj.join_channel = lambda cid, runner=None: {"is_member": True}
        aj.greet = lambda cid, runner=None: True
        aj.backfill_channel = lambda cid, runner=None: {
            "recovered": 0, "checked": 0, "errors": []}
        aj.channels_with_baseline = lambda p=aj.INBOX_PATH: {
            "C0EXAMPLE102"}
        aj.list_conversations = lambda runner=None: [
            dict(NEW_PUBLIC), dict(JOINED)]
        check("main writes a success status on a normal run",
              aj.main([]) == 0
              and json.load(open(aj.STATUS_PATH,
                                  encoding="utf-8"))["status"] == "success"
              and json.load(open(aj.STATUS_PATH,
                                  encoding="utf-8"))["joined"] == 1
              and json.load(open(aj.STATUS_PATH,
                                  encoding="utf-8"))["checked"] == 2)

        def failing_list(runner=None):
            raise RuntimeError("Remote end closed")
        aj.list_conversations = failing_list
        check("main writes an error status when the list fails",
              aj.main([]) == 2
              and json.load(open(aj.STATUS_PATH,
                                  encoding="utf-8"))["status"] == "error"
              and json.load(open(aj.STATUS_PATH,
                                  encoding="utf-8"))["errors"] == 1)
        os.remove(aj.STATUS_PATH)
        aj.list_conversations = lambda runner=None: [dict(JOINED)]
        check("main writes no status file on a dry run",
              aj.main(["--dry-run"]) == 0
              and not os.path.exists(aj.STATUS_PATH))
    finally:
        for k, v in orig.items():
            setattr(aj, k, v)

print(f"\n{CHECKS[0]} passed, {CHECKS[1]} failed")
sys.exit(1 if CHECKS[1] else 0)
