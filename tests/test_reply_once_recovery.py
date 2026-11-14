#!/usr/bin/env python3
"""Tests for reply_once.py recovery confirmation (ambiguous_post_outcome).

Adopted 2026-11-12 from Yama's share in #slack-へようこそ: when the
send response is lost, retry the recovery confirmation up to 3 times;
if the reply is found in the thread treat it as delivered, otherwise
record ambiguous_post_outcome and do not repost blindly.

Plain stdlib script, no pytest (same style as test_msgstate.py). Run:
  python3 tests/test_reply_once_recovery.py
Exit 0 when every check passes; each check prints PASS/FAIL.

The module's send / thread-read functions are stubbed and the record
files are redirected to a temp dir, so no real Slack call is made and
the real events/ directory is never touched.
"""
import importlib.util
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SPEC = importlib.util.spec_from_file_location(
    "reply_once", os.path.join(ROOT, "reply_once.py"))
ro = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ro)

CHECKS = [0, 0]  # [passed, failed]


def check(name, cond, detail=""):
    CHECKS[0 if cond else 1] += 1
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not cond else ""))
    return cond


MSG = "test reply text"
ARGS = ["--channel", "C0TEST", "--thread-ts", "111.1",
        "--target-ts", "222.2", "--message", MSG]


def setup(tmp, name):
    sub = os.path.join(tmp, name)
    os.makedirs(sub, exist_ok=True)
    ro.REPLIED = os.path.join(sub, "replied.jsonl")
    ro.AMBIGUOUS = os.path.join(sub, "ambiguous_posts.jsonl")
    ro.EVENTS = sub
    ro.already_replied = lambda *a, **k: None
    # The duplicate-text guard (2026-11-14) scans the raw thread via
    # thread_all_messages; default it to an empty thread so these
    # recovery cases are not blocked by it. The guard itself is
    # covered by test_reply_once_duplicate_text.py. Its send-time
    # latest-page re-check (2026-11-14, Neiro's share) reads via
    # thread_latest_page, so stub that to an empty page too -- the
    # re-check itself is covered by
    # test_reply_once_latest_page.py.
    ro.thread_all_messages = lambda *a, **k: []
    ro.thread_latest_page = lambda *a, **k: {"messages": []}


def read_jsonl(path):
    try:
        with open(path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        return []


def test_send_lost_reply_found(tmp):
    """Send raises, recovery finds the text -> delivered, exit 0."""
    setup(tmp, "found")
    ro.send = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("timeout"))
    ro.thread_yanayana_replies = lambda *a, **k: [{"ts": "444.4", "text": MSG}]
    rc = ro.main(list(ARGS))
    recs = read_jsonl(ro.REPLIED)
    check("lost-send + reply found -> exit 0", rc == 0, f"rc={rc}")
    check("lost-send + reply found -> record written",
          len(recs) == 1 and recs[0].get("recovered") is True)
    check("lost-send + reply found -> reply_ts recorded (Yama 2026-11-12)",
          len(recs) == 1 and recs[0].get("reply_ts") == "444.4",
          f"recs={recs}")
    check("lost-send + reply found -> no ambiguous record",
          read_jsonl(ro.AMBIGUOUS) == [])


def test_send_lost_reply_absent(tmp):
    """Send raises, recovery confirms absent -> ambiguous, exit 3."""
    setup(tmp, "absent")
    ro.send = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("timeout"))
    ro.thread_yanayana_replies = lambda *a, **k: [{"ts": "445.5", "text": "something else"}]
    rc = ro.main(list(ARGS))
    amb = read_jsonl(ro.AMBIGUOUS)
    check("lost-send + reply absent -> exit 3", rc == 3, f"rc={rc}")
    check("lost-send + reply absent -> ambiguous record",
          len(amb) == 1 and amb[0].get("outcome") == "ambiguous_post_outcome")
    check("lost-send + reply absent -> no delivered record",
          read_jsonl(ro.REPLIED) == [])


def test_send_lost_reads_fail(tmp):
    """Send raises, recovery reads fail -> ambiguous, exit 3, no blind repost."""
    setup(tmp, "reads_fail")
    ro.send = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("timeout"))
    ro.thread_yanayana_replies = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("read failed"))
    ro.time = ro.time  # time.sleep is used between recovery attempts
    rc = ro.main(list(ARGS))
    amb = read_jsonl(ro.AMBIGUOUS)
    check("lost-send + reads fail -> exit 3", rc == 3, f"rc={rc}")
    check("lost-send + reads fail -> ambiguous record",
          len(amb) == 1 and "could not verify" in amb[0].get("reason", ""))
    check("lost-send + reads fail -> no delivered record",
          read_jsonl(ro.REPLIED) == [])


