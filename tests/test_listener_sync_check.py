#!/usr/bin/env python3
"""Tests for slack-listener-sync-check.sh (Yanayana version of Neiro's
listener sync check, e79a783).

The Yanayana difference: the public copy is scrubbed (real IDs ->
placeholders), so the check compares the live file after applying
scrub.sed. Also the public slack-listener.service corresponds to the
live slack-listener.service, and live-only files that are not
known live-only (config.json etc.) are flagged UNPUBLISHED.

Plain stdlib script, no pytest (same style as the other suites).
Run:
  python3 tests/test_listener_sync_check.py
Exit 0 when every check passes; each check prints PASS/FAIL.

No real Slack calls: the check script is driven with env overrides
into temp dirs, and a fake slackcli records warning attempts.
"""
import json
import os
import stat
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CHECK = os.path.join(ROOT, "slack-listener-sync-check.sh")

CHECKS = [0, 0]  # [passed, failed]


def check(name, cond):
    if cond:
        CHECKS[0] += 1
        print(f"PASS {name}")
    else:
        CHECKS[1] += 1
        print(f"FAIL {name}")


# Minimal scrub fixture: the real channel ID and the live base path
# are rewritten to the placeholders the public copy carries.
SCRUB_RULES = (
    "s/C0EXAMPLE001/C0EXAMPLE001/g\n"
    "s|/path/to/slack-watcher|/path/to/slack-watcher|g\n"
    "s/slack-listener/slack-listener/g\n"
)


class Env:
    """Temp live/public dirs plus a fake slackcli and state file."""

    def __init__(self):
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
            f.write("#!/bin/sh\necho WARN >> \"$WARN_LOG\"\n")
        os.chmod(self.fake_cli, os.stat(self.fake_cli).st_mode | stat.S_IEXEC)

    def cleanup(self):
        self.tmp.cleanup()

    def write_both(self, name, content, live_content=None):
        for base in (self.pub, self.live):
            os.makedirs(os.path.dirname(os.path.join(base, name)),
                        exist_ok=True)
        with open(os.path.join(self.pub, name), "w", encoding="utf-8") as f:
            f.write(content)
        with open(os.path.join(self.live, name), "w", encoding="utf-8") as f:
            f.write(content if live_content is None else live_content)

    def write_live(self, name, content):
        os.makedirs(os.path.dirname(os.path.join(self.live, name)),
                    exist_ok=True)
        with open(os.path.join(self.live, name), "w", encoding="utf-8") as f:
            f.write(content)

    def run(self, extra_args=None, scrub=None):
        env = dict(os.environ,
                   LIVE_DIR=self.live, PUBLIC_DIR=self.pub,
                   SCRUB_SED=self.scrub if scrub is None else scrub,
                   SYNC_CHECK_STATE=self.state,
                   SLACKCLI=self.fake_cli, WARN_LOG=self.warn_log)
        args = ["bash", CHECK] + (extra_args or [])
        return subprocess.run(args, env=env, capture_output=True, text=True)

    def warns(self):
        if not os.path.exists(self.warn_log):
            return 0
        with open(self.warn_log, encoding="utf-8") as f:
            return len(f.readlines())


