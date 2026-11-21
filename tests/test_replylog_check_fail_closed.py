#!/usr/bin/env python3
"""Tests for replylog.py check being fail-closed.

Added 2026-11-21 from Neiro's 中-1 (send-once fail-open, shared in
#slack-へようこそ): their duplicate-check Python piece exits 1
(= "no duplicate") when it raises, and the send proceeds. Our
replylog.py check had the same shape -- load() raising (e.g. the
ledger being a directory, or unreadable) ended in a traceback with
exit 1 and no FOUND/NOT_FOUND line, which a caller that sends
unless FOUND would read as "not replied". The check must instead
exit 2 (cannot-verify) and never print NOT_FOUND on error.

Plain stdlib script, no pytest (same style as the other tests here).
Run: python3 tests/test_replylog_check_fail_closed.py
Exit 0 when every check passes; each check prints PASS/FAIL.

All state lives in a temp dir (SLACK_LISTENER_BASE), so the real
events/ directory is never touched.
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
REPLYLOG = os.path.join(ROOT, "replylog.py")

CHECKS = [0, 0]  # [passed, failed]


def check(name, cond, detail=""):
    CHECKS[0 if cond else 1] += 1
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not cond else ""))
    return cond


def run_check(base, target_ts):
    env = dict(os.environ, SLACK_LISTENER_BASE=base)
    return subprocess.run(
        [sys.executable, REPLYLOG, "check", "--target-ts", target_ts],
        capture_output=True, text=True, env=env, timeout=30)


def main():
    with tempfile.TemporaryDirectory(prefix="replylog-failclosed-") as tmp:
        events = os.path.join(tmp, "events")
        os.makedirs(events)

        # 1. Missing ledger: NOT_FOUND, exit 0 (nothing recorded yet).
        r = run_check(tmp, "1795270082.688459")
        check("missing ledger -> NOT_FOUND exit 0",
              r.returncode == 0 and r.stdout.strip() == "NOT_FOUND",
              f"rc={r.returncode} out={r.stdout.strip()!r}")

        # 2. Recorded entry: FOUND, exit 0.
        env = dict(os.environ, SLACK_LISTENER_BASE=tmp)
        rec = subprocess.run(
            [sys.executable, REPLYLOG, "record", "--channel", "C0EXAMPLE001",
             "--target-ts", "1795270082.688459", "--sent-ts", "1795270100.000000",
             "--thread-ts", "1795266091.223789"],
            capture_output=True, text=True, env=env, timeout=30)
        check("record exit 0", rec.returncode == 0, rec.stderr[:200])
        r = run_check(tmp, "1795270082.688459")
        check("recorded entry -> FOUND exit 0",
              r.returncode == 0 and r.stdout.strip().startswith("FOUND"),
              f"rc={r.returncode} out={r.stdout.strip()[:80]!r}")

        # 3. Ledger unreadable (a directory in its place): exit 2,
        # no NOT_FOUND -- the fail-open hole this test guards.
        os.remove(os.path.join(events, "replies.jsonl"))
        os.mkdir(os.path.join(events, "replies.jsonl"))
        r = run_check(tmp, "1795270082.688459")
        check("unreadable ledger -> exit 2",
              r.returncode == 2, f"rc={r.returncode}")
        check("unreadable ledger -> no NOT_FOUND",
              "NOT_FOUND" not in r.stdout, f"out={r.stdout.strip()!r}")
        check("unreadable ledger -> check-failed on stderr",
              "check-failed" in r.stderr, f"err={r.stderr.strip()[:120]!r}")
        check("unreadable ledger -> no traceback",
              "Traceback" not in r.stderr, f"err={r.stderr.strip()[:120]!r}")

        # 4. Ledger with a malformed line still answers from the
        # good lines (per-line errors are skipped, as before).
        os.rmdir(os.path.join(events, "replies.jsonl"))
        with open(os.path.join(events, "replies.jsonl"), "w", encoding="utf-8") as f:
            f.write("{not json\n")
            f.write(json.dumps({"target_ts": "1795270082.688459"}) + "\n")
        r = run_check(tmp, "1795270082.688459")
        check("malformed line tolerated -> FOUND exit 0",
              r.returncode == 0 and r.stdout.strip().startswith("FOUND"),
              f"rc={r.returncode} out={r.stdout.strip()[:80]!r}")

    print(f"\n{CHECKS[0]} passed, {CHECKS[1]} failed")
    return 1 if CHECKS[1] else 0


if __name__ == "__main__":
    sys.exit(main())
