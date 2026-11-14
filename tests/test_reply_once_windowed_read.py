#!/usr/bin/env python3
"""Tests for reply_once.py windowed pre-send read.

Adopted 2026-11-14 from Yama's share in #slack-へようこそ: the
pre-send check now reads only from 60s before the target event
onward, instead of re-reading the whole thread. Yama's new tests
were: duplicate after the event detected, pre-event boilerplate not
mistaken for a duplicate, detection within the margin, the
answered-check maintained, and read failure staying unverifiable.
The same five cases are covered here, plus: manual (no-target) mode
still reads the full thread, and the parroting guard
(duplicate_of_other) is windowed the same way.

Plain stdlib script, no pytest (same style as
test_reply_once_recovery.py). Run:
  python3 tests/test_reply_once_windowed_read.py
Exit 0 when every check passes; each check prints PASS/FAIL.

The thread-read layer is stubbed with a fake that filters by the
--oldest value exactly like the API does (oldest-first paging from
that ts), so the tests prove which range the code actually asks for.
No real Slack call is made and the real events/ directory is never
touched.
"""
import importlib.util
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
THREAD_TS = "1000.000000"
TARGET_TS = "2000.000000"
WINDOW_START = "1940.000000"  # TARGET_TS - 60s
OTHER = "U0EXAMPLEOTHER"
YANAYANA = ro.YANAYANA_USER


class FakeThread:
    """Canned thread messages, filtered by oldest like the API."""

    def __init__(self, messages):
        self.messages = messages
        self.oldests = []
        self.fail = False

    def read_page(self, channel, thread_ts, oldest=None, attempts=5):
        self.oldests.append(oldest)
        if self.fail:
            raise RuntimeError("read failed")
        msgs = [m for m in self.messages
                if oldest is None or float(m["ts"]) >= float(oldest)]
        return {"messages": msgs, "has_more": False, "next_oldest": None}


def msg(ts, user, text):
    return {"ts": ts, "user": user, "text": text}


def parent():
    return msg(THREAD_TS, OTHER, "parent question")


def setup(tmp, name, messages, fail=False):
    sub = os.path.join(tmp, name)
    os.makedirs(sub, exist_ok=True)
    ro.REPLIED = os.path.join(sub, "replied.jsonl")
    ro.AMBIGUOUS = os.path.join(sub, "ambiguous_posts.jsonl")
    ro.EVENTS = sub
    fake = FakeThread(messages)
    fake.fail = fail
    ro.thread_read_page_retry = fake.read_page
    return fake


def test_window_start(tmp):
    check("window_start is target - 60s",
          ro.window_start(TARGET_TS) == WINDOW_START,
          f"got {ro.window_start(TARGET_TS)}")


def test_duplicate_after_event_detected(tmp):
    """Yanayana reply with the same text after the event -> blocked."""
    fake = setup(tmp, "after", [parent(),
                                msg("2010.000000", YANAYANA, MSG)])
    found = ro.already_replied("C0TEST", THREAD_TS,
                               target_ts=TARGET_TS, message=MSG)
    check("duplicate after event -> already-replied (thread)",
          found == "thread", f"found={found}")
    check("duplicate after event -> read starts at target - 60s",
          fake.oldests == [WINDOW_START], f"oldests={fake.oldests}")


def test_pre_event_boilerplate_not_mistaken(tmp):
    """Same text from Yanayana long before the event is not this
    reply's duplicate (it answered an earlier message)."""
    fake = setup(tmp, "before", [parent(),
                                 msg("1500.000000", YANAYANA, MSG)])
    found = ro.already_replied("C0TEST", THREAD_TS,
                               target_ts=TARGET_TS, message=MSG)
    check("pre-event same text -> not a duplicate",
          found is None, f"found={found}")
    check("pre-event same text -> windowed read used",
          fake.oldests == [WINDOW_START], f"oldests={fake.oldests}")


def test_detection_within_margin(tmp):
    """Same text from Yanayana 30s before the event is still caught:
    the 60s margin covers clock skew / near-simultaneous posts."""
    setup(tmp, "margin", [parent(),
                          msg("1970.000000", YANAYANA, MSG)])
    found = ro.already_replied("C0TEST", THREAD_TS,
                               target_ts=TARGET_TS, message=MSG)
    check("same text within 60s margin -> already-replied (thread)",
          found == "thread", f"found={found}")


