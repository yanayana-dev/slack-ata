#!/usr/bin/env python3
"""Tests for reply_once.py's send-time latest-page re-check.

Adopted 2026-11-14 from Neiro's share in #slack-へようこそ (thread
1794583788.831849): Neiro's parroting guard re-checks the thread's
latest 100 messages right before sending, which catches an identical
post older than the 60s window that the windowed read cannot see
(e.g. the parent text in an old thread, or the same question asked
again further up). duplicate_of_other in target mode now does the
windowed read first, then the latest-page re-check; the remaining
hole is the same as Neiro's (an identical post pushed beyond the
latest 100 is still missed).

Plain stdlib script, no pytest (same style as
test_reply_once_duplicate_text.py). Run:
  python3 tests/test_reply_once_latest_page.py
Exit 0 when every check passes; each check prints PASS/FAIL.

Thread reads and sends are stubbed and the record files are
redirected to a temp dir, so no real Slack call is made. The stub
distinguishes the two reads by the `oldest` argument: the windowed
read passes oldest=target-60s, the latest-page read passes none.
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
# An old post from another author, older than the 60s window
# (target 222.2 -> window starts 162.2), still in the latest page.
OLD_POST = {"user": "U0OTHER2", "ts": "150.0", "text": "old repeated question"}
OWN_OLD = {"user": ro.YANAYANA_USER, "ts": "155.0", "text": "my old reply"}
WINDOW_POST = {"user": "U0OTHER2", "ts": "200.0", "text": "recent post"}

SENT = []


def setup(tmp, name, window_messages, latest_messages, latest_fails=False):
    sub = os.path.join(tmp, name)
    os.makedirs(sub, exist_ok=True)
    ro.REPLIED = os.path.join(sub, "replied.jsonl")
    ro.AMBIGUOUS = os.path.join(sub, "ambiguous_posts.jsonl")
    ro.EVENTS = sub
    ro.already_replied = lambda *a, **k: None

    def fake_read(ch, ts, oldest=None):
        if latest_fails and oldest is None:
            raise RuntimeError("latest page read failed")
        if oldest is None:
            return {"messages": latest_messages, "has_more": False}
        return {"messages": window_messages, "has_more": False}

    ro.thread_read_page_retry = fake_read
    SENT.clear()
    ro.send = lambda ch, ts, msg, event_id=None, request_id=None: SENT.append(msg) or "999.9"


def args(message):
    return ["--channel", "C0TEST", "--thread-ts", "111.1",
            "--target-ts", "222.2", "--message", message]


def main():
    with tempfile.TemporaryDirectory() as tmp:
        # 1. Identical only to an old post outside the window, visible
        # in the latest page: refused (exit 4), nothing sent.
        setup(tmp, "old_dup", [PARENT, WINDOW_POST],
              [PARENT, OLD_POST, WINDOW_POST])
        rc = ro.main(args("old repeated question"))
        check("old identical post in latest page -> exit 4", rc == 4, f"rc={rc}")
        check("old identical post in latest page -> not sent", SENT == [])

        # 2. Fresh text, no match in either read: sent normally.
        setup(tmp, "fresh", [PARENT, WINDOW_POST],
              [PARENT, OLD_POST, WINDOW_POST])
        rc = ro.main(args("a brand new reply"))
        check("fresh text -> sent, exit 0", rc == 0 and SENT == ["a brand new reply"],
              f"rc={rc} sent={SENT}")

        # 3. Identical to a post inside the window: still refused by
        # the windowed check, before the latest-page read matters.
        setup(tmp, "window_dup", [PARENT, WINDOW_POST],
              [PARENT, OLD_POST, WINDOW_POST])
        rc = ro.main(args("recent post"))
        check("identical in window -> exit 4", rc == 4, f"rc={rc}")
        check("identical in window -> not sent", SENT == [])

        # 4. Identical only to Yanayana's own old post in the latest
        # page: the guard ignores own posts (the own-ledger guard
        # covers repeats of our own replies, and the record here is
        # empty so nothing blocks) -> sent.
        setup(tmp, "own_old", [PARENT, WINDOW_POST],
              [PARENT, OWN_OLD, WINDOW_POST])
        rc = ro.main(args("my old reply"))
        check("identical only to own old post -> sent, exit 0",
              rc == 0 and SENT == ["my old reply"], f"rc={rc} sent={SENT}")

        # 5. Latest-page read fails: fail closed (exit 2), nothing
        # sent -- the windowed check alone is not enough to send.
        setup(tmp, "latest_fail", [PARENT, WINDOW_POST],
              [PARENT, OLD_POST, WINDOW_POST], latest_fails=True)
        rc = ro.main(args("a brand new reply"))
        check("latest-page read failure -> exit 2, not sent",
              rc == 2 and SENT == [], f"rc={rc} sent={SENT}")

        # 6. Unit level: duplicate_of_other with since_ts finds the
        # old dup through the latest page, and returns None when
        # neither read has it.
        setup(tmp, "unit", [PARENT, WINDOW_POST],
              [PARENT, OLD_POST, WINDOW_POST])
        check("duplicate_of_other finds old dup via latest page",
              ro.duplicate_of_other("C0TEST", "111.1",
                                    "old repeated question",
                                    since_ts="162.200000") == "150.0")
        check("duplicate_of_other no match in either read -> None",
              ro.duplicate_of_other("C0TEST", "111.1", "unrelated",
                                    since_ts="162.200000") is None)
        check("duplicate_of_other ignores own posts in latest page",
              ro.duplicate_of_other("C0TEST", "111.1", "my old reply",
                                    since_ts="162.200000") is None)

        # 7. Manual mode (no target) is unchanged: the full-thread
        # read already covers the latest page, so no second read is
        # needed -- the stub returning the same list for both calls
        # still refuses on an identical post.
        setup(tmp, "manual", [PARENT, OLD_POST], [PARENT, OLD_POST])
        check("manual mode identical post -> ts found",
              ro.duplicate_of_other("C0TEST", "111.1",
                                    "old repeated question") == "150.0")

        # 8. Remaining hole (same as Neiro's): an identical post
        # pushed beyond the latest 100 is still missed. Simulated
        # by a latest page that no longer contains the old post
        # (99 newer messages + parent fill the page) while the
        # window does not contain it either -> sent.
        fillers = [{"user": "U0OTHER2", "ts": f"{3000 + i}.0",
                    "text": f"filler {i}"} for i in range(98)]
        setup(tmp, "pushed_out", [PARENT, WINDOW_POST],
              [PARENT, WINDOW_POST] + fillers)
        rc = ro.main(args("old repeated question"))
        check("identical post beyond latest 100 -> not seen (hole)",
              rc == 0 and SENT == ["old repeated question"],
              f"rc={rc} sent={SENT}")

    print(f"\n{CHECKS[0]} passed, {CHECKS[1]} failed")
    return 1 if CHECKS[1] else 0


if __name__ == "__main__":
    sys.exit(main())
