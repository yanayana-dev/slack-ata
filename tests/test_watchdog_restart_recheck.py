#!/usr/bin/env python3
"""Tests for the watchdog.sh heartbeat-restart re-check and cooldown
(adopted 2026-11-19 from Yama's share in thread 1795006461.907139).

watchdog.sh restarts the listener when the heartbeat is stale
(>120s) or not connected. Before restarting it now re-reads the
heartbeat 5s later, so a single stale read does not restart the
listener; and after a successful restart it stamps a cooldown
file, so the same heartbeat cannot trigger another restart within
60s (the next check can land while the listener is still starting
up, and back-to-back/manual runs must not double-restart).

The watchdog is run sandboxed, same harness style as
test_watchdog_unit_failed.py: WATCHDOG_BASE/WATCHDOG_DST_DIR point
at temp dirs with identical unit files (so no reinstall happens),
a stub `systemctl` on PATH answers is-active/is-failed/show from a
state file and logs every call, a stub `sleep` optionally refreshes
the heartbeat (simulating recovery during the 5s re-check), and a
fake slackcli records warning sends. No real systemd or Slack calls.

Plain stdlib script, no pytest (same style as the other suites).
Run:
  python3 tests/test_watchdog_restart_recheck.py
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

STUB_SLEEP = """#!/usr/bin/env bash
# Stub sleep: when SLEEP_MODE=recover, refresh the heartbeat file
# (connected, current mtime) to simulate the listener recovering
# during the 5s re-check wait. Otherwise a no-op.
if [ "$SLEEP_MODE" = "recover" ]; then
  printf '%s' '{"at": "2026-11-19T00:00:00+00:00", "connected": true}' \\
    > "$HB_PATH"
fi
exit 0
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
        # listener.py must exist in the sandbox: watchdog.sh now gates
        # restarts on `python3 -m py_compile $BASE/listener.py`
        # (adopted 2026-11-19 from Yama's share in thread
        # 1795064369.469599).
        if os.path.exists(os.path.join(ROOT, "listener.py")):
            shutil.copy(os.path.join(ROOT, "listener.py"),
                        os.path.join(self.base, "listener.py"))
        self.hb_path = os.path.join(self.base, "events",
                                    "heartbeat.json")
        self.set_heartbeat(connected=True, age=0)
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
        with open(os.path.join(self.bin, "sleep"), "w",
                  encoding="utf-8") as f:
            f.write(STUB_SLEEP)
        os.chmod(os.path.join(self.bin, "sleep"), stat.S_IRWXU)
        # Fake slackcli under BASE/bin (watchdog calls $BASE/bin/slackcli).
        with open(os.path.join(self.base, "bin", "slackcli"), "w",
                  encoding="utf-8") as f:
            f.write("#!/bin/sh\necho SEND \"$@\" >> \"$WARN_LOG\"\n")
        os.chmod(os.path.join(self.base, "bin", "slackcli"),
                 stat.S_IRWXU)
        self.sleep_mode = "noop"

    def cleanup(self):
        self.tmp.cleanup()

    def set_heartbeat(self, connected=True, age=0):
        with open(self.hb_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"at": "2026-11-19T00:00:00+00:00",
                                "connected": connected}))
        mtime = time.time() - age
        os.utime(self.hb_path, (mtime, mtime))

    def set_listener(self, text):
        with open(os.path.join(self.base, "listener.py"), "w",
                  encoding="utf-8") as f:
            f.write(text)

    def differ_unit(self):
        # Make the live unit differ from the installed one so the
        # reinstall branch runs.
        with open(os.path.join(self.dst,
                               "slack-listener.service"), "a",
                  encoding="utf-8") as f:
            f.write("\n# sandbox difference\n")

    def run_watchdog(self):
        env = dict(os.environ,
                   PATH=self.bin + ":" + os.environ.get("PATH", ""),
                   WATCHDOG_BASE=self.base,
                   WATCHDOG_DST_DIR=self.dst,
                   WATCHDOG_STALL_FLAG=os.path.join(
                       self.base, "events", "stall.flag"),
                   WATCHDOG_UNIT_FAIL_CHANNEL="C0EXAMPLE111",
                   STUB_STATE=self.state, STUB_LOG=self.stub_log,
                   WARN_LOG=self.warn_log,
                   HB_PATH=self.hb_path,
                   SLEEP_MODE=self.sleep_mode)
        return subprocess.run(["bash", WATCHDOG], env=env,
                              capture_output=True, text=True)

    def restarts(self):
        if not os.path.exists(self.stub_log):
            return 0
        with open(self.stub_log, encoding="utf-8") as f:
            return sum(1 for ln in f
                       if ln.startswith(
                           "restart slack-listener.service"))

    def cooldown_path(self):
        return os.path.join(self.base, "events",
                            "watchdog_restart.cooldown")


