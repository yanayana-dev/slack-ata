#!/usr/bin/env python3
"""Tests for reply_once.py's duplicate-text (parroting) guard.

Incident 2026-11-14: a reply went out identical to Yama's post from
21 seconds earlier, because the draft path picked up the newest
post's text while a reply to an earlier message was being composed.
The pre-existing dedupe only watches for sending twice to the same
target; main() now refuses (exit 4) when the outgoing text is
identical to another author's existing post in the thread.

Plain stdlib script, no pytest (same style as test_msgstate.py). Run:
  python3 tests/test_reply_once_duplicate_text.py
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
YAMA_POST = {"user": "U0OTHER2", "ts": "333.3", "text": "shared finding text"}
OWN_POST = {"user": ro.YANAYANA_USER, "ts": "444.4", "text": "my earlier reply"}

SENT = []


def setup(tmp, name, messages):
    sub = os.path.join(tmp, name)
    os.makedirs(sub, exist_ok=True)
    ro.REPLIED = os.path.join(sub, "replied.jsonl")
    ro.AMBIGUOUS = os.path.join(sub, "ambiguous_posts.jsonl")
    ro.EVENTS = sub
    ro.already_replied = lambda *a, **k: None
    ro.thread_read_page_retry = lambda ch, ts, oldest=None: {
        "messages": messages, "has_more": False}
    SENT.clear()
    ro.send = lambda ch, ts, msg, event_id=None: SENT.append(msg) or "999.9"


def args(message):
    return ["--channel", "C0TEST", "--thread-ts", "111.1",
            "--target-ts", "222.2", "--message", message]


def main():
    with tempfile.TemporaryDirectory() as tmp:
        # 1. Identical to another author's post: refused, nothing sent.
        setup(tmp, "dup_other", [PARENT, YAMA_POST])
        rc = ro.main(args("shared finding text"))
        check("identical to other author's post -> exit 4", rc == 4, f"rc={rc}")
        check("identical to other author's post -> not sent", SENT == [])

        # 2. Same text with a trailing newline: still refused (strip).
        setup(tmp, "dup_ws", [PARENT, YAMA_POST])
        rc = ro.main(args("shared finding text\n"))
        check("whitespace-only difference -> exit 4", rc == 4, f"rc={rc}")
        check("whitespace-only difference -> not sent", SENT == [])

        # 3. Identical to the parent (question) itself: refused.
        setup(tmp, "dup_parent", [PARENT])
        rc = ro.main(args("question text"))
        check("identical to parent post -> exit 4", rc == 4, f"rc={rc}")

        # 4. Identical only to Yanayana's own post: guard does not
        # block (already_replied is stubbed away, so the send runs).
        setup(tmp, "own_only", [PARENT, OWN_POST])
        rc = ro.main(args("my earlier reply"))
        check("identical only to own post -> sent, exit 0", rc == 0 and SENT == ["my earlier reply"],
              f"rc={rc} sent={SENT}")

        # 5. Fresh text: sent normally.
        setup(tmp, "fresh", [PARENT, YAMA_POST, OWN_POST])
        rc = ro.main(args("a brand new reply"))
        check("fresh text -> sent, exit 0", rc == 0 and SENT == ["a brand new reply"],
              f"rc={rc} sent={SENT}")

        # 6. Unit level: duplicate_of_other returns the matching ts,
        # ignores own posts, and returns None when nothing matches.
        setup(tmp, "unit", [PARENT, YAMA_POST, OWN_POST])
        check("duplicate_of_other finds ts",
              ro.duplicate_of_other("C0TEST", "111.1", "shared finding text") == "333.3")
        check("duplicate_of_other ignores own posts",
              ro.duplicate_of_other("C0TEST", "111.1", "my earlier reply") is None)
        check("duplicate_of_other no match -> None",
              ro.duplicate_of_other("C0TEST", "111.1", "unrelated") is None)
        check("duplicate_of_other empty message -> None",
              ro.duplicate_of_other("C0TEST", "111.1", "  ") is None)

        # 7. Thread read fails during the guard: fail closed, exit 2,
        # nothing sent.
        setup(tmp, "read_fail", [PARENT, YAMA_POST])
        ro.thread_read_page_retry = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("read failed"))
        rc = ro.main(args("a brand new reply"))
        check("guard read failure -> exit 2, not sent", rc == 2 and SENT == [],
              f"rc={rc} sent={SENT}")

        # 7. Sent-text logging (2026-11-14, Yama's question): a normal
        # send records the exact text in replied.jsonl, so what was
        # sent is provable from local records, not only the live thread.
        setup(tmp, "text_logged", [PARENT, YAMA_POST, OWN_POST])
        rc = ro.main(args("a brand new reply"))
        rows = [json.loads(line) for line in
                open(os.path.join(tmp, "text_logged", "replied.jsonl"),
                     encoding="utf-8") if line.strip()]
        check("sent text recorded in replied.jsonl",
              rc == 0 and len(rows) == 1 and rows[0].get("text") == "a brand new reply"
              and rows[0].get("reply_ts") == "999.9", str(rows))

    print(f"\n{CHECKS[0]} passed, {CHECKS[1]} failed")
    return 1 if CHECKS[1] else 0


if __name__ == "__main__":
    sys.exit(main())
