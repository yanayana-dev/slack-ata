#!/usr/bin/env python3
"""Tests for reply_once.py one-page fail-closed windowed read.

Adopted 2026-11-14 from Yama's proposal in #slack-へようこそ
(thread 1794583788.831849): the target-mode pre-send read starts at
the target event (60s margin), reads at most one page (100
messages), and when more than a page follows the target it refuses
to send (fail-closed) instead of treating the question as
unanswered -- a reply hours later to a question buried under 100+
posts would be out of place, and paging onward is the same
unbounded-read weakness the window was meant to remove.

Plain stdlib script, no pytest (same style as
test_reply_once_windowed_read.py). Run:
  python3 tests/test_reply_once_fail_closed_window.py
Exit 0 when every check passes; each check prints PASS/FAIL.

The thread-read layer is stubbed with a paged fake (2 messages per
page) so the tests prove the one-page cap and the fail-closed
behaviour. No real Slack call is made and the real events/
directory is never touched.
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


class PagedFakeThread:
    """Canned thread messages, paged 2 per page oldest-first."""

    PAGE = 2

    def __init__(self, messages):
        self.messages = sorted(messages, key=lambda m: float(m["ts"]))
        self.oldests = []

    def read_page(self, channel, thread_ts, oldest=None, attempts=5):
        self.oldests.append(oldest)
        rest = [m for m in self.messages
                if oldest is None or float(m["ts"]) > float(oldest)]
        page = rest[:self.PAGE]
        has_more = len(rest) > self.PAGE
        return {"messages": page, "has_more": has_more,
                "next_oldest": page[-1]["ts"] if page and has_more else None}


def msg(ts, user, text):
    return {"ts": ts, "user": user, "text": text}


def parent():
    return msg(THREAD_TS, OTHER, "parent question")


def setup(tmp, name, messages):
    sub = os.path.join(tmp, name)
    os.makedirs(sub, exist_ok=True)
    ro.REPLIED = os.path.join(sub, "replied.jsonl")
    ro.AMBIGUOUS = os.path.join(sub, "ambiguous_posts.jsonl")
    ro.EVENTS = sub
    fake = PagedFakeThread(messages)
    ro.thread_read_page_retry = fake.read_page
    return fake


def test_window_fits_in_one_page(tmp):
    """Window of 2 messages: the duplicate check still works and
    reads exactly one page starting at target - 60s."""
    fake = setup(tmp, "fits", [parent(),
                               msg("1950.000000", OTHER, "other post"),
                               msg("2010.000000", YANAYANA, MSG)])
    found = ro.already_replied("C0TEST", THREAD_TS,
                               target_ts=TARGET_TS, message=MSG)
    check("window fits in one page -> duplicate detected",
          found == "thread", f"found={found}")
    check("window fits in one page -> single read at window start",
          fake.oldests == [WINDOW_START], f"oldests={fake.oldests}")


def test_window_overflow_is_fail_closed(tmp):
    """More than one page follows the target: the check cannot
    verify, so already_replied is unknown and main refuses to send
    (exit 2) instead of treating the question as unanswered."""
    fake = setup(tmp, "overflow", [parent(),
                                   msg("1950.000000", OTHER, "a"),
                                   msg("1960.000000", OTHER, "b"),
                                   msg("1970.000000", OTHER, "c"),
                                   msg("2010.000000", YANAYANA, MSG)])
    found = ro.already_replied("C0TEST", THREAD_TS,
                               target_ts=TARGET_TS, message=MSG)
    check("window over one page -> already_replied unknown",
          found == "unknown", f"found={found}")
    check("window over one page -> only one page read",
          fake.oldests == [WINDOW_START], f"oldests={fake.oldests}")
    rc = ro.main(["--channel", "C0TEST", "--thread-ts", THREAD_TS,
                  "--target-ts", TARGET_TS, "--message", MSG,
                  "--check-only"])
    check("window over one page -> main exits 2 (cannot-verify)",
          rc == 2, f"rc={rc}")


def test_duplicate_of_other_overflow_raises(tmp):
    """Parroting guard in target mode also stops at one page: an
    overflowing window raises instead of returning a partial scan."""
    setup(tmp, "parrot_overflow", [parent(),
                                   msg("1950.000000", OTHER, "a"),
                                   msg("1960.000000", OTHER, "b"),
                                   msg("1970.000000", OTHER, "c")])
    raised = False
    try:
        ro.duplicate_of_other("C0TEST", THREAD_TS, MSG,
                              since_ts=WINDOW_START)
    except RuntimeError:
        raised = True
    check("parroting guard over one page -> raises (fail-closed)",
          raised)


def test_recovery_check_overflow_unverifiable(tmp):
    """Recovery check over a one-page window returns None (could not
    verify) rather than False (confirmed absent)."""
    setup(tmp, "rec_overflow", [parent(),
                                msg("1950.000000", OTHER, "a"),
                                msg("1960.000000", OTHER, "b"),
                                msg("1970.000000", OTHER, "c")])
    found = ro.recovery_check("C0TEST", THREAD_TS, MSG,
                              since_ts=WINDOW_START, attempts=1)
    check("recovery check over one page -> None (unverifiable)",
          found is None, f"found={found}")


def test_manual_mode_still_pages_full_thread(tmp):
    """No target event (manual post) keeps the full-thread read:
    paging continues past the first page and a same-text reply on a
    later page still blocks."""
    fake = setup(tmp, "manual", [parent(),
                                 msg("1500.000000", YANAYANA, MSG),
                                 msg("1600.000000", OTHER, "x"),
                                 msg("1700.000000", OTHER, "y")])
    found = ro.already_replied("C0TEST", THREAD_TS,
                               target_ts=None, message=MSG)
    check("manual mode -> same text on later page blocks",
          found == "thread", f"found={found}")
    check("manual mode -> paging continues past first page",
          fake.oldests == [THREAD_TS, "1600.000000"],
          f"oldests={fake.oldests}")


def main():
    with tempfile.TemporaryDirectory() as tmp:
        test_window_fits_in_one_page(tmp)
        test_window_overflow_is_fail_closed(tmp)
        test_duplicate_of_other_overflow_raises(tmp)
        test_recovery_check_overflow_unverifiable(tmp)
        test_manual_mode_still_pages_full_thread(tmp)
    print(f"\n{CHECKS[0]} passed, {CHECKS[1]} failed")
    return 0 if CHECKS[1] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
