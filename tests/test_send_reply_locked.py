#!/usr/bin/env python3
"""Tests for send_reply_locked.py: check -> send -> record serialized.

Yama's 2026-11-14 question in #slack-へようこそ (thread
1794583788.831849): the pre-send check and the send are separate
steps, so two concurrent workers can both check "not replied" and
both send. The wrapper holds one flock across replylog check,
reply_once.py (check + send + record), and the replylog record.

Plain stdlib script, no pytest (same style as the reply_once tests).
Run:
  python3 tests/test_send_reply_locked.py
Exit 0 when every check passes; each check prints PASS/FAIL.

The wrapper, reply_once.py and replylog.py are copied into a temp
dir with a fake bin/slackcli (thread read returns the parent only;
send sleeps briefly to open a real race window and logs each send),
so no real Slack call is made and the real events/ directory is
never touched.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

CHECKS = [0, 0]  # [passed, failed]


def check(name, cond, detail=""):
    CHECKS[0 if cond else 1] += 1
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not cond else ""))
    return cond


FAKE_SLACKCLI = '''#!/usr/bin/env python3
import json, sys, time, os
args = sys.argv[1:]
base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if args[:2] == ["conversations", "read"]:
    msgs = [{"ts": "1000.000000", "thread_ts": "1000.000000",
             "user": "U0OTHER", "text": "parent"}]
    print(json.dumps({"channel_id": "C0EXAMPLE001", "message_count": 1,
                      "messages": msgs}, ensure_ascii=False))
    sys.exit(0)
if args[:2] == ["messages", "send"]:
    time.sleep(0.5)
    ts = f"{time.time():.6f}"
    with open(os.path.join(base, "events", "fake_sends.log"), "a") as f:
        f.write(ts + "\\n")
    print(json.dumps({"ok": True, "ts": ts}))
    sys.exit(0)
print("fake slackcli: unknown command", args, file=sys.stderr)
sys.exit(1)
'''

CHANNEL = "C0EXAMPLE001"
THREAD_TS = "1000.000000"
TARGET_TS = "2000.000000"
MESSAGE = "test reply text"


def setup_tmp():
    tmp = tempfile.mkdtemp(prefix="sendlock-test-")
    for name in ("send_reply_locked.py", "reply_once.py", "replylog.py"):
        shutil.copy(os.path.join(ROOT, name), os.path.join(tmp, name))
    os.makedirs(os.path.join(tmp, "bin"))
    os.makedirs(os.path.join(tmp, "events"))
    fake = os.path.join(tmp, "bin", "slackcli")
    with open(fake, "w", encoding="utf-8") as f:
        f.write(FAKE_SLACKCLI)
    os.chmod(fake, 0o755)
    for name in ("replies.jsonl", "replied.jsonl"):
        open(os.path.join(tmp, "events", name), "w").close()
    return tmp


def run_wrapper(tmp, target=TARGET_TS, message=MESSAGE, extra=None):
    cmd = [sys.executable, os.path.join(tmp, "send_reply_locked.py"),
           "--channel", CHANNEL, "--thread-ts", THREAD_TS,
           "--target-ts", target, "--message", message]
    if extra:
        cmd += extra
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60)


def count_lines(path):
    try:
        with open(path, encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())
    except FileNotFoundError:
        return 0


def main():
    tmp = setup_tmp()
    try:
        sends_log = os.path.join(tmp, "events", "fake_sends.log")
        replies = os.path.join(tmp, "events", "replies.jsonl")
        replied = os.path.join(tmp, "events", "replied.jsonl")

        # 1. Two concurrent runs for the same target: exactly one send.
        results = []
        def _run():
            results.append(run_wrapper(tmp))
        t1 = threading.Thread(target=_run)
        t2 = threading.Thread(target=_run)
        t1.start(); t2.start(); t1.join(); t2.join()
        check("concurrent runs both exit 0",
              all(r.returncode == 0 for r in results),
              "; ".join(f"rc={r.returncode} out={r.stdout.strip()} err={r.stderr.strip()[:100]}" for r in results))
        check("concurrent runs send exactly once", count_lines(sends_log) == 1,
              f"sends={count_lines(sends_log)}")
        check("replies.jsonl has exactly one entry", count_lines(replies) == 1)
        check("replied.jsonl has exactly one entry", count_lines(replied) == 1)
        if count_lines(replies) == 1 and count_lines(replied) == 1:
            with open(replies, encoding="utf-8") as f:
                a = json.loads(f.readline())
            with open(replied, encoding="utf-8") as f:
                b = json.loads(f.readline())
            check("both ledgers record the same sent ts",
                  a.get("sent_ts") == b.get("reply_ts") and a.get("target_ts") == TARGET_TS)

        # 2. Re-run for the same target: already-replied, no new send.
        r = run_wrapper(tmp)
        check("re-run exits 0 without sending",
              r.returncode == 0 and "already-replied" in r.stdout
              and count_lines(sends_log) == 1,
              f"rc={r.returncode} out={r.stdout.strip()}")

        # 3. Dry-run for a fresh target: no send.
        r = run_wrapper(tmp, target="3000.000000", extra=["--dry-run"])
        check("dry-run exits 0 without sending",
              r.returncode == 0 and "would-send" in r.stdout
              and count_lines(sends_log) == 1,
              f"rc={r.returncode} out={r.stdout.strip()}")

        # 4. replylog already FOUND blocks the send before reply_once.
        with open(replies, "a", encoding="utf-8") as f:
            f.write(json.dumps({"channel": CHANNEL, "target_ts": "4000.000000",
                                "sent_ts": "4000.000001",
                                "thread_ts": THREAD_TS}) + "\n")
        r = run_wrapper(tmp, target="4000.000000")
        check("pre-seeded replylog entry blocks send",
              r.returncode == 0 and "already-replied (replylog)" in r.stdout
              and count_lines(sends_log) == 1,
              f"rc={r.returncode} out={r.stdout.strip()}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{CHECKS[0]} passed, {CHECKS[1]} failed")
    return 1 if CHECKS[1] else 0


if __name__ == "__main__":
    sys.exit(main())
