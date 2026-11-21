#!/usr/bin/env python3
"""Tests for ledger_health.py and its wiring into the three loaders.

Added 2026-11-22 from Yama's proposal in #slack-へようこそ
(thread 1795265680.893129): when a ledger loader skips a corrupt
line, do not create a dedicated log file -- note the sighting
(once per JST date + ledger + line number) in
events/broken_ledger.jsonl so the 21:00 daily summary can carry
one line per ledger: "台帳に壊れた行あり（<file>・<n>件・
最初の行番号 <m>）". The stderr warning stays as the immediate
trace; the state file is only the summary's input.

Plain stdlib script, no pytest (same style as the other tests here).
Run: python3 tests/test_ledger_health.py
Exit 0 when every check passes; each check prints PASS/FAIL.

All state lives in a temp dir; the real events/ directory is
never touched (the state file sits next to the ledger path given,
and the loaders' ledger paths are monkeypatched to tmp files).
"""
import contextlib
import importlib.util
import io
import json
import os
import subprocess
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


def read_jsonl(path):
    if not os.path.exists(path):
        return []
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for l in f:
            if not l.strip():
                continue
            try:
                out.append(json.loads(l))
            except Exception:
                continue
    return out


GOOD = {"target_ts": "1795270082.688459", "thread_ts": "1795266091.223789"}


def main():
    with tempfile.TemporaryDirectory(prefix="ledger-health-") as tmp:
        events = os.path.join(tmp, "events")
        lh = load_module("ledger_health", "ledger_health.py")

        # note_corrupt_line: first sighting is recorded next to the ledger.
        ledger = os.path.join(events, "replies.jsonl")
        write_ledger(ledger, [json.dumps(GOOD)])
        check("note records first sighting",
              lh.note_corrupt_line(ledger, 2) is True)
        state = os.path.join(events, "broken_ledger.jsonl")
        recs = read_jsonl(state)
        check("state file has one record", len(recs) == 1, f"recs={recs!r}")
        check("record names ledger + lineno",
              recs and recs[0].get("ledger") == "replies.jsonl"
              and recs[0].get("lineno") == 2, f"recs={recs!r}")
        check("record carries a JST date",
              recs and len(recs[0].get("date", "")) == 10, f"recs={recs!r}")

        # Same line again: deduped, no second record.
        lh.note_corrupt_line(ledger, 2)
        lh.note_corrupt_line(ledger, 2)
        check("repeat sighting of same line is deduped",
              len(read_jsonl(state)) == 1, f"recs={read_jsonl(state)!r}")

        # A different line number is a separate sighting.
        lh.note_corrupt_line(ledger, 5)
        check("different lineno is a separate record",
              len(read_jsonl(state)) == 2, f"recs={read_jsonl(state)!r}")

        # today_summary: per-ledger count + first lineno, today only.
        other = os.path.join(events, "replied.jsonl")
        lh.note_corrupt_line(other, 9)
        summary = lh.today_summary(state)
        check("today_summary groups by ledger",
              summary == [{"ledger": "replied.jsonl", "count": 1, "first_lineno": 9},
                          {"ledger": "replies.jsonl", "count": 2, "first_lineno": 2}],
              f"summary={summary!r}")
        check("today_summary is empty for another date",
              lh.today_summary(state, date="2000-01-01") == [])

        # A corrupt line inside the state file itself is skipped.
        with open(state, "a", encoding="utf-8") as f:
            f.write("{broken\n")
        check("today_summary skips corrupt state lines",
              lh.today_summary(state) == [
                  {"ledger": "replied.jsonl", "count": 1, "first_lineno": 9},
                  {"ledger": "replies.jsonl", "count": 2, "first_lineno": 2}],
              f"summary={lh.today_summary(state)!r}")
        check("today_summary is empty when the file is missing",
              lh.today_summary(os.path.join(events, "nope.jsonl")) == [])

        # CLI prints Yama's one-liner per ledger, nothing when empty.
        env = dict(os.environ)
        env["SLACK_LISTENER_BASE"] = tmp
        r = subprocess.run([sys.executable, os.path.join(ROOT, "ledger_health.py")],
                           capture_output=True, text=True, env=env)
        check("CLI exit 0", r.returncode == 0, f"rc={r.returncode} err={r.stderr!r}")
        check("CLI prints one line per ledger",
              "台帳に壊れた行あり（replied.jsonl・1件・最初の行番号 9）" in r.stdout
              and "台帳に壊れた行あり（replies.jsonl・2件・最初の行番号 2）" in r.stdout,
              f"stdout={r.stdout!r}")
        r2 = subprocess.run([sys.executable, os.path.join(ROOT, "ledger_health.py")],
                            capture_output=True, text=True,
                            env={**env, "SLACK_LISTENER_BASE": os.path.join(tmp, "empty")})
        check("CLI prints nothing when there are no sightings",
              r2.returncode == 0 and r2.stdout == "", f"rc={r2.returncode} out={r2.stdout!r}")

        # Wiring: each loader notes the sighting when it skips a line.
        # replylog.load(path)
        rl = load_module("replylog", "replylog.py")
        rl_path = os.path.join(events, "replies2.jsonl")
        write_ledger(rl_path, [json.dumps(GOOD), "{not json", json.dumps({"target_ts": "x"})])
        with contextlib.redirect_stderr(io.StringIO()):
            entries = rl.load(rl_path)
        check("replylog.load keeps good lines",
              len(entries) == 2, f"entries={entries!r}")
        check("replylog.load notes the sighting",
              any(r.get("ledger") == "replies2.jsonl" and r.get("lineno") == 2
                  for r in read_jsonl(state)), f"recs={read_jsonl(state)!r}")

        # reply_once.load_record() with monkeypatched REPLIED.
        ro = load_module("reply_once", "reply_once.py")
        ro_path = os.path.join(events, "replied2.jsonl")
        write_ledger(ro_path, ["{broken", json.dumps(GOOD)])
        ro.REPLIED = ro_path
        with contextlib.redirect_stderr(io.StringIO()):
            entries = ro.load_record()
        check("reply_once.load_record keeps good lines",
              entries == [GOOD], f"entries={entries!r}")
        check("reply_once.load_record notes the sighting",
              any(r.get("ledger") == "replied2.jsonl" and r.get("lineno") == 1
                  for r in read_jsonl(state)), f"recs={read_jsonl(state)!r}")

        # channel_send_once.load_record() with monkeypatched POSTS.
        cso = load_module("channel_send_once", "channel_send_once.py")
        cso_path = os.path.join(events, "channel_posts2.jsonl")
        write_ledger(cso_path, [json.dumps({"text": "hi"}), "{broken"])
        cso.POSTS = cso_path
        with contextlib.redirect_stderr(io.StringIO()):
            entries = cso.load_record()
        check("channel_send_once.load_record keeps good lines",
              entries == [{"text": "hi"}], f"entries={entries!r}")
        check("channel_send_once.load_record notes the sighting",
              any(r.get("ledger") == "channel_posts2.jsonl" and r.get("lineno") == 2
                  for r in read_jsonl(state)), f"recs={read_jsonl(state)!r}")

        # Loading twice notes once (dedup across loads).
        with contextlib.redirect_stderr(io.StringIO()):
            cso.load_record()
        check("repeat loads do not duplicate the sighting",
              len([r for r in read_jsonl(state)
                   if r.get("ledger") == "channel_posts2.jsonl"]) == 1,
              f"recs={read_jsonl(state)!r}")

    print(f"\n{CHECKS[0]} passed, {CHECKS[1]} failed")
    return 0 if CHECKS[1] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
