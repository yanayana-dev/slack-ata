#!/usr/bin/env python3
"""Tests for reply_once.py metadata integration (post_metadata.py).

Completes the three-way agreement with Yama and Neiro in
#slack-へようこそ (thread 1794583788.831849): with --event-id,
event-driven replies are sent metadata-stamped via post_metadata.py
(direct chat.postMessage) and the pre-send duplicate check looks
for our yanayana_reply stamp first (台帳 -> metadata -> 文面);
without --event-id the slackcli send and text-only check are used.

Plain stdlib script, no pytest (same style as the other
reply_once tests). Run:
  python3 tests/test_reply_once_metadata.py
Exit 0 when every check passes; each check prints PASS/FAIL.

The thread-read layer and the post_metadata calls are stubbed, so
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
OTHER = "U0OTHER"
YANAYANA = ro.YANAYANA_USER


class FakeThread:
    """Canned thread messages; records which oldest values were read."""

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


class FakeMetadata:
    """Stub for post_metadata: canned stamp lookup and send."""

    def __init__(self, stamped_ts=None):
        self.stamped_ts = stamped_ts  # ts of the stamped reply, or None
        self.fail_read = False
        self.send_calls = []
        self.send_result = (True, {"ts": "3000.000001"})

    def resolve_token(self):
        return "fake-test-token"

    def find_reply(self, channel, thread_ts, token, event_id=None,
                   request_id=None):
        if self.fail_read:
            raise RuntimeError("metadata read failed")
        if self.stamped_ts is not None:
            return {"ts": self.stamped_ts, "text": OTHER_MSG}
        return None

    def send_reply(self, channel, thread_ts, text, event_id=None,
                   request_id=None, **kwargs):
        self.send_calls.append({"channel": channel, "thread_ts": thread_ts,
                                "text": text, "event_id": event_id})
        return self.send_result


def install_fake_metadata(fake):
    saved = (ro.post_metadata.resolve_token,
             ro.post_metadata.find_reply,
             ro.post_metadata.send_reply)
    ro.post_metadata.resolve_token = fake.resolve_token
    ro.post_metadata.find_reply = fake.find_reply
    ro.post_metadata.send_reply = fake.send_reply
    return saved


def restore_metadata(saved):
    (ro.post_metadata.resolve_token,
     ro.post_metadata.find_reply,
     ro.post_metadata.send_reply) = saved


def test_stamp_found_blocks_different_text(tmp):
    """A metadata stamp proves the event was answered even when the
    reply text differs from the draft (retry with edited wording)."""
    fake = FakeMetadata(stamped_ts="2010.000000")
    saved = install_fake_metadata(fake)
    try:
        setup(tmp, "stamp", [parent(),
                             msg("2010.000000", YANAYANA, OTHER_MSG)])
        found = ro.already_replied("C0TEST", THREAD_TS,
                                   target_ts=TARGET_TS, message=MSG,
                                   event_id=EVENT_ID)
        check("metadata stamp found -> already-replied (thread)",
              found == "thread", f"found={found}")
    finally:
        restore_metadata(saved)


def test_stamp_absent_falls_back_to_text(tmp):
    """No stamp: the text check still decides (same text blocks,
    different text does not)."""
    fake = FakeMetadata(stamped_ts=None)
    saved = install_fake_metadata(fake)
    try:
        setup(tmp, "nostamp_same", [parent(),
                                    msg("2010.000000", YANAYANA, MSG)])
        found = ro.already_replied("C0TEST", THREAD_TS,
                                   target_ts=TARGET_TS, message=MSG,
                                   event_id=EVENT_ID)
        check("no stamp + same text -> already-replied (thread)",
              found == "thread", f"found={found}")
        setup(tmp, "nostamp_diff", [parent(),
                                    msg("2010.000000", YANAYANA, OTHER_MSG)])
        found2 = ro.already_replied("C0TEST", THREAD_TS,
                                    target_ts=TARGET_TS, message=MSG,
                                    event_id=EVENT_ID)
        check("no stamp + different text -> not replied",
              found2 is None, f"found={found2}")
    finally:
        restore_metadata(saved)


def test_metadata_read_failure_falls_back(tmp):
    """A failed metadata read does not block a reply the text check
    can verify -- it falls back instead of failing closed on the
    extra channel (the text check itself stays fail-closed)."""
    fake = FakeMetadata()
    fake.fail_read = True
    saved = install_fake_metadata(fake)
    try:
        setup(tmp, "metafail_same", [parent(),
                                     msg("2010.000000", YANAYANA, MSG)])
        found = ro.already_replied("C0TEST", THREAD_TS,
                                   target_ts=TARGET_TS, message=MSG,
                                   event_id=EVENT_ID)
        check("metadata read fails + same text -> already-replied",
              found == "thread", f"found={found}")
        setup(tmp, "metafail_diff", [parent(),
                                     msg("2010.000000", YANAYANA, OTHER_MSG)])
        found2 = ro.already_replied("C0TEST", THREAD_TS,
                                    target_ts=TARGET_TS, message=MSG,
                                    event_id=EVENT_ID)
        check("metadata read fails + different text -> not replied",
              found2 is None, f"found2={found2}")
    finally:
        restore_metadata(saved)


def test_no_event_id_skips_metadata(tmp):
    """Without --event-id the metadata check is never consulted:
    a stamp for the event must not block a manual post."""
    fake = FakeMetadata(stamped_ts="2010.000000")
    calls = []
    orig_find = fake.find_reply

    def counting_find(*a, **kw):
        calls.append(True)
        return orig_find(*a, **kw)
    fake.find_reply = counting_find
    saved = install_fake_metadata(fake)
    try:
        setup(tmp, "noevent", [parent(),
                               msg("2010.000000", YANAYANA, OTHER_MSG)])
        found = ro.already_replied("C0TEST", THREAD_TS,
                                   target_ts=TARGET_TS, message=MSG)
        check("no event_id -> not replied (different text)",
              found is None, f"found={found}")
        check("no event_id -> metadata check never called",
              calls == [], f"calls={len(calls)}")
    finally:
        restore_metadata(saved)


def test_send_with_event_id_uses_metadata(tmp):
    """send() with event_id goes through post_metadata.send_reply
    and returns the ts; a failed metadata send raises so the
    caller's recovery flow runs."""
    fake = FakeMetadata()
    saved = install_fake_metadata(fake)
    try:
        ts = ro.send("C0TEST", THREAD_TS, MSG, event_id=EVENT_ID)
        check("send with event_id -> metadata send returns ts",
              ts == "3000.000001", f"ts={ts}")
        check("send with event_id -> called with the event_id",
              fake.send_calls == [{"channel": "C0TEST",
                                   "thread_ts": THREAD_TS,
                                   "text": MSG, "event_id": EVENT_ID}],
              f"calls={fake.send_calls}")
        fake.send_result = (False, "slack: channel_not_found")
        try:
            ro.send("C0TEST", THREAD_TS, MSG, event_id=EVENT_ID)
            raised = False
        except RuntimeError:
            raised = True
        check("failed metadata send -> RuntimeError (recovery path)",
              raised)
    finally:
        restore_metadata(saved)


