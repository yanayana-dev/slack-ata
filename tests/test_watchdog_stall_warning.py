#!/usr/bin/env python3
"""Tests for the watchdog.sh stall warning's ledger counts and the
"worker alive" gate (2026-11-24).

Neiro added "うち処理中 N件" to their stall warning the same day:
a warning that only says "the oldest pending message is N seconds
old" cannot tell "worker stopped" from "busy processing a long
batch". Our side mirrors it: the warning now carries the counts
from Yanayana's own ledger -- "Yanayana側の台帳では未処理 X件・
処理中 Y件です" -- read in one snapshot via
`msgstate.py count-pending --json`.

Neiro then fixed the false alarms themselves (commit ccc2890,
shared 2026-11-24): processing threshold 600 -> 1800s, plus a
3600s episode cooldown. Our analog of the threshold split: our age
metric already excludes processing items, so instead the warning
is gated on processing == 0 -- while any claim is live a worker is
chewing the backlog and the warning must not fire. The cooldown
needs no separate implementation: the flag file already warns once
per stall episode and resets on drain.

The watchdog is run sandboxed like test_watchdog_unit_failed.py:
WATCHDOG_BASE/WATCHDOG_DST_DIR point at temp dirs with identical
unit files (so no reinstall happens), a stub `systemctl` on PATH
answers is-active/is-failed/show, a fake slackcli records warning
sends, and the REAL msgstate.py + inbox_state.py are copied into
the sandbox base so oldest-pending-age and count-pending --json
work for real. No real systemd or Slack calls.

Plain stdlib script, no pytest (same style as the other suites).
Run:
  python3 tests/test_watchdog_stall_warning.py
Exit 0 when every check passes; each check prints PASS/FAIL.
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

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


def msg(event_id, ts, text, received_at):
    return {"event_id": event_id, "received_at": received_at,
            "channel": "C0EXAMPLE001", "channel_type": "channel",
            "user": "U0EXAMPLEUSER", "text": text, "ts": ts,
            "thread_ts": None}


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
        # The real msgstate.py (+ its inbox_state import) so the
        # stall age and the --json counts are computed for real.
        for mod in ("msgstate.py", "inbox_state.py"):
            shutil.copy(os.path.join(ROOT, mod),
                        os.path.join(self.base, mod))
        # Fresh, connected heartbeat so no listener restart happens.
        with open(os.path.join(self.base, "events", "heartbeat.json"),
                  "w", encoding="utf-8") as f:
            f.write(json.dumps(
                {"at": datetime.now(timezone.utc).isoformat(),
                 "connected": True}))
        # No-op hook check.
        with open(os.path.join(self.base, "check_hooks.sh"), "w",
                  encoding="utf-8") as f:
            f.write("#!/bin/sh\nexit 0\n")
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

    def seed_inbox(self, rows):
        with open(os.path.join(self.base, "events", "inbox.jsonl"),
                  "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    def cli_mark(self, key, status):
        r = subprocess.run(
            [sys.executable, os.path.join(self.base, "msgstate.py"),
             "mark", key, status],
            capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, r.stderr
        return r.stdout.strip()

    def run_watchdog(self):
        env = dict(os.environ,
                   PATH=self.bin + ":" + os.environ.get("PATH", ""),
                   WATCHDOG_BASE=self.base,
                   WATCHDOG_DST_DIR=self.dst,
                   WATCHDOG_STALL_FLAG=os.path.join(
                       self.base, "events", "stall.flag"),
                   WATCHDOG_STALL_THRESHOLD="10",
                   WATCHDOG_STALL_CHANNEL="C0EXAMPLE111",
                   WATCHDOG_UNIT_FAIL_CHANNEL="C0EXAMPLE111",
                   STUB_STATE=self.state, STUB_LOG=self.stub_log,
                   WARN_LOG=self.warn_log)
        return subprocess.run(["bash", WATCHDOG], env=env,
                              capture_output=True, text=True)

    def warns(self):
        if not os.path.exists(self.warn_log):
            return []
        with open(self.warn_log, encoding="utf-8") as f:
            return f.readlines()

    def stall_flag(self):
        return os.path.join(self.base, "events", "stall.flag")


def main():
    old = (datetime.now(timezone.utc)
           - timedelta(seconds=1200)).isoformat()
    now = datetime.now(timezone.utc).isoformat()

    # 1. 古い未処理1件＋処理中1件 → ワーカー稼働中なので警告しない
    # （Neiroさんの誤報の形：未処理全件を処理中のときの警告）
    e = Env()
    e.seed_inbox([msg("EvS1", "1794478306.000301", "古い未処理", old),
                  msg("EvS2", "1794478306.000302", "処理中", now)])
    e.cli_mark("EvS2", "processing")
    r = e.run_watchdog()
    check("処理中がある間は警告しない（ワーカー稼働中）",
          r.returncode == 0 and e.warns() == []
          and not os.path.exists(e.stall_flag()))
    # 処理が終わって未処理だけ残れば、次回は警告する
    e.cli_mark("EvS2", "done")
    r = e.run_watchdog()
    warns = e.warns()
    check("処理中が0になれば滞留で警告が1回送られる",
          r.returncode == 0 and len(warns) == 1
          and os.path.exists(e.stall_flag()))
    check("警告文に台帳の未処理・処理中件数が入る",
          len(warns) == 1
          and "Yanayana側の台帳では未処理 1件・処理中 0件です" in warns[0])
    check("警告文に最古の滞留秒数が入る",
          len(warns) == 1 and "最古の未処理メッセージが" in warns[0])
    # 2. 同じ滞留のまま2回目は警告しない（重複抑止）
    r = e.run_watchdog()
    check("繰り返し実行では再警告しない",
          r.returncode == 0 and len(e.warns()) == 1)
    # 3. 未処理が捌けたらフラグが消え、新規警告もない
    e.cli_mark("EvS1", "done")
    r = e.run_watchdog()
    check("滞留解消でフラグが消える",
          r.returncode == 0 and not os.path.exists(e.stall_flag())
          and len(e.warns()) == 1)
    e.cleanup()

    # 4. 処理中だけで未処理が無ければ警告しない（止まったのと
    # 忙しいのを区別する、という今回の改善の肝）
    e = Env()
    e.seed_inbox([msg("EvS3", "1794478306.000303", "処理中だけ", now)])
    e.cli_mark("EvS3", "processing")
    r = e.run_watchdog()
    check("処理中だけでは警告しない",
          r.returncode == 0 and e.warns() == []
          and not os.path.exists(e.stall_flag()))
    e.cleanup()

    print(f"{CHECKS[0]} passed, {CHECKS[1]} failed")
    sys.exit(0 if CHECKS[1] == 0 else 1)


if __name__ == "__main__":
    main()