def test_send_no_ts_reply_found(tmp):
    """Send ok but no ts, recovery finds the text -> delivered, exit 0."""
    setup(tmp, "no_ts")
    ro.send = lambda *a, **k: None
    ro.thread_yanayana_replies = lambda *a, **k: [{"ts": "446.6", "text": MSG}]
    rc = ro.main(list(ARGS))
    recs = read_jsonl(ro.REPLIED)
    check("no-ts + reply found -> exit 0", rc == 0, f"rc={rc}")
    check("no-ts + reply found -> recovered record",
          len(recs) == 1 and recs[0].get("recovered") is True)
    check("no-ts + reply found -> reply_ts recorded",
          len(recs) == 1 and recs[0].get("reply_ts") == "446.6",
          f"recs={recs}")


def test_normal_send(tmp):
    """Normal send still records and exits 0 (regression)."""
    setup(tmp, "normal")
    ro.send = lambda *a, **k: "333.3"
    rc = ro.main(list(ARGS))
    recs = read_jsonl(ro.REPLIED)
    check("normal send -> exit 0", rc == 0, f"rc={rc}")
    check("normal send -> record with reply_ts",
          len(recs) == 1 and recs[0].get("reply_ts") == "333.3"
          and recs[0].get("recovered") is False)
    check("normal send -> no ambiguous record",
          read_jsonl(ro.AMBIGUOUS) == [])


def test_ambiguous_resolved_on_later_check(tmp):
    """Later pre-send check finds the reply -> ambiguous record corrected.

    Adopted 2026-11-12 from Yama's self-review proposal: when a
    response is lost and a later thread check proves the reply landed,
    the 'reply not posted' record must be corrected, not left standing.
    """
    setup(tmp, "resolve")
    ro.already_replied = lambda *a, **k: "thread"
    with open(ro.AMBIGUOUS, "w", encoding="utf-8") as f:
        f.write(json.dumps({
            "outcome": "ambiguous_post_outcome", "thread_ts": "111.1",
            "target_ts": "222.2", "channel": "C0TEST",
            "reason": "send-response-lost; recovery check confirmed reply absent",
            "at": "2026-11-12T00:00:00+00:00",
        }, ensure_ascii=False) + "\n")
    rc = ro.main(list(ARGS) + ["--check-only"])
    amb = read_jsonl(ro.AMBIGUOUS)
    check("later check + ambiguous exists -> exit 0", rc == 0, f"rc={rc}")
    check("later check -> resolution entry appended",
          len(amb) == 2 and amb[1].get("outcome") == "resolved_by_thread_check"
          and amb[1].get("target_ts") == "222.2", f"amb={amb}")
    check("later check -> original ambiguous entry kept as history",
          amb[0].get("outcome") == "ambiguous_post_outcome")
    # Second run must not append a duplicate resolution.
    rc2 = ro.main(list(ARGS) + ["--check-only"])
    amb2 = read_jsonl(ro.AMBIGUOUS)
    check("second check -> no duplicate resolution",
          rc2 == 0 and len(amb2) == 2, f"amb2={amb2}")


def test_no_ambiguous_nothing_resolved(tmp):
    """Pre-send check finds a reply but no ambiguous entry -> no write."""
    setup(tmp, "noamb")
    ro.already_replied = lambda *a, **k: "thread"
    rc = ro.main(list(ARGS) + ["--check-only"])
    check("no ambiguous entry -> exit 0", rc == 0, f"rc={rc}")
    check("no ambiguous entry -> no file written",
          not os.path.exists(ro.AMBIGUOUS))


def main():
    with tempfile.TemporaryDirectory() as tmp:
        test_send_lost_reply_found(tmp)
        test_send_lost_reply_absent(tmp)
        test_send_lost_reads_fail(tmp)
        test_send_no_ts_reply_found(tmp)
        test_normal_send(tmp)
        test_ambiguous_resolved_on_later_check(tmp)
        test_no_ambiguous_nothing_resolved(tmp)
    print(f"\n{CHECKS[0]} passed, {CHECKS[1]} failed")
    return 0 if CHECKS[1] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
