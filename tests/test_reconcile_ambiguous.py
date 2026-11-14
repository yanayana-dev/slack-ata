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


def write_replied(tmp, entries):
    os.makedirs(os.path.join(tmp, "events"), exist_ok=True)
    with open(os.path.join(tmp, "events", "replied.jsonl"), "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


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
        write_ambiguous(tmp, [amb(target="1000.000001", message="hello")])
        s = ra.reconcile(thread_reader=lambda c, t: [{"ts": "1000.000002", "text": "hello"}])
        check("found reply is resolved", s["resolved"] == 1 and s["checked"] == 1, str(s))
        replied = read_jsonl(os.path.join(tmp, "events", "replied.jsonl"))
        check("recovered reply recorded with ts",
              len(replied) == 1 and replied[0]["reply_ts"] == "1000.000002"
              and replied[0]["recovered"] is True and replied[0]["target_ts"] == "1000.000001",
              str(replied))
        entries = read_jsonl(os.path.join(tmp, "events", "ambiguous_posts.jsonl"))
        check("resolved_by_thread_check appended",
              any(e.get("outcome") == "resolved_by_thread_check" and e.get("target_ts") == "1000.000001"
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

        # 3. Entry without message text -> closed as closed_no_text
        # on first sight (decided with Yama 2026-11-14), never guessed.
        write_ambiguous(tmp, [amb(target="T3", message=None)])
        s = ra.reconcile(thread_reader=lambda c, t: (_ for _ in ()).throw(AssertionError("must not read")))
        check("entry without message text is closed", s["closed_no_text"] == 1 and s["skipped_no_text"] == 0, str(s))
        entries = read_jsonl(os.path.join(tmp, "events", "ambiguous_posts.jsonl"))
        check("closed_no_text record appended",
              any(e.get("outcome") == "closed_no_text" and e.get("target_ts") == "T3"
                  for e in entries), str(entries))
        # Re-run: closed target is not re-checked or re-closed.
        s = ra.reconcile(thread_reader=lambda c, t: (_ for _ in ()).throw(AssertionError("must not read")))
        check("closed target is not re-processed",
              s["closed_no_text"] == 0 and s["checked"] == 0 and s["skipped_no_text"] == 0, str(s))
        # A later real proof can still resolve the same target.
        check("resolve still works after closing",
              ra.ro.resolve_ambiguous("C0TEST", "111.1", "T3") is True)
        entries = read_jsonl(os.path.join(tmp, "events", "ambiguous_posts.jsonl"))
        check("resolved_by_thread_check appended after close",
              any(e.get("outcome") == "resolved_by_thread_check" and e.get("target_ts") == "T3"
                  for e in entries), str(entries))

        # 4. Thread read failure -> skipped, left unresolved.
        write_ambiguous(tmp, [amb(target="T4", message="hello")])
        def boom(c, t):
            raise RuntimeError("slack down")
        s = ra.reconcile(thread_reader=boom)
        check("read failure leaves entry unresolved", s["skipped_read_failed"] == 1, str(s))

        # 5. Gating: main() runs once, then gates until interval passes.
        # Stub the live reader: main() uses the module default.
        ra.ro.thread_yanayana_replies = lambda c, t: [{"ts": "1000.000006", "text": "hello"}]
        write_ambiguous(tmp, [amb(target="1000.000005", message="hello")])
        rc = ra.main(["--interval-secs", "3600"])
        check("first main run succeeds", rc == 0)
        entries = read_jsonl(os.path.join(tmp, "events", "ambiguous_posts.jsonl"))
        check("gated run still resolved the entry first time",
              any(e.get("outcome") == "resolved_by_thread_check" and e.get("target_ts") == "1000.000005"
                  for e in entries), str(entries))
        rc = ra.main(["--interval-secs", "3600"])
        check("second main run within interval is gated", rc == 0)
        rc = ra.main(["--force"])
        check("forced main run bypasses the gate", rc == 0)

        # 6. No ambiguous file at all -> clean no-op, still records last run.
        os.remove(os.path.join(tmp, "events", "ambiguous_posts.jsonl"))
        s = ra.reconcile(thread_reader=lambda c, t: (_ for _ in ()).throw(AssertionError("must not read")))
        check("missing file is a clean no-op", s["checked"] == 0 and s["resolved"] == 0, str(s))

        # 7. Same-text reply posted BEFORE the target must not resolve it.
        # (Time-order check adopted 2026-11-14 from Yama's audit share,
        # their指摘2: msg.ts > event.ts.)
        write_ambiguous(tmp, [amb(target="1000.000010", message="hello")])
        s = ra.reconcile(thread_reader=lambda c, t: [{"ts": "1000.000009", "text": "hello"}])
        check("reply before target does not resolve", s["still_unresolved"] == 1 and s["resolved"] == 0, str(s))
        check("no record written for pre-target reply",
              len(read_jsonl(os.path.join(tmp, "events", "replied.jsonl"))) == 2)

        # 8. Two entries with identical text share one reply: only the
        # first resolves; the same reply ts is never reused in a sweep.
        # (Yama's assigned_reply_ts, adopted 2026-11-14.)
        write_ambiguous(tmp, [amb(target="1000.000020", message="hello"),
                              amb(target="1000.000021", message="hello")])
        s = ra.reconcile(thread_reader=lambda c, t: [{"ts": "1000.000022", "text": "hello"}])
        check("same reply ts resolves only one entry",
              s["resolved"] == 1 and s["still_unresolved"] == 1, str(s))
        check("no duplicate record for the reused ts",
              len(read_jsonl(os.path.join(tmp, "events", "replied.jsonl"))) == 3)

        # 9. A reply ts consumed in an EARLIER sweep must not resolve a
        # different entry in this sweep (Neiro's share 2026-11-14:
        # already-resolved found_ts counts as used). The reply that
        # resolved target 1000.000020 lives in replied.jsonl; a second
        # same-text entry for 1000.000021 must stay unresolved even
        # though the same reply text is in the thread.
        write_ambiguous(tmp, [amb(target="1000.000021", message="hello")])
        write_replied(tmp, [{"channel": "C0TEST", "thread_ts": "111.1",
                             "target_ts": "1000.000020", "reply_ts": "1000.000022",
                             "recovered": True,
                             "at": "2026-11-12T00:00:00+00:00"}])
        s = ra.reconcile(thread_reader=lambda c, t: [{"ts": "1000.000022", "text": "hello"}])
        check("reply ts from earlier sweep is not reused",
              s["resolved"] == 0 and s["still_unresolved"] == 1, str(s))
        check("no new record for the reused ts",
              len(read_jsonl(os.path.join(tmp, "events", "replied.jsonl"))) == 1)

    print(f"\n{CHECKS[0]} passed, {CHECKS[1]} failed")
    return 1 if CHECKS[1] else 0


if __name__ == "__main__":
    sys.exit(main())