def test_recovery_check_within_window(tmp):
    """Answered-check maintained: recovery finds a reply that landed
    just after the target, and confirms absent when only a pre-window
    same-text reply exists."""
    setup(tmp, "rec_found", [parent(),
                             msg("2005.000000", YANAYANA, MSG)])
    found = ro.recovery_check("C0TEST", THREAD_TS, MSG,
                              since_ts=WINDOW_START)
    check("recovery check finds in-window reply",
          found == "2005.000000", f"found={found}")
    setup(tmp, "rec_absent", [parent(),
                              msg("1500.000000", YANAYANA, MSG)])
    absent = ro.recovery_check("C0TEST", THREAD_TS, MSG,
                               since_ts=WINDOW_START)
    check("recovery check ignores pre-window reply (absent)",
          absent is False, f"absent={absent}")


def test_read_failure_unverifiable(tmp):
    """Read failure stays fail-closed: already_replied is unknown and
    main refuses to send (exit 2), never sending blind."""
    setup(tmp, "fail", [parent()], fail=True)
    found = ro.already_replied("C0TEST", THREAD_TS,
                               target_ts=TARGET_TS, message=MSG)
    check("read failure -> already_replied unknown",
          found == "unknown", f"found={found}")
    rc = ro.main(["--channel", "C0TEST", "--thread-ts", THREAD_TS,
                  "--target-ts", TARGET_TS, "--message", MSG,
                  "--check-only"])
    check("read failure -> main exits 2 (cannot-verify)",
          rc == 2, f"rc={rc}")


def test_manual_mode_reads_full_thread(tmp):
    """No target event (manual post) has no anchor, so the read still
    starts at the parent ts and a same-text reply anywhere in the
    thread blocks, as before."""
    fake = setup(tmp, "manual", [parent(),
                                 msg("1500.000000", YANAYANA, MSG)])
    found = ro.already_replied("C0TEST", THREAD_TS,
                               target_ts=None, message=MSG)
    check("manual mode -> same text anywhere blocks",
          found == "thread", f"found={found}")
    check("manual mode -> read starts at parent ts",
          fake.oldests == [THREAD_TS], f"oldests={fake.oldests}")


def test_duplicate_of_other_windowed(tmp):
    """Parroting guard: another author's identical post 30s before
    the target is caught by the windowed read; one from long before
    the target is outside the window but is now caught by the
    send-time latest-page re-check (adopted 2026-11-14 from Neiro's
    share in #slack-へようこそ) -- the old boundary where it was not
    seen at all no longer holds. The remaining hole (identical post
    pushed beyond the latest 100) is covered in
    test_reply_once_latest_page.py."""
    setup(tmp, "parrot_in", [parent(),
                             msg("1970.000000", OTHER, MSG)])
    dup = ro.duplicate_of_other("C0TEST", THREAD_TS, MSG,
                                since_ts=WINDOW_START)
    check("identical post within margin -> duplicate detected",
          dup == "1970.000000", f"dup={dup}")
    fake2 = setup(tmp, "parrot_out", [parent(),
                                      msg("1500.000000", OTHER, MSG)])
    dup2 = ro.duplicate_of_other("C0TEST", THREAD_TS, MSG,
                                 since_ts=WINDOW_START)
    check("identical post before window -> seen via latest-page re-check",
          dup2 == "1500.000000", f"dup2={dup2}")
    check("windowed read first, latest-page read second",
          fake2.oldests == [WINDOW_START, None],
          f"oldests={fake2.oldests}")


def main():
    with tempfile.TemporaryDirectory() as tmp:
        test_window_start(tmp)
        test_duplicate_after_event_detected(tmp)
        test_pre_event_boilerplate_not_mistaken(tmp)
        test_detection_within_margin(tmp)
        test_recovery_check_within_window(tmp)
        test_read_failure_unverifiable(tmp)
        test_manual_mode_reads_full_thread(tmp)
        test_duplicate_of_other_windowed(tmp)
    print(f"\n{CHECKS[0]} passed, {CHECKS[1]} failed")
    return 0 if CHECKS[1] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
