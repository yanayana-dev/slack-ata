#!/usr/bin/env python3
"""Tests for slack-sync-check-runner.sh.

The runner normalizes the check script's exit code for the systemd
oneshot: a drift warning that was posted to Slack (or already posted
for the same drift) is exit 0, so the unit only goes "failed" when
the check could not run or could not warn. --dry-run posts nothing
and passes the check's exit code through.

The check script itself is driven with env overrides into temp dirs
(same Env shape as test_listener_sync_check.py), plus fake check
scripts for the abnormal-exit paths that the real script cannot
produce on demand.

Plain stdlib script, no pytest (same style as the other suites).
Run:
  python3 tests/test_sync_check_runner.py
Exit 0 when every check passes; each check prints PASS/FAIL.
"""
import json
import os
import stat
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RUNNER = os.path.join(ROOT, "slack-sync-check-runner.sh")
CHECK = os.path.join(ROOT, "slack-listener-sync-check.sh")

CHECKS = [0, 0]  # [passed, failed]


def check(name, cond):
    if cond:
        CHECKS[0] += 1
        print(f"PASS {name}")
    else:
        CHECKS[1] += 1
        print(f"FAIL {name}")


SCRUB_RULES = (
    "s/C0EXAMPLE001/C0EXAMPLE001/g\n"
    "s|/path/to/slack-watcher|/path/to/slack-watcher|g\n"
    "s/slack-listener/slack-listener/g\n"
)


class Env:
    """Temp live/public dirs plus a fake slackcli and state file."""

    def __init__(self, cli_exit=0):
        self.tmp = tempfile.TemporaryDirectory()
        self.live = os.path.join(self.tmp.name, "live")
        self.pub = os.path.join(self.tmp.name, "pub")
        os.makedirs(self.live)
        os.makedirs(self.pub)
        self.scrub = os.path.join(self.tmp.name, "scrub.sed")
        with open(self.scrub, "w", encoding="utf-8") as f:
            f.write(SCRUB_RULES)
        self.state = os.path.join(self.tmp.name, "state.json")
        self.warn_log = os.path.join(self.tmp.name, "warns.log")
        self.fake_cli = os.path.join(self.tmp.name, "slackcli")
        with open(self.fake_cli, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\necho WARN >> \"$WARN_LOG\"\n"
                    f"exit {cli_exit}\n")
        os.chmod(self.fake_cli, os.stat(self.fake_cli).st_mode | stat.S_IEXEC)

    def cleanup(self):
        self.tmp.cleanup()

    def write_both(self, name, content, live_content=None):
        with open(os.path.join(self.pub, name), "w", encoding="utf-8") as f:
            f.write(content)
        with open(os.path.join(self.live, name), "w", encoding="utf-8") as f:
            f.write(content if live_content is None else live_content)

    def run_runner(self, extra_args=None, check_script=None):
        env = dict(os.environ,
                   LIVE_DIR=self.live, PUBLIC_DIR=self.pub,
                   SCRUB_SED=self.scrub,
                   SYNC_CHECK_STATE=self.state,
                   SYNC_CHECK_SCRIPT=check_script or CHECK,
                   SLACKCLI=self.fake_cli, WARN_LOG=self.warn_log)
        args = ["bash", RUNNER] + (extra_args or [])
        return subprocess.run(args, env=env, capture_output=True, text=True)

    def warns(self):
        if not os.path.exists(self.warn_log):
            return 0
        with open(self.warn_log, encoding="utf-8") as f:
            return len(f.readlines())


def make_fake_check(path, body):
    with open(path, "w", encoding="utf-8") as f:
        f.write("#!/bin/sh\n" + body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)


def main():
    # 1. 一致していれば exit 0 で ok（警告なし）
    e = Env()
    e.write_both("auto_join_channels.py", "print('ok')\n")
    r = e.run_runner()
    check("matching files exit 0", r.returncode == 0
          and "listener sync: ok" in r.stdout and e.warns() == 0)
    e.cleanup()

    # 2. drift で警告が送れれば runner は exit 0（警告済み＝成功）
    e = Env()
    e.write_both("auto_join_channels.py", "print('new')\n",
                 live_content="print('old')\n")
    r = e.run_runner()
    check("drift warned exits 0", r.returncode == 0
          and "DRIFT" in r.stdout and "NG:" in r.stdout
          and e.warns() == 1)
    e.cleanup()

    # 3. 同じ drift の2回目（重複抑止で警告なし）も exit 0
    e = Env()
    e.write_both("auto_join_channels.py", "print('new')\n",
                 live_content="print('old')\n")
    r1 = e.run_runner()
    r2 = e.run_runner()
    check("already warned exits 0", r1.returncode == 0
          and r2.returncode == 0 and e.warns() == 1
          and "already warned" in r2.stdout)
    e.cleanup()

    # 4. 警告の送信に失敗すれば exit 1（硬い失敗）
    e = Env(cli_exit=1)
    e.write_both("auto_join_channels.py", "print('new')\n",
                 live_content="print('old')\n")
    r = e.run_runner()
    check("warn send failure exits 1", r.returncode == 1
          and "Slack warn send failed" in r.stderr)
    e.cleanup()

    # 5. --dry-run は何も送らず exit 1 をそのまま返す
    e = Env()
    e.write_both("auto_join_channels.py", "print('new')\n",
                 live_content="print('old')\n")
    r = e.run_runner(extra_args=["--dry-run"])
    check("dry run passes exit 1 through", r.returncode == 1
          and e.warns() == 0 and "would warn" in r.stdout)
    e.cleanup()

    # 6. チェック自体が異常終了（exit 2）すれば exit 1
    e = Env()
    fake = os.path.join(e.tmp.name, "fake_check.sh")
    make_fake_check(fake, "echo boom\nexit 2\n")
    r = e.run_runner(check_script=fake)
    check("abnormal check exit maps to 1", r.returncode == 1)
    e.cleanup()

    # 7. exit 1 だが NG 行なし（想定外の失敗）も exit 1
    e = Env()
    fake = os.path.join(e.tmp.name, "fake_check.sh")
    make_fake_check(fake, "echo something went wrong >&2\nexit 1\n")
    r = e.run_runner(check_script=fake)
    check("exit 1 without NG maps to 1", r.returncode == 1)
    e.cleanup()

    # 8. チェックが存在しなければ exit 1
    e = Env()
    r = e.run_runner(check_script=os.path.join(e.tmp.name, "no-such.sh"))
    check("missing check script maps to 1", r.returncode == 1)
    e.cleanup()

    print(f"{CHECKS[0]} passed, {CHECKS[1]} failed")
    sys.exit(0 if CHECKS[1] == 0 else 1)


if __name__ == "__main__":
    main()