def main():
    # 1. 新しい heartbeat なら再起動しない
    e = Env()
    r = e.run_watchdog()
    check("fresh heartbeat restarts nothing", r.returncode == 0
          and e.restarts() == 0
          and not os.path.exists(e.cooldown_path()))
    e.cleanup()

    # 2. 古い heartbeat（300秒）なら再起動が1回走り、クールダウンファイルが残る
    e = Env()
    e.set_heartbeat(connected=True, age=300)
    r = e.run_watchdog()
    check("stale heartbeat restarts once", r.returncode == 0
          and e.restarts() == 1
          and os.path.exists(e.cooldown_path()))
    # 3. すぐにもう一度走ってもクールダウンで再起動しない
    r = e.run_watchdog()
    check("cooldown suppresses second restart", r.returncode == 0
          and e.restarts() == 1)
    # 4. クールダウンが切れた（60秒以上前の刻印）あとは再び再起動する
    with open(e.cooldown_path(), "w", encoding="utf-8") as f:
        f.write(str(int(time.time()) - 120))
    r = e.run_watchdog()
    check("restart resumes after cooldown expires", r.returncode == 0
          and e.restarts() == 2)
    e.cleanup()

    # 5. 5秒の再確認のあいだに heartbeat が回復したら再起動しない
    e = Env()
    e.set_heartbeat(connected=True, age=300)
    e.sleep_mode = "recover"
    r = e.run_watchdog()
    check("recovery during re-check skips restart", r.returncode == 0
          and e.restarts() == 0
          and not os.path.exists(e.cooldown_path()))
    e.cleanup()

    # 6. 接続フラグが false（mtime は新しい）でも再起動する
    e = Env()
    e.set_heartbeat(connected=False, age=0)
    r = e.run_watchdog()
    check("disconnected heartbeat restarts", r.returncode == 0
          and e.restarts() == 1)
    e.cleanup()

    # 7. 壊れたクールダウンファイル（数字でない）は 0 扱いで再起動を止めない
    e = Env()
    e.set_heartbeat(connected=True, age=300)
    with open(e.cooldown_path(), "w", encoding="utf-8") as f:
        f.write("not-a-number")
    r = e.run_watchdog()
    check("garbage cooldown treated as expired", r.returncode == 0
          and e.restarts() == 1)
    e.cleanup()

    # 8. listener.py が壊れている（py_compile が通らない）ときは、
    # heartbeat が古くても再起動しない（稼働中のプロセスを殺さない）
    e = Env()
    e.set_heartbeat(connected=True, age=300)
    e.set_listener("def broken(:\n")
    r = e.run_watchdog()
    check("broken listener.py skips stale-heartbeat restart",
          r.returncode == 0 and e.restarts() == 0
          and not os.path.exists(e.cooldown_path()))
    e.cleanup()

    # 9. ユニット再インストール後の再起動も同じガードが止める
    e = Env()
    e.set_listener("def broken(:\n")
    e.differ_unit()
    r = e.run_watchdog()
    check("broken listener.py skips post-reinstall restart",
          r.returncode == 0 and e.restarts() == 0)
    e.cleanup()

    # 10. listener.py が正常なら、再インストール後の再起動は走る
    e = Env()
    e.differ_unit()
    r = e.run_watchdog()
    check("valid listener.py restarts after reinstall",
          r.returncode == 0 and e.restarts() == 1)
    e.cleanup()

    print(f"{CHECKS[0]} passed, {CHECKS[1]} failed")
    sys.exit(0 if CHECKS[1] == 0 else 1)


if __name__ == "__main__":
    main()
