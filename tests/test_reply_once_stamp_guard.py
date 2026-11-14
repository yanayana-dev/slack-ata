#!/usr/bin/env python3
"""Tests for the stamp guard in reply_once.stamped_texts.

Added 2026-11-15 from Neiro's correction in #slack-へようこそ:
Yama had shared that their text matching excluded replies stamped
with a different event_id, then found the guard was never in their
code -- the ledger and thread text checks read without metadata, so
a same-text reply to another event counted as a duplicate. Our
slackcli text fallback had the same gap, so the text check now
reads the thread with include_all_metadata and drops our own
messages stamped with a different event_id (台帳 -> metadata ->
文面, with the 文面 step stamp-aware).

Plain stdlib script, no pytest (same style as the other
reply_once tests). Run:
  python3 tests/test_reply_once_stamp_guard.py
Exit 0 when every check passes; each check prints PASS/FAIL.

The post_metadata calls and the thread-read layer are stubbed, so
no real Slack call is made and the real events/ directory is never
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
SPEC_PM = importlib.util.spec_from_file_location(
    "post_metadata", os.path.join(ROOT, "post_metadata.py"))
pm = importlib.util.module_from_spec(SPEC_PM)
SPEC_PM.loader.exec_module(pm)

CHECKS = [0, 0]  # [passed, failed]


def check(name, cond, detail=""):
    CHECKS[0 if cond else 1] += 1
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not cond else ""))
    return cond


MSG = "test reply text"
OTHER_MSG = "a different reply text"
THREAD_TS = "1000.000000"
TARGET_TS = "2000.000000"
EVENT_ID = "Ev0TEST00001"
OTHER_EVENT = "Ev0TEST00002"
OTHER = "U0OTHER"
YANAYANA = ro.YANAYANA_USER


class FakeThread:
    """Canned thread messages for the slackcli read path."""

    def __init__(self, messages):
        self.messages = messages
        self.fail = False

    def read_page(self, channel, thread_ts, oldest=None, attempts=5):
        if self.fail:
            raise RuntimeError("read failed")
        msgs = [m for m in self.messages
                if oldest is None or float(m["ts"]) >= float(oldest)]
        return {"messages": msgs, "has_more": False, "next_oldest": None}


def msg(ts, user, text, stamp=None):
    m = {"ts": ts, "user": user, "text": text}
    if stamp is not None:
        m["metadata"] = {"event_type": "yanayana_reply",
                         "event_payload": stamp}
    return m


def parent():
    return msg(THREAD_TS, OTHER, "parent question")


def setup(tmp, name, messages):
    sub = os.path.join(tmp, name)
    os.makedirs(sub, exist_ok=True)
    ro.REPLIED = os.path.join(sub, "replied.jsonl")
    ro.AMBIGUOUS = os.path.join(sub, "ambiguous_posts.jsonl")
    ro.EVENTS = sub
    fake = FakeThread(messages)
    ro.thread_read_page_retry = fake.read_page
    return fake


class FakeMetadata:
    """Stub for post_metadata: canned stamp lookup and thread read."""

    def __init__(self, messages, stamped_ts=None):
        self.messages = messages
        self.stamped_ts = stamped_ts  # ts of the stamp for this event
        self.fail_read = False
        self.read_calls = []

    def resolve_token(self):
        return "fake-test-token"

    def find_reply(self, channel, thread_ts, token, event_id=None,
                   request_id=None):
        if self.fail_read:
            raise RuntimeError("metadata read failed")
        if self.stamped_ts is not None:
            return {"ts": self.stamped_ts, "text": OTHER_MSG}
        return None

    def read_thread(self, channel, thread_ts, token, oldest=None,
                    **kwargs):
        self.read_calls.append(oldest)
        if self.fail_read:
            raise RuntimeError("metadata read failed")
        return [m for m in self.messages
                if oldest is None or float(m["ts"]) >= float(oldest)]


def install_fake_metadata(fake):
    saved = (ro.post_metadata.resolve_token,
             ro.post_metadata.find_reply,
             ro.post_metadata.read_thread)
    ro.post_metadata.resolve_token = fake.resolve_token
    ro.post_metadata.find_reply = fake.find_reply
    ro.post_metadata.read_thread = fake.read_thread
    return saved


def restore_metadata(saved):
    (ro.post_metadata.resolve_token,
     ro.post_metadata.find_reply,
     ro.post_metadata.read_thread) = saved


def test_stamp_event_id_helper():
    """stamp_event_id reads our event_id stamp, nothing else."""
    check("stamp_event_id: our stamp -> event_id",
          pm.stamp_event_id(msg("2010.000000", YANAYANA, MSG,
                                {"event_id": EVENT_ID})) == EVENT_ID)
    check("stamp_event_id: request_id stamp -> None",
          pm.stamp_event_id(msg("2010.000000", YANAYANA, MSG,
                                {"request_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"})) is None)
    check("stamp_event_id: no metadata -> None",
          pm.stamp_event_id(msg("2010.000000", YANAYANA, MSG)) is None)
    check("stamp_event_id: other bot's stamp -> None",
          pm.stamp_event_id({"ts": "2010.000000", "user": YANAYANA,
                             "text": MSG,
                             "metadata": {"event_type": "yama_reply",
                                          "event_payload": {"event_id": EVENT_ID}}}) is None)


def test_other_event_same_text_not_duplicate(tmp):
    """The guard itself: a same-text reply stamped with a different
    event_id must not block this event's send."""
    fake = FakeMetadata([parent(),
                         msg("2010.000000", YANAYANA, MSG,
                             {"event_id": OTHER_EVENT})])
    saved = install_fake_metadata(fake)
    try:
        setup(tmp, "other_event", fake.messages)
        found = ro.already_replied("C0TEST", THREAD_TS,
                                   target_ts=TARGET_TS, message=MSG,
                                   event_id=EVENT_ID)
        check("other event's same-text reply -> not replied",
              found is None, f"found={found}")
    finally:
        restore_metadata(saved)


