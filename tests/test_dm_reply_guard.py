#!/usr/bin/env python3
"""Tests for dm_reply_guard.py: the DM代行 consecutive-reply valve.

The guard counts consecutive proxy (auto-reply) sends per DM
conversation from history + a small record file, and stops auto-
replying at LIMIT=3 so a back-and-forth can never run away (Yama's
「送信と同時に代行の記録を残す」 shape). These tests replace the
user-token helper with a stub script (DM_REPLY_GUARD_HELPER) that
answers history from a scenario JSON file, and run dm_reply_guard.py
as a subprocess with SLACK_LISTENER_BASE pointed at a temp dir --
the same isolation pattern as test_dm_poll.py. No real Slack call
is made and the real events/ directory is never touched.

Plain stdlib script, no pytest (same style as the other suites).
Run:
  python3 tests/test_dm_reply_guard.py
Exit 0 when every check passes; each check prints PASS/FAIL.
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
GUARD = os.path.join(ROOT, "dm_reply_guard.py")

CHECKS = [0, 0]  # [passed, failed]

ATA = "U0EXAMPLEOWNER"        # あた (proxy sends and own messages share this id)
OTHER = "U0EXAMPLEUSER2"      # Yama
THIRD = "U0EXAMPLEUSER"      # Neiro
CONV_A = "D0EXAMPLE001"

STUB = r'''
import json, os, sys

scenario = json.load(open(os.environ["DM_GUARD_STUB_SCENARIO"]))
cmd = sys.argv[1]
args = sys.argv[2:]

def opt(name, default=None):
    if name in args:
        return args[args.index(name) + 1]
    return default

if cmd == "history":
    channel = opt("--channel")
    if channel in scenario.get("fail_history", []):
        print("stub history failure", file=sys.stderr)
        sys.exit(1)
    msgs = list(scenario.get("history", {}).get(channel, []))
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


def msg(ts, user, text="hi"):
    return {"ts": ts, "user": user, "text": text}


class Rig:
    def __init__(self):
        self.base = tempfile.mkdtemp(prefix="dm-guard-test-")
        self.events = os.path.join(self.base, "events")
        self.scenario_path = os.path.join(self.base, "scenario.json")
        self.stub_path = os.path.join(self.base, "stub_helper.py")
        with open(self.stub_path, "w", encoding="utf-8") as f:
            f.write(STUB)
        self.scenario = {"history": {CONV_A: []}, "fail_history": []}
        self.save_scenario()

    def save_scenario(self):
        with open(self.scenario_path, "w", encoding="utf-8") as f:
            json.dump(self.scenario, f)

    def set_history(self, messages):
        self.scenario["history"][CONV_A] = messages
        self.save_scenario()

    def run_guard(self, *args):
        env = dict(os.environ)
        env["SLACK_LISTENER_BASE"] = self.base
        env["DM_REPLY_GUARD_HELPER"] = self.stub_path
        env["DM_GUARD_STUB_SCENARIO"] = self.scenario_path
        return subprocess.run([sys.executable, GUARD, *args],
                              capture_output=True, text=True, env=env)

    def state(self):
        path = os.path.join(self.events, "dm_reply_guard.json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}


def main():
    rig = Rig()

    # --- consecutive_proxy: pure counting, no subprocess ---
    sys.path.insert(0, ROOT)
    import dm_reply_guard as g

    # Empty history and counterpart-only history: 0.
    check("count empty history is 0",
          g.consecutive_proxy([], ["1.0"]) == 0)
    check("count counterpart-only history is 0",
          g.consecutive_proxy([msg("1.0", OTHER), msg("2.0", THIRD)], ["1.0"]) == 0)

    # Alternating counterpart / proxy: the counterpart's messages do
    # not break the chain, so two proxy sends count as 2.
    hist = [msg("1.0", OTHER), msg("2.0", ATA), msg("3.0", OTHER), msg("4.0", ATA)]
    check("count alternating proxy sends is 2",
          g.consecutive_proxy(hist, ["2.0", "4.0"]) == 2)

    # あた herself posting after the proxies breaks the chain: only
    # the proxies after her message count.
    hist = [msg("1.0", OTHER), msg("2.0", ATA), msg("3.0", ATA),
            msg("4.0", OTHER), msg("5.0", ATA)]
    check("count stops at ata's own message",
          g.consecutive_proxy(hist, ["2.0", "5.0"]) == 1)

    # An unrecorded あた message at the tail means あた wrote it
    # herself: the chain is 0.
    hist = [msg("1.0", OTHER), msg("2.0", ATA), msg("3.0", ATA)]
    check("count is 0 when newest ata message is unrecorded",
          g.consecutive_proxy(hist, ["2.0"]) == 0)

    # Bot/subtype messages without a user are skipped, not counted.
    hist = [{"ts": "1.0", "bot_id": "B0X", "text": "bot"},
            msg("2.0", OTHER), msg("3.0", ATA)]
    check("count skips messages without a user",
          g.consecutive_proxy(hist, ["3.0"]) == 1)

    # --- check / record / reset through the CLI ---
    # Fresh conversation: ALLOW with count 0.
    r = rig.run_guard("check", "--channel", CONV_A)
    check("fresh conversation allows", r.returncode == 0 and "ALLOW" in r.stdout, r.stdout.strip())

    # Record two proxy sends (with matching history): still ALLOW.
    rig.run_guard("record", "--channel", CONV_A, "--ts", "10.000001")
    rig.run_guard("record", "--channel", CONV_A, "--ts", "12.000001")
    rig.set_history([msg("9.0", OTHER), msg("10.000001", ATA),
                     msg("11.0", OTHER), msg("12.000001", ATA)])
    r = rig.run_guard("check", "--channel", CONV_A)
    check("two consecutive proxies still allow",
          r.returncode == 0 and "consecutive=2" in r.stdout, r.stdout.strip())

    # Third proxy send: STOP.
    rig.run_guard("record", "--channel", CONV_A, "--ts", "14.000001")
    rig.set_history([msg("9.0", OTHER), msg("10.000001", ATA),
                     msg("11.0", OTHER), msg("12.000001", ATA),
                     msg("13.0", OTHER), msg("14.000001", ATA)])
    r = rig.run_guard("check", "--channel", CONV_A)
    check("three consecutive proxies stop",
          r.returncode == 1 and "STOP" in r.stdout and "consecutive=3" in r.stdout,
          r.stdout.strip())

    # Recording the same sent ts twice does not double-count.
    rig.run_guard("record", "--channel", CONV_A, "--ts", "14.000001")
    check("duplicate record does not double-count",
          rig.state()[CONV_A]["count"] == 3, str(rig.state()))

    # あた herself posting resets the chain: check heals the stored
    # count to 0 and allows again.
    rig.set_history([msg("9.0", OTHER), msg("10.000001", ATA),
                     msg("11.0", OTHER), msg("12.000001", ATA),
                     msg("13.0", OTHER), msg("14.000001", ATA),
                     msg("15.0", ATA), msg("16.0", OTHER)])
    r = rig.run_guard("check", "--channel", CONV_A)
    check("ata's own message resets and allows",
          r.returncode == 0 and "consecutive=0" in r.stdout, r.stdout.strip())
    check("reset is persisted to state",
          rig.state()[CONV_A]["count"] == 0, str(rig.state()))

    # History unreadable: fail closed (STOP), count untouched.
    rig.scenario["fail_history"] = [CONV_A]
    rig.save_scenario()
    r = rig.run_guard("check", "--channel", CONV_A)
    check("unreadable history fails closed",
          r.returncode == 1 and "STOP" in r.stdout and "history-unreadable" in r.stdout,
          r.stdout.strip())
    rig.scenario["fail_history"] = []
    rig.save_scenario()

    # reset clears the stored count even at the limit.
    rig.run_guard("record", "--channel", CONV_A, "--ts", "20.000001")
    rig.run_guard("record", "--channel", CONV_A, "--ts", "22.000001")
    rig.run_guard("record", "--channel", CONV_A, "--ts", "24.000001")
    r = rig.run_guard("reset", "--channel", CONV_A)
    check("reset clears count",
          r.returncode == 0 and rig.state()[CONV_A]["count"] == 0, r.stdout.strip())

    # Usage errors exit 2.
    r = rig.run_guard("check")
    check("check without --channel exits 2", r.returncode == 2)
    r = rig.run_guard("record", "--channel", CONV_A)
    check("record without --ts exits 2", r.returncode == 2)

    print(f"\n{CHECKS[0]} passed, {CHECKS[1]} failed")
    return 1 if CHECKS[1] else 0


if __name__ == "__main__":
    sys.exit(main())