def main():
    # 1. 一致していれば exit 0 で ok を出す（警告なし・状態ファイルなし）
    e = Env()
    e.write_both("auto_join_channels.py", "print('ok')\n")
    e.write_both("watchdog.sh", "#!/bin/bash\necho ok\n")
    e.write_both("slack-auto-join.service", "[Unit]\n")
    r = e.run()
    check("matching files pass", r.returncode == 0
          and "listener sync: ok" in r.stdout and e.warns() == 0
          and not os.path.exists(e.state))
    e.cleanup()

    # 2. 内容が違えば DRIFT で exit 1（警告は1回だけ送られる）
    e = Env()
    e.write_both("auto_join_channels.py", "print('new')\n",
                 live_content="print('old')\n")
    r = e.run()
    check("drift detected", r.returncode == 1 and "DRIFT" in r.stdout
          and "auto_join_channels.py" in r.stdout and e.warns() == 1)
    e.cleanup()

    # 3. 稼働中のファイルが無ければ MISSING で exit 1
    e = Env()
    with open(os.path.join(e.pub, "backfill.py"), "w", encoding="utf-8") as f:
        f.write("print('x')\n")
    r = e.run()
    check("missing live file", r.returncode == 1 and "MISSING" in r.stdout
          and "backfill.py" in r.stdout)
    e.cleanup()

    # 4. scrub 済みの差分は無視される（実IDを含む稼働中と、伏せ字の公開用）
    e = Env()
    e.write_both("listener.py",
                 "CHANNEL = \"C0EXAMPLE001\"\nBASE = \"/path/to/slack-watcher\"\n",
                 live_content="CHANNEL = \"C0EXAMPLE001\"\n"
                 "BASE = \"/path/to/slack-watcher\"\n")
    r = e.run()
    check("scrubbed difference ignored", r.returncode == 0
          and "listener sync: ok" in r.stdout)
    e.cleanup()

    # 5. 構文が壊れていれば SYNTAX-ERROR で exit 1
    e = Env()
    e.write_both("listener.py", "def broken(:\n")
    r = e.run()
    check("syntax error detected", r.returncode == 1
          and "SYNTAX-ERROR" in r.stdout)
    e.cleanup()

    # 6. 稼働中だけの既知ファイル（config.json 等）は対象外
    e = Env()
    e.write_both("auto_join_channels.py", "print('ok')\n")
    e.write_live("config.json", "{}\n")
    e.write_live("oauth_helper_server.py", "print('local')\n")
    r = e.run()
    check("known live-only files ignored", r.returncode == 0)
    e.cleanup()

    # 7. サービス名の対応: 公開用 slack-listener.service は稼働中の
    #    slack-listener.service と比べる
    e = Env()
    with open(os.path.join(e.pub, "slack-listener.service"), "w",
              encoding="utf-8") as f:
        f.write("[Unit]\nDescription=slack-listener\n")
    with open(os.path.join(e.live, "slack-listener.service"), "w",
              encoding="utf-8") as f:
        f.write("[Unit]\nDescription=slack-listener\n")
    r = e.run()
    check("service rename mapping matches", r.returncode == 0)
    with open(os.path.join(e.live, "slack-listener.service"), "w",
              encoding="utf-8") as f:
        f.write("[Unit]\nDescription=something-else\n")
    r = e.run()
    check("service rename mapping drift detected", r.returncode == 1
          and "DRIFT" in r.stdout and "slack-listener.service" in r.stdout)
    e.cleanup()

    # 8. 稼働中にだけある公開対象ファイルは UNPUBLISHED で exit 1
    e = Env()
    e.write_both("auto_join_channels.py", "print('ok')\n")
    e.write_live("brand_new.py", "print('not synced yet')\n")
    r = e.run()
    check("unpublished live file flagged", r.returncode == 1
          and "UNPUBLISHED" in r.stdout and "brand_new.py" in r.stdout)
    e.cleanup()

    # 9. 警告の重複抑止: 同じ内容なら2回目は送らず、直れば状態が消える
    e = Env()
    e.write_both("auto_join_channels.py", "print('new')\n",
                 live_content="print('old')\n")
    r1 = e.run()
    r2 = e.run()
    check("warn deduplicated on repeat", r1.returncode == 1
          and r2.returncode == 1 and e.warns() == 1
          and "already warned" in r2.stdout)
    with open(os.path.join(e.live, "auto_join_channels.py"), "w",
              encoding="utf-8") as f:
        f.write("print('new')\n")
    r3 = e.run()
    check("recovery clears warn state", r3.returncode == 0
          and not os.path.exists(e.state) and e.warns() == 1)
    e.cleanup()

    # 10. --dry-run では送らず、送るはずの文面を出す
    e = Env()
    e.write_both("auto_join_channels.py", "print('new')\n",
                 live_content="print('old')\n")
    r = e.run(extra_args=["--dry-run"])
    check("dry run does not send", r.returncode == 1 and e.warns() == 0
          and "would warn" in r.stdout and not os.path.exists(e.state))
    e.cleanup()

    # 11. scrub.sed が無ければ比較できないと報告する
    e = Env()
    e.write_both("auto_join_channels.py", "print('ok')\n")
    r = e.run(scrub=os.path.join(e.tmp.name, "no-such.sed"))
    check("missing scrub file reported", r.returncode == 1
          and "SCRUB-MISSING" in r.stdout)
    e.cleanup()

    # 12. tests/ が一致していれば ok（トップレベルと同じ形で照合される）
    e = Env()
    e.write_both("tests/test_example.py", "print('ok')\n")
    r = e.run()
    check("matching tests pass", r.returncode == 0
          and "listener sync: ok" in r.stdout)
    e.cleanup()

    # 13. tests/ の1行のずれも DRIFT で検出する
    e = Env()
    e.write_both("tests/test_example.py", "print('new')\n",
                 live_content="print('old')\n")
    r = e.run()
    check("tests drift detected", r.returncode == 1 and "DRIFT" in r.stdout
          and "tests/test_example.py" in r.stdout and e.warns() == 1)
    e.cleanup()

    # 14. 稼働中にだけあるテストは UNPUBLISHED で拾う（.bak は対象外）
    e = Env()
    e.write_both("tests/test_example.py", "print('ok')\n")
    e.write_live("tests/test_brand_new.py", "print('not synced yet')\n")
    e.write_live("tests/test_example.py.bak", "print('backup')\n")
    r = e.run()
    check("unpublished test flagged, bak ignored", r.returncode == 1
          and "UNPUBLISHED" in r.stdout
          and "tests/test_brand_new.py" in r.stdout
          and "test_example.py.bak" not in r.stdout)
    e.cleanup()

    print(f"{CHECKS[0]} passed, {CHECKS[1]} failed")
    sys.exit(0 if CHECKS[1] == 0 else 1)


if __name__ == "__main__":
    main()