def test_unstamped_same_text_still_duplicate(tmp):
    """No stamp (sent before stamping existed): the text check
    still catches the duplicate."""
    fake = FakeMetadata([parent(),
                         msg("2010.000000", YANAYANA, MSG)])
    saved = install_fake_metadata(fake)
    try:
        setup(tmp, "unstamped", fake.messages)
        found = ro.already_replied("C0TEST", THREAD_TS,
                                   target_ts=TARGET_TS, message=MSG,
                                   event_id=EVENT_ID)
        check("unstamped same text -> already-replied (thread)",
              found == "thread", f"found={found}")
    finally:
        restore_metadata(saved)


def test_request_id_stamp_stays_candidate(tmp):
    """A request_id stamp carries no event_id, so it stays a text
    candidate instead of being excluded."""
    fake = FakeMetadata([parent(),
                         msg("2010.000000", YANAYANA, MSG,
                             {"request_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"})])
    saved = install_fake_metadata(fake)
    try:
        setup(tmp, "request_id", fake.messages)
        found = ro.already_replied("C0TEST", THREAD_TS,
                                   target_ts=TARGET_TS, message=MSG,
                                   event_id=EVENT_ID)
        check("request_id-stamped same text -> already-replied",
              found == "thread", f"found={found}")
    finally:
        restore_metadata(saved)


def test_stamped_read_failure_falls_back(tmp):
    """A failed stamped read falls back to the slackcli text read
    (degraded, with a warning) instead of failing closed."""
    fake = FakeMetadata([parent(),
                         msg("2010.000000", YANAYANA, MSG)])
    fake.fail_read = True
    saved = install_fake_metadata(fake)
    try:
        setup(tmp, "stamped_fail", fake.messages)
        found = ro.already_replied("C0TEST", THREAD_TS,
                                   target_ts=TARGET_TS, message=MSG,
                                   event_id=EVENT_ID)
        check("stamped read fails + same text (slackcli) -> already-replied",
              found == "thread", f"found={found}")
    finally:
        restore_metadata(saved)


def test_no_event_id_uses_slackcli_text(tmp):
    """Without --event-id the stamped read is never consulted."""
    fake = FakeMetadata([parent(),
                         msg("2010.000000", YANAYANA, MSG,
                             {"event_id": OTHER_EVENT})])
    saved = install_fake_metadata(fake)
    try:
        setup(tmp, "noevent_guard", fake.messages)
        found = ro.already_replied("C0TEST", THREAD_TS,
                                   target_ts=TARGET_TS, message=MSG)
        check("no event_id + same text -> already-replied (text check)",
              found == "thread", f"found={found}")
        check("no event_id -> stamped read never called",
              fake.read_calls == [], f"calls={fake.read_calls}")
    finally:
        restore_metadata(saved)


def main():
    test_stamp_event_id_helper()
    with tempfile.TemporaryDirectory() as tmp:
        test_other_event_same_text_not_duplicate(tmp)
        test_unstamped_same_text_still_duplicate(tmp)
        test_request_id_stamp_stays_candidate(tmp)
        test_stamped_read_failure_falls_back(tmp)
        test_no_event_id_uses_slackcli_text(tmp)
    print(f"\n{CHECKS[0]} passed, {CHECKS[1]} failed")
    return 0 if CHECKS[1] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
