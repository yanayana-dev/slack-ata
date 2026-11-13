#!/usr/bin/env python3
"""Tests for reconcile_ambiguous.py (periodic ambiguous-send sweep).

Adopted 2026-11-12 from Yama's implementation share in #slack-へようこそ
(their "遅れて着地した返信の定期照合"): unresolved ambiguous_post_outcome
entries are re-checked against the live thread; a found reply is
recorded (recovered) and the entry resolved, never reposted.

Plain stdlib script, no pytest (same style as test_reply_once_recovery.py).
Run:  python3 tests/test_reconcile_ambiguous.py
Exit 0 when every check passes; each check prints PASS/FAIL.

The thread reader is stubbed and all record files live in a temp dir,
so no real Slack call is made and the real events/ directory is never touched.
"""
import importlib.util
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SPEC = importlib.util.spec_from_file_location(
    "reconcile_ambiguous", os.path.join(ROOT, "reconcile_ambiguous.py"))
ra = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ra)

CHECKS = [0, 0]  # [passed, failed]


def check(name, cond, detail=""):
    CHECKS[0 if cond else 1] += 1
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not cond else ""))
    return cond


def write_ambiguous(tmp, entries):
    os.makedirs(os.path.join(tmp, "events"), exist_ok=True)
    with open(os.path.join(tmp, "events", "ambiguous_posts.jsonl"), "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def read_jsonl(path):
    try:
        with open(path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
    except Exception:
        return []


def amb(channel="C0TEST", thread="111.1", target="222.2", message="hello", outcome="ambiguous_post_outcome"):
    e = {"outcome": outcome, "channel": channel, "thread_ts": thread,
         "target_ts": target, "reason": "send-response-lost", "at": "2026-11-12T00:00:00+00:00"}
    if message is not None:
        e["message"] = message
    return e


def setup(tmp):
    ra.BASE = tmp
    ra.EVENTS = os.path.join(tmp, "events")
    ra.STATE_FILE = os.path.join(tmp, "events", "reconcile_ambiguous.state")
    ra.ro.BASE = tmp
    ra.ro.EVENTS = os.path.join(tmp, "events")
    ra.ro.REPLIED = os.path.join(tmp, "events", "replied.jsonl")
    ra.ro.AMBIGUOUS = os.path.join(tmp, "events", "ambiguous_posts.jsonl")


def main():
    with tempfile.TemporaryDirectory() as tmp:
        setup(tmp)
        # 1. Found reply -> recorded (recovered) and resolved.
        write_ambiguous(tmp, [amb(target="T1", message="hello")])
        s = ra.reconcile(thread_reader=lambda c, t: [{"ts": "R1", "text": "hello"}])
        check("found reply is resolved", s["resolved"] == 1 and s["checked"] == 1, str(s))
        replied = read_jsonl(os.path.join(tmp, "events", "replied.jsonl"))
        check("recovered reply recorded with ts",
              len(replied) == 1 and replied[0]["reply_ts"] == "R1"
              and replied[0]["recovered"] is True and replied[0]["target_ts"] == "T1",
              str(replied))
        entries = read_jsonl(os.path.join(tmp, "events", "ambiguous_posts.jsonl"))
        check("resolved_by_thread_check appended",
              any(e.get("outcome") == "resolved_by_thread_check" and e.get("target_ts") == "T1"
                  for e in entries), str(entries))
        # Re-run: already-resolved target is not re-checked.
        s2 = ra.reconcile(thread_reader=lambda c, t: (_ for _ in ()).throw(AssertionError("must not read")))
        check("already-resolved target is not re-checked", s2["checked"] == 0, str(s2))

        # 2. Reply absent -> stays unresolved, nothing recorded.
        write_ambiguous(tmp, [amb(target="T2", message="gone")])
        s = ra.reconcile(thread_reader=lambda c, t: [{"ts": "R9", "text": "other"}])
        check("absent reply stays unresolved", s["still_unresolved"] == 1 and s["resolved"] == 0, str(s))
        check("no record written for absent reply",
              len(read_jsonl(os.path.join(tmp, "events", "replied.jsonl"))) == 1)

        # 3. Entry without message text -> skipped, never guessed.
        write_ambiguous(tmp, [amb(target="T3", message=None)])
        s = ra.reconcile(thread_reader=lambda c, t: (_ for _ in ()).throw(AssertionError("must not read")))
        check("entry without message text is skipped", s["skipped_no_text"] == 1, str(s))

        # 4. Thread read failure -> skipped, left unresolved.
        write_ambiguous(tmp, [amb(target="T4", message="hello")])
        def boom(c, t):
            raise RuntimeError("slack down")
        s = ra.reconcile(thread_reader=boom)
        check("read failure leaves entry unresolved", s["skipped_read_failed"] == 1, str(s))

        # 5. Gating: main() runs once, then gates until interval passes.
        # Stub the live reader: main() uses the module default.
        ra.ro.thread_yanayana_replies = lambda c, t: [{"ts": "R5", "text": "hello"}]
        write_ambiguous(tmp, [amb(target="T5", message="hello")])
        rc = ra.main(["--interval-secs", "3600"])
        check("first main run succeeds", rc == 0)
        entries = read_jsonl(os.path.join(tmp, "events", "ambiguous_posts.jsonl"))
        check("gated run still resolved the entry first time",
              any(e.get("outcome") == "resolved_by_thread_check" and e.get("target_ts") == "T5"
                  for e in entries), str(entries))
        rc = ra.main(["--interval-secs", "3600"])
        check("second main run within interval is gated", rc == 0)
        rc = ra.main(["--force"])
        check("forced main run bypasses the gate", rc == 0)

        # 6. No ambiguous file at all -> clean no-op, still records last run.
        os.remove(os.path.join(tmp, "events", "ambiguous_posts.jsonl"))
        s = ra.reconcile(thread_reader=lambda c, t: (_ for _ in ()).throw(AssertionError("must not read")))
        check("missing file is a clean no-op", s["checked"] == 0 and s["resolved"] == 0, str(s))

    print(f"\n{CHECKS[0]} passed, {CHECKS[1]} failed")
    return 1 if CHECKS[1] else 0


if __name__ == "__main__":
    sys.exit(main())
