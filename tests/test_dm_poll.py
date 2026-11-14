#!/usr/bin/env python3
"""Tests for dm_poll.py: polling あた's DMs via the user-token helper.

dm_poll shells out to the custom.slack-user-token connector helper
for conversations.list / conversations.history. These tests replace
the helper with a stub script (DM_POLL_HELPER) that answers from a
scenario JSON file, and run dm_poll.py as a subprocess with
SLACK_LISTENER_BASE pointed at a temp dir -- the same isolation
pattern as test_msgstate.py. No real Slack call is made and the real
events/ directory is never touched.

Plain stdlib script, no pytest (same style as the other suites).
Run:
  python3 tests/test_dm_poll.py
Exit 0 when every check passes; each check prints PASS/FAIL.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DM_POLL = os.path.join(ROOT, "dm_poll.py")

CHECKS = [0, 0]  # [passed, failed]

ATA = "U0EXAMPLEOWNER"        # あた (own messages are skipped)
OTHER = "U0EXAMPLEUSER2"      # Yama
THIRD = "U0EXAMPLEUSER"      # Neiro
CONV_A = "D0AAAAAAAA1"     # im with OTHER
CONV_B = "D0BBBBBBBB2"     # im, empty
CONV_C = "D0CCCCCCCC3"     # mpim

STUB = r'''
import json, os, sys

scenario = json.load(open(os.environ["DM_POLL_STUB_SCENARIO"]))
cmd = sys.argv[1]
args = sys.argv[2:]

def opt(name, default=None):
    if name in args:
        return args[args.index(name) + 1]
    return default

if cmd == "conversations-list":
    if scenario.get("fail_list"):
        print("stub list failure", file=sys.stderr)
        sys.exit(1)
    print(json.dumps({"ok": True, "channels": scenario["conversations"]}))
elif cmd == "history":
    channel = opt("--channel")
    if channel in scenario.get("fail_history", []):
        print("stub history failure", file=sys.stderr)
        sys.exit(1)
    msgs = list(scenario.get("history", {}).get(channel, []))
    oldest = opt("--oldest")
    if oldest is not None and float(oldest) <= 0:
        # Real Slack rejects oldest=0 with ok:false invalid_ts_oldest
        # (HTTP 200, so the helper exits 0 and prints the body).
        print(json.dumps({"ok": False, "error": "invalid_ts_oldest"}))
        sys.exit(0)
    if oldest is not None:
        msgs = [m for m in msgs if float(m["ts"]) > float(oldest)]
    msgs.sort(key=lambda m: float(m["ts"]), reverse=True)
    limit = int(opt("--limit", "200"))
    print(json.dumps({"ok": True, "messages": msgs[:limit],
                      "has_more": len(msgs) > limit}))
else:
    print("unknown subcommand", file=sys.stderr)
    sys.exit(2)
'''


def check(name, cond, detail=""):
    CHECKS[0 if cond else 1] += 1
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not cond else ""))
    return cond


def msg(ts, user, text="hi", **kw):
    m = {"ts": ts, "user": user, "text": text}
    m.update(kw)
    return m


def base_scenario():
    return {
        "conversations": [
            {"id": CONV_A, "is_im": True, "user": OTHER},
            {"id": CONV_B, "is_im": True, "user": THIRD},
            {"id": CONV_C, "is_im": False, "is_mpim": True},
        ],
        "history": {
            CONV_A: [msg("1000.000001", OTHER, "old one"),
                     msg("1002.000002", ATA, "my old note"),
                     msg("1001.000003", OTHER, "old two")],
            CONV_B: [],
            CONV_C: [msg("2000.000001", THIRD, "group old"),
                     msg("2001.000002", OTHER, "group old 2")],
        },
        "fail_history": [],
        "fail_list": False,
    }


class Rig:
    def __init__(self):
        self.base = tempfile.mkdtemp(prefix="dm-poll-test-")
        self.events = os.path.join(self.base, "events")
        self.scenario_path = os.path.join(self.base, "scenario.json")
        self.stub_path = os.path.join(self.base, "stub_helper.py")
        with open(self.stub_path, "w", encoding="utf-8") as f:
            f.write(STUB)
        self.scenario = base_scenario()
        self.save_scenario()

    def save_scenario(self):
        with open(self.scenario_path, "w", encoding="utf-8") as f:
            json.dump(self.scenario, f)

    def run(self):
        env = dict(os.environ)
        env["SLACK_LISTENER_BASE"] = self.base
        env["DM_POLL_HELPER"] = self.stub_path
        env["DM_POLL_STUB_SCENARIO"] = self.scenario_path
        return subprocess.run([sys.executable, DM_POLL],
                              capture_output=True, text=True, env=env,
                              timeout=60)

    def inbox(self):
        path = os.path.join(self.events, "dm_inbox.jsonl")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def state(self):
        with open(os.path.join(self.events, "dm_state.json"),
                  encoding="utf-8") as f:
            return json.load(f)

    def cleanup(self):
        shutil.rmtree(self.base, ignore_errors=True)


def test_first_run_seeds():
    rig = Rig()
    try:
        r = rig.run()
        check("first run exits 0", r.returncode == 0,
              f"rc={r.returncode} err={r.stderr.strip()}")
        check("first run summary counts all conversations, none new",
              "conversations=3" in r.stdout and "new=0" in r.stdout
              and "failed=0" in r.stdout, f"out={r.stdout.strip()}")
        check("first run appends nothing (no history replay)",
              rig.inbox() == [])
        st = rig.state()
        check("watermark seeded at latest ts per conversation",
              st[CONV_A]["watermark"] == "1002.000002"
              and st[CONV_C]["watermark"] == "2001.000002",
              f"state={st}")
        check("empty conversation seeds watermark 0",
              st[CONV_B]["watermark"] == "0.000000")
    finally:
        rig.cleanup()


def test_new_messages_and_idempotency():
    rig = Rig()
    try:
        rig.run()  # seed
        sc = rig.scenario
        sc["history"][CONV_A] += [
            msg("1003.000004", OTHER, "fresh question"),
            msg("1004.000005", ATA, "my own reply"),
            msg("999.000006", OTHER, "below watermark, must be filtered"),
        ]
        # CONV_B was empty at seeding (watermark 0): its first ever
        # message must still be picked up, and the run must not trip
        # Slack's invalid_ts_oldest (regression: live run 2026-11-14
        # failed 6 conversations that way).
        sc["history"][CONV_B] += [msg("3000.000001", THIRD, "first ever dm")]
        rig.save_scenario()
        r = rig.run()
        check("second run exits 0 with no failures", r.returncode == 0
              and "failed=0" in r.stdout,
              f"rc={r.returncode} out={r.stdout.strip()} err={r.stderr.strip()}")
        check("second run appends the new foreign messages",
              "new=2" in r.stdout, f"out={r.stdout.strip()}")
        lines = rig.inbox()
        check("inbox holds exactly the two new records", len(lines) == 2,
              f"lines={lines}")
        if lines:
            rec = lines[0]
            check("record mirrors the inbox shape plus dm_poll fields",
                  rec.get("source") == "dm_poll"
                  and rec.get("channel") == CONV_A
                  and rec.get("conversation") == CONV_A
                  and rec.get("channel_type") == "im"
                  and rec.get("user") == OTHER
                  and rec.get("text") == "fresh question"
                  and rec.get("ts") == "1003.000004"
                  and "received_at" in rec and "thread_ts" in rec
                  and "subtype" in rec,
                  f"rec={rec}")
        if len(lines) == 2:
            check("first message in the zero-watermark conversation lands",
                  lines[1].get("conversation") == CONV_B
                  and lines[1].get("text") == "first ever dm",
                  f"rec={lines[1]}")
        st = rig.state()
        check("watermark advanced past own message to newest seen ts",
              st[CONV_A]["watermark"] == "1004.000005"
              and st[CONV_B]["watermark"] == "3000.000001",
              f"state={st}")
        r = rig.run()
        check("third run with nothing new appends nothing (idempotent)",
              r.returncode == 0 and "new=0" in r.stdout
              and len(rig.inbox()) == 2,
              f"rc={r.returncode} out={r.stdout.strip()}")
    finally:
        rig.cleanup()


def test_partial_and_total_failure():
    rig = Rig()
    try:
        rig.run()  # seed
        sc = rig.scenario
        sc["history"][CONV_A] += [msg("1005.000007", OTHER, "will fail")]
        sc["history"][CONV_C] += [msg("2002.000003", THIRD, "group fresh",
                                      thread_ts="2000.000001")]
        sc["fail_history"] = [CONV_A]
        rig.save_scenario()
        r = rig.run()
        check("partial failure still exits 0", r.returncode == 0,
              f"rc={r.returncode} err={r.stderr.strip()}")
        check("partial failure reported in the summary",
              "failed=1" in r.stdout and "new=1" in r.stdout,
              f"out={r.stdout.strip()}")
        lines = rig.inbox()
        check("healthy conversation's message was appended",
              len(lines) == 1 and lines[0].get("conversation") == CONV_C
              and lines[0].get("channel_type") == "mpim"
              and lines[0].get("thread_ts") == "2000.000001",
              f"lines={lines}")
        st = rig.state()
        check("failed conversation keeps its old watermark",
              st[CONV_A]["watermark"] == "1002.000002"
              and st[CONV_C]["watermark"] == "2002.000003",
              f"state={st}")

        sc["fail_history"] = [CONV_A, CONV_B, CONV_C]
        rig.save_scenario()
        r = rig.run()
        check("every conversation failing exits non-zero",
              r.returncode != 0, f"rc={r.returncode} out={r.stdout.strip()}")

        sc["fail_history"] = []
        sc["fail_list"] = True
        rig.save_scenario()
        r = rig.run()
        check("conversation list failure exits non-zero",
              r.returncode != 0, f"rc={r.returncode} out={r.stdout.strip()}")
    finally:
        rig.cleanup()


def main():
    test_first_run_seeds()
    test_new_messages_and_idempotency()
    test_partial_and_total_failure()
    print(f"\n{CHECKS[0]} passed, {CHECKS[1]} failed")
    return 1 if CHECKS[1] else 0


if __name__ == "__main__":
    sys.exit(main())
