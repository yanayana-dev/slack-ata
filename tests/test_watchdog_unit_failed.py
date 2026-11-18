#!/usr/bin/env python3
"""Tests for the watchdog.sh unit-failure warnings (adopted 2026-11-19
from Neiro's design).

watchdog.sh runs `systemctl is-failed` on slack-sync-check.service
and slack-auto-join.service: a failed unit warns once to Slack
(deduped by one flag file per unit), the flag is removed only when
the unit has run successfully again (Result=success), and a unit
that is merely running or has never run leaves the flag alone.

The watchdog is run sandboxed: WATCHDOG_BASE/WATCHDOG_DST_DIR point
at temp dirs with identical unit files (so no reinstall happens),
a stub `systemctl` on PATH answers is-active/is-failed/show from a
state file, and a fake slackcli records warning sends. No real
systemd or Slack calls.

Plain stdlib script, no pytest (same style as the other suites).
Run:
  python3 tests/test_watchdog_unit_failed.py
Exit 0 when every check passes; each check prints PASS/FAIL.
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WATCHDOG = os.path.join(ROOT, "watchdog.sh")

CHECKS = [0, 0]  # [passed, failed]


def check(name, cond):
    if cond:
        CHECKS[0] += 1
        print(f"PASS {name}")
    else:
        CHECKS[1] += 1
        print(f"FAIL {name}")


STUB_SYSTEMCTL = """#!/usr/bin/env bash
# Stub systemctl: answers from $STUB_STATE ("unit state" lines,
# state in failed|ok|running). Logs every call to $STUB_LOG.
echo "$*" >> "$STUB_LOG"
cmd="$1"; shift
case "$cmd" in
  is-active) exit 0 ;;
  is-failed)
    unit="${@: -1}"
    st=$(grep "^$unit " "$STUB_STATE" 2>/dev/null | awk '{print $2}')
    [ "$st" = "failed" ] && exit 0 || exit 1
    ;;
  show)
    unit="${@: -1}"
    st=$(grep "^$unit " "$STUB_STATE" 2>/dev/null | awk '{print $2}')
    case "$st" in
      failed) echo "exit-code" ;;
      running) echo "" ;;
      *) echo "success" ;;
    esac
    exit 0
    ;;
  *) exit 0 ;;
