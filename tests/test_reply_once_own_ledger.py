#!/usr/bin/env python3
"""Tests for reply_once.py's local-ledger parroting guard.

Adopted 2026-11-14 from Yama's proposal in #slack-へようこそ
(thread 1794614134.265419): the 60s Slack-read window cannot catch a
draft that repeats Yanayana's own earlier summary when a bot-to-bot
turn takes longer than 60s, so parroting prevention now also reads
the local ledger events/replied.jsonl -- same thread, whitespace
normalized, exact match -- at no API cost. A match refuses the send
(exit 5); double-send prevention still reads Slack (unchanged).

Plain stdlib script, no pytest (same style as
test_reply_once_duplicate_text.py). Run:
  python3 tests/test_reply_once_own_ledger.py
Exit 0 when every check passes; each check prints PASS/FAIL.

Thread reads and sends are stubbed and the record files are
redirected to a temp dir, so no real Slack call is made.
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


PARENT = {"user": "U0OTHER1", "ts": "111.1", "text": "question text"}
THREAD_TS = "111.1"
OTHER_THREAD = "999.9"

SENT = []


def setup(tmp, name, ledger_rows):
    sub = os.path.join(tmp, name)
    os.makedirs(sub, exist_ok=True)
    ro.REPLIED = os.path.join(sub, "replied.jsonl")
    ro.AMBIGUOUS = os.path.join(sub, "ambiguous_posts.jsonl")
    ro.EVENTS = sub
    with open(ro.REPLIED, "w", encoding="utf-8") as f:
        for row in ledger_rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    ro.already_replied = lambda *a, **k: None
    ro.thread_read_page_retry = lambda ch, ts, oldest=None: {
        "messages": [PARENT], "has_more": False}
    SENT.clear()
    ro.send = lambda ch, ts, msg, event_id=None: SENT.append(msg) or "999.9"


def ledger_entry(thread_ts, text, reply_ts="555.5"):
    entry = {"thread_ts": thread_ts, "reply_ts": reply_ts,
             "channel": "C0TEST", "target_ts": "222.2",
             "in_reply_to_event": "222.2", "recovered": False,
             "at": "2026-11-14T07:00:00+00:00"}
    if text is not None:
        entry["text"] = text
    return entry


def args(message, target=True):
    a = ["--channel", "C0TEST", "--thread-ts", THREAD_TS, "--message", message]
    if target:
        a += ["--target-ts", "222.2"]
    return a


def main():
    with tempfile.TemporaryDirectory() as tmp:
        # 1. Identical to Yanayana's own earlier reply in the same
        # thread (ledger): refused, nothing sent.
        setup(tmp, "dup_own", [ledger_entry(THREAD_TS, "my earlier reply")])
        rc = ro.main(args("my earlier reply"))
        check("identical to own ledger reply -> exit 5", rc == 5, f"rc={rc}")
        check("identical to own ledger reply -> not sent", SENT == [])

        # 2. Same text re-wrapped across lines: normalized match
        # still refuses (whitespace/newlines collapse).
        setup(tmp, "dup_rewrap", [ledger_entry(
            THREAD_TS, "first line\nsecond line")])
        rc = ro.main(args("first line second line"))
        check("re-wrapped own reply -> exit 5", rc == 5, f"rc={rc}")
        check("re-wrapped own reply -> not sent", SENT == [])

        # 3. Same text, but the ledger entry is in another thread:
        # different conversation, so the send goes through.
        setup(tmp, "other_thread", [ledger_entry(OTHER_THREAD, "my earlier reply")])
        rc = ro.main(args("my earlier reply"))
        check("same text in another thread -> sent, exit 0",
              rc == 0 and SENT == ["my earlier reply"], f"rc={rc} sent={SENT}")

        # 4. Ledger record without text (written before 2026-11-14,
        # when texts started being stored): cannot match, ignored.
        setup(tmp, "no_text", [ledger_entry(THREAD_TS, None)])
        rc = ro.main(args("my earlier reply"))
        check("ledger entry without text -> sent, exit 0",
              rc == 0 and SENT == ["my earlier reply"], f"rc={rc} sent={SENT}")

        # 5. Fresh text: sent normally.
        setup(tmp, "fresh", [ledger_entry(THREAD_TS, "my earlier reply")])
        rc = ro.main(args("a brand new reply"))
        check("fresh text -> sent, exit 0",
              rc == 0 and SENT == ["a brand new reply"], f"rc={rc} sent={SENT}")

        # 6. Manual mode (no target event) still runs the ledger
        # check: the scope is the thread, not the target anchor.
        setup(tmp, "manual", [ledger_entry(THREAD_TS, "my earlier reply")])
        rc = ro.main(args("my earlier reply", target=False))
        check("manual mode ledger match -> exit 5", rc == 5, f"rc={rc}")
        check("manual mode ledger match -> not sent", SENT == [])

        # 7. Unit level: duplicate_of_own_ledger returns the
        # matching reply ts, ignores other threads and text-less
        # records, and returns None for empty messages.
        setup(tmp, "unit", [ledger_entry(THREAD_TS, "first line\nsecond line"),
                            ledger_entry(OTHER_THREAD, "other thread text"),
                            ledger_entry(THREAD_TS, None)])
        check("duplicate_of_own_ledger finds reply ts",
              ro.duplicate_of_own_ledger(THREAD_TS, "first line second line") == "555.5")
        check("duplicate_of_own_ledger other thread -> None",
              ro.duplicate_of_own_ledger(THREAD_TS, "other thread text") is None)
        check("duplicate_of_own_ledger no match -> None",
              ro.duplicate_of_own_ledger(THREAD_TS, "unrelated") is None)
        check("duplicate_of_own_ledger empty message -> None",
              ro.duplicate_of_own_ledger(THREAD_TS, "   ") is None)
        check("normalize_text collapses whitespace",
              ro.normalize_text("a\n  b\t c ") == "a b c")

        # 8. Ledger-first order (adopted 2026-11-14 from Yama's
        # confirmation that their ledger check is the mainline step
        # before the history scan): when the ledger matches, the
        # history read is not attempted at all -- even if the thread
        # read would fail, the send is still refused (exit 5) with
        # no API call.
        setup(tmp, "ledger_first", [ledger_entry(THREAD_TS, "my earlier reply")])
        reads = []

        def _must_not_read(*a, **k):
            reads.append(1)
            raise RuntimeError("history read must not run on a ledger hit")

        ro.thread_read_page_retry = _must_not_read
        rc = ro.main(args("my earlier reply"))
        check("ledger hit skips history read -> exit 5",
              rc == 5, f"rc={rc}")
        check("ledger hit skips history read -> no read attempted",
              reads == [])
        check("ledger hit skips history read -> not sent", SENT == [])

    print(f"\n{CHECKS[0]} passed, {CHECKS[1]} failed")
    return 1 if CHECKS[1] else 0


if __name__ == "__main__":
    sys.exit(main())
