#!/usr/bin/env python3
"""Tests that a skipped corrupt ledger line is logged, not silent.

Added 2026-11-21 from Yama's question in #slack-へようこそ
(thread 1795265680.893129): reply_once.load_record() skips a corrupt
line per line so one bad row does not discard the whole ledger --
but the skip vanished silently, so ledger loss went unnoticed.
The same silent shape was in replylog.load() and
channel_send_once.load_record(). All three now print a stderr
warning (line number + truncated content) for each skipped line,
while the good lines still load.

Plain stdlib script, no pytest (same style as the other tests here).
Run: python3 tests/test_ledger_corrupt_line_logged.py
Exit 0 when every check passes; each check prints PASS/FAIL.

All state lives in a temp dir; the real events/ directory is
never touched (module-level ledger paths are monkeypatched).
"""
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

CHECKS = [0, 0]  # [passed, failed]


def check(name, cond, detail=""):
    CHECKS[0 if cond else 1] += 1
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not cond else ""))
    return cond


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(ROOT, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def write_ledger(path, lines):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for line in lines:
            f.write(line + "\n")


GOOD = {"target_ts": "1795270082.688459", "thread_ts": "1795266091.223789"}


def main():
    with tempfile.TemporaryDirectory(prefix="ledger-corrupt-logged-") as tmp:
        # replylog.load(path): corrupt line on line 2, good lines kept.
        replylog = load_module("replylog", "replylog.py")
        path = os.path.join(tmp, "events", "replies.jsonl")
        write_ledger(path, [json.dumps(GOOD), "{not json", json.dumps({"target_ts": "x"})])
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            entries = replylog.load(path)
        check("replylog.load keeps good lines",
              len(entries) == 2 and entries[0] == GOOD,
              f"entries={entries!r}")
        check("replylog.load warns on stderr",
              "skipping corrupt line 2" in err.getvalue(),
              f"stderr={err.getvalue()!r}")
        check("replylog.load warning names the file",
              "replies.jsonl" in err.getvalue(),
              f"stderr={err.getvalue()!r}")

        # reply_once.load_record(): same shape, path monkeypatched.
        ro = load_module("reply_once", "reply_once.py")
        ro_path = os.path.join(tmp, "events", "replied.jsonl")
        write_ledger(ro_path, ["{broken", json.dumps(GOOD)])
        ro.REPLIED = ro_path
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            entries = ro.load_record()
        check("reply_once.load_record keeps good lines",
              entries == [GOOD], f"entries={entries!r}")
        check("reply_once.load_record warns with line number",
              "skipping corrupt line 1" in err.getvalue(),
              f"stderr={err.getvalue()!r}")

        # channel_send_once.load_record(): same shape.
        cso = load_module("channel_send_once", "channel_send_once.py")
        cso_path = os.path.join(tmp, "events", "channel_posts.jsonl")
        write_ledger(cso_path, [json.dumps({"text": "hello"}), "garbage{"])
        cso.POSTS = cso_path
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            entries = cso.load_record()
        check("channel_send_once.load_record keeps good lines",
              entries == [{"text": "hello"}], f"entries={entries!r}")
        check("channel_send_once.load_record warns with line number",
              "skipping corrupt line 2" in err.getvalue(),
              f"stderr={err.getvalue()!r}")

        # Clean ledger: no warning at all.
        write_ledger(path, [json.dumps(GOOD)])
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            entries = replylog.load(path)
        check("clean ledger -> no warning",
              err.getvalue() == "" and entries == [GOOD],
              f"stderr={err.getvalue()!r}")

    print(f"\n{CHECKS[0]} passed, {CHECKS[1]} failed")
    return 1 if CHECKS[1] else 0


if __name__ == "__main__":
    sys.exit(main())