class FakeSubprocess:
    """Stub for subprocess.run: the slackcli send path."""

    def __init__(self):
        self.calls = []

    def run(self, cmd, **kwargs):
        self.calls.append(cmd)
        import json as _json

        class Result:
            returncode = 0
            stdout = _json.dumps({"ok": True, "ts": "3000.000002"})
            stderr = ""
        return Result()


def test_send_without_event_id_uses_slackcli(tmp):
    """send() without event_id keeps the slackcli path unchanged."""
    fake_sub = FakeSubprocess()
    saved_sub = ro.subprocess
    ro.subprocess = fake_sub
    try:
        ts = ro.send("C0TEST", THREAD_TS, MSG)
        check("send without event_id -> slackcli ts returned",
              ts == "3000.000002", f"ts={ts}")
        check("send without event_id -> slackcli messages send called",
              len(fake_sub.calls) == 1
              and fake_sub.calls[0][:3] == [ro.SLACKCLI, "messages", "send"],
              f"calls={fake_sub.calls}")
    finally:
        ro.subprocess = saved_sub


def test_main_with_event_id_sends_stamped_and_records(tmp):
    """Full main() run with --event-id: metadata send, ledger
    record; a second run for the same target is already-replied
    via the record without sending again."""
    fake = FakeMetadata()
    saved = install_fake_metadata(fake)
    try:
        setup(tmp, "main", [parent()])
        rc = ro.main(["--channel", "C0TEST", "--thread-ts", THREAD_TS,
                      "--target-ts", TARGET_TS, "--event-id", EVENT_ID,
                      "--message", MSG])
        check("main with event_id -> exit 0 (sent)",
              rc == 0, f"rc={rc}")
        check("main with event_id -> one metadata send",
              len(fake.send_calls) == 1
              and fake.send_calls[0]["event_id"] == EVENT_ID,
              f"calls={fake.send_calls}")
        records = ro.load_record()
        check("main with event_id -> reply recorded with sent ts",
              len(records) == 1 and records[0].get("reply_ts") == "3000.000001"
              and records[0].get("target_ts") == TARGET_TS,
              f"records={records}")
        rc2 = ro.main(["--channel", "C0TEST", "--thread-ts", THREAD_TS,
                       "--target-ts", TARGET_TS, "--event-id", EVENT_ID,
                       "--message", MSG])
        check("second run same target -> exit 0 already-replied",
              rc2 == 0, f"rc2={rc2}")
        check("second run -> no second send",
              len(fake.send_calls) == 1, f"calls={len(fake.send_calls)}")
    finally:
        restore_metadata(saved)


def test_main_metadata_stamp_blocks_resend(tmp):
    """main() with --event-id: when the metadata check finds the
    stamp (record missing, e.g. crash after send before record),
    nothing is sent."""
    fake = FakeMetadata(stamped_ts="2010.000000")
    saved = install_fake_metadata(fake)
    try:
        setup(tmp, "mainstamp", [parent(),
                                 msg("2010.000000", YANAYANA, OTHER_MSG)])
        rc = ro.main(["--channel", "C0TEST", "--thread-ts", THREAD_TS,
                      "--target-ts", TARGET_TS, "--event-id", EVENT_ID,
                      "--message", MSG])
        check("stamp found at main level -> exit 0, no send",
              rc == 0 and fake.send_calls == [],
              f"rc={rc} calls={fake.send_calls}")
    finally:
        restore_metadata(saved)


def main():
    with tempfile.TemporaryDirectory() as tmp:
        test_stamp_found_blocks_different_text(tmp)
        test_stamp_absent_falls_back_to_text(tmp)
        test_metadata_read_failure_falls_back(tmp)
        test_no_event_id_skips_metadata(tmp)
        test_send_with_event_id_uses_metadata(tmp)
        test_send_without_event_id_uses_slackcli(tmp)
        test_main_with_event_id_sends_stamped_and_records(tmp)
        test_main_metadata_stamp_blocks_resend(tmp)
    print(f"\n{CHECKS[0]} passed, {CHECKS[1]} failed")
    return 0 if CHECKS[1] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