esac
"""

UNITS = [
    "slack-listener.service",
    "slack-listener-watchdog.service", "slack-listener-watchdog.timer",
    "slack-reconcile.service", "slack-reconcile.timer",
    "slack-dm-poll.service", "slack-dm-poll.timer",
    "slack-auto-join.service", "slack-auto-join.timer",
    "slack-sync-check.service", "slack-sync-check.timer",
]


class Env:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = os.path.join(self.tmp.name, "base")
        self.dst = os.path.join(self.tmp.name, "dst")
        self.bin = os.path.join(self.tmp.name, "bin")
        os.makedirs(os.path.join(self.base, "events"))
        os.makedirs(os.path.join(self.base, "bin"))
        os.makedirs(self.dst)
        os.makedirs(self.bin)
        # Identical unit files on both sides so no reinstall happens.
        for u in UNITS:
            src = os.path.join(ROOT, u)
            if os.path.exists(src):
                shutil.copy(src, os.path.join(self.base, u))
                shutil.copy(src, os.path.join(self.dst, u))
        # Fresh, connected heartbeat so no listener restart happens.
        with open(os.path.join(self.base, "events", "heartbeat.json"),
                  "w", encoding="utf-8") as f:
            f.write(json.dumps({"at": "2026-11-19T00:00:00+00:00",
                                "connected": True}))
        # No-op hook check and msgstate (no pending -> no stall warn).
        with open(os.path.join(self.base, "check_hooks.sh"), "w",
                  encoding="utf-8") as f:
            f.write("#!/bin/sh\nexit 0\n")
        with open(os.path.join(self.base, "msgstate.py"), "w",
                  encoding="utf-8") as f:
            f.write("import sys\n")
        self.state = os.path.join(self.tmp.name, "stub_state")
        self.stub_log = os.path.join(self.tmp.name, "stub.log")
        self.warn_log = os.path.join(self.tmp.name, "warns.log")
        with open(self.state, "w", encoding="utf-8"):
            pass
        with open(os.path.join(self.bin, "systemctl"), "w",
                  encoding="utf-8") as f:
            f.write(STUB_SYSTEMCTL)
        os.chmod(os.path.join(self.bin, "systemctl"),
                 stat.S_IRWXU)
        # Fake slackcli under BASE/bin (watchdog calls $BASE/bin/slackcli).
        with open(os.path.join(self.base, "bin", "slackcli"), "w",
                  encoding="utf-8") as f:
            f.write("#!/bin/sh\necho SEND \"$@\" >> \"$WARN_LOG\"\n")
        os.chmod(os.path.join(self.base, "bin", "slackcli"),
                 stat.S_IRWXU)

    def cleanup(self):
        self.tmp.cleanup()

    def set_unit(self, unit, state):
        lines = []
        if os.path.exists(self.state):
            with open(self.state, encoding="utf-8") as f:
                lines = [ln for ln in f.read().splitlines()
                         if not ln.startswith(unit + " ")]
        lines.append(f"{unit} {state}")
        with open(self.state, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    def run_watchdog(self):
        env = dict(os.environ,
                   PATH=self.bin + ":" + os.environ.get("PATH", ""),
                   WATCHDOG_BASE=self.base,
                   WATCHDOG_DST_DIR=self.dst,
                   WATCHDOG_STALL_FLAG=os.path.join(
                       self.base, "events", "stall.flag"),
                   WATCHDOG_UNIT_FAIL_CHANNEL="C0EXAMPLE111",
                   STUB_STATE=self.state, STUB_LOG=self.stub_log,
                   WARN_LOG=self.warn_log)
        return subprocess.run(["bash", WATCHDOG], env=env,
                              capture_output=True, text=True)

    def warns(self, unit=None):
        if not os.path.exists(self.warn_log):
            return 0
        with open(self.warn_log, encoding="utf-8") as f:
            lines = f.readlines()
        if unit is None:
            return len(lines)
        return sum(1 for ln in lines if unit in ln)

    def flag(self, unit):
        return os.path.join(self.base, "events",
                            f"unit_failed_{unit[:-len('.service')]}.flag")


def main():
    # 1. failed なユニットは1回だけ警告され、フラグが作られる
    e = Env()
    e.set_unit("slack-sync-check.service", "failed")
    r = e.run_watchdog()
    check("failed unit warns once", r.returncode == 0
          and e.warns("slack-sync-check.service") == 1
          and os.path.exists(e.flag("slack-sync-check.service"))
          and e.warns("slack-auto-join.service") == 0)
    # 2. 同じ failed のまま2回目は警告しない（重複抑止）
    r = e.run_watchdog()
    check("repeat run does not re-warn", r.returncode == 0
          and e.warns("slack-sync-check.service") == 1)
    # 3. ユニットが成功したらフラグが消える
    e.set_unit("slack-sync-check.service", "ok")
    r = e.run_watchdog()
    check("success clears flag", r.returncode == 0
          and not os.path.exists(e.flag("slack-sync-check.service")))
    e.cleanup()

    # 4. auto-join の failed も警告される
    e = Env()
    e.set_unit("slack-auto-join.service", "failed")
    r = e.run_watchdog()
    check("auto-join failure warns", r.returncode == 0
          and e.warns("slack-auto-join.service") == 1
          and os.path.exists(e.flag("slack-auto-join.service")))
    e.cleanup()

    # 5. 実行中（failed でも success でもない）のときフラグは残る
    e = Env()
    e.set_unit("slack-sync-check.service", "failed")
    e.run_watchdog()
    check("flag set before running case",
          os.path.exists(e.flag("slack-sync-check.service")))
    e.set_unit("slack-sync-check.service", "running")
    r = e.run_watchdog()
    check("running unit keeps flag", r.returncode == 0
          and os.path.exists(e.flag("slack-sync-check.service"))
          and e.warns("slack-sync-check.service") == 1)
    e.cleanup()

    # 6. 両方とも健全なら警告なし・フラグなし
    e = Env()
    r = e.run_watchdog()
    check("healthy units warn nothing", r.returncode == 0
          and e.warns() == 0
          and not os.path.exists(e.flag("slack-sync-check.service"))
          and not os.path.exists(e.flag("slack-auto-join.service")))
    e.cleanup()

    print(f"{CHECKS[0]} passed, {CHECKS[1]} failed")
    sys.exit(0 if CHECKS[1] == 0 else 1)


if __name__ == "__main__":
    main()
