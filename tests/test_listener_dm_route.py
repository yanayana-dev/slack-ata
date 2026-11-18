#!/usr/bin/env python3
"""Tests for listener.py's user-DM routing (added 2026-11-14).

Since the app subscribed to message.im / message.mpim on behalf of
users, the Socket Mode listener also receives events for DMs
addressed to あた (the authorizing user). listener.is_user_dm_event
classifies them from the envelope's authorizations, and
listener.handle_user_dm_event sends them to events/dm_inbox.jsonl
(source "socket_push") while advancing dm_poll's watermark so the
5-minute poller never ingests the same message twice.

These tests import listener and dm_poll in-process and patch their
path globals to a temp dir, so the real events/ directory is never
touched and no socket is opened (slack_sdk is imported only inside
listener.main). The seen-set discipline is replaced with a small
in-memory fake with the same semantics (either key blocks).

Plain stdlib script, no pytest (same style as the other suites).
Run:
  python3 tests/test_listener_dm_route.py
Exit 0 when every check passes; each check prints PASS/FAIL.
"""
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import dm_poll
import listener

CHECKS = [0, 0]  # [passed, failed]

ATA = listener.ATA_USER_ID       # U0EXAMPLEOWNER, the authorizing user
OTHER = "U0EXAMPLEUSER2"            # Yama
BOT = "U0EXAMPLEBOT"              # Yanayana bot user
CONV = "D0EXAMPLE001"             # an im conversation with OTHER
GROUP_CONV = "D0EXAMPLE003"       # an mpim conversation


def check(name, cond, detail=""):
    CHECKS[0 if cond else 1] += 1
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not cond else ""))
    return cond


def dm_event(user=OTHER, ts="5000.000001", channel=CONV,
             channel_type="im", **kw):
    e = {"type": "message", "channel": channel,
         "channel_type": channel_type, "user": user,
         "text": "hello there", "ts": ts}
    e.update(kw)
    return e


def payload(event, authorizations, event_id="Ev0000000001"):
    return {"event_id": event_id, "type": "event_callback",
            "authorizations": authorizations, "event": event}


def user_auth():
    return [{"enterprise_id": None, "team_id": "T0EXAMPLE001",
             "user_id": ATA, "is_bot": False,
             "is_enterprise_install": False}]


def bot_auth():
    return [{"enterprise_id": None, "team_id": "T0EXAMPLE001",
             "user_id": BOT, "is_bot": True,
             "is_enterprise_install": False}]


def user_payload(event, event_id="EvUSER000001"):
    return payload(event, user_auth(), event_id)


def bot_payload(event, event_id="EvBOT0000001"):
    return payload(event, bot_auth(), event_id)


class Seen:
    """In-memory stand-in for the listener's seen-set discipline."""

    def __init__(self):
        self.keys = set()

    def is_duplicate(self, event_id, ts_key):
        return bool((event_id and event_id in self.keys)
                    or ts_key in self.keys)

    def mark(self, *keys):
        for k in keys:
            if k:
                self.keys.add(k)


class Rig:
    """Temp events dir wired into listener/dm_poll path globals."""

    def __init__(self):
        self.base = tempfile.mkdtemp(prefix="listener-dm-test-")
        self.events = os.path.join(self.base, "events")
        os.makedirs(self.events)
        self._saved = {}
        for mod, names in (
                (dm_poll, ("EVENTS", "DM_STATE", "DM_LOCK", "DM_INBOX")),
                (listener, ("DM_INBOX_PATH", "INBOX_PATH"))):
            for name in names:
                self._saved[(mod, name)] = getattr(mod, name)
        dm_poll.EVENTS = self.events
        dm_poll.DM_STATE = os.path.join(self.events, "dm_state.json")
        dm_poll.DM_LOCK = os.path.join(self.events, "dm_state.lock")
        dm_poll.DM_INBOX = os.path.join(self.events, "dm_inbox.jsonl")
        listener.DM_INBOX_PATH = dm_poll.DM_INBOX
        listener.INBOX_PATH = os.path.join(self.events, "inbox.jsonl")
        self.seen = Seen()

    def handle(self, pl):
        return listener.handle_user_dm_event(
            pl, pl["event"], self.seen.is_duplicate, self.seen.mark)

    def dm_inbox(self):
        if not os.path.exists(dm_poll.DM_INBOX):
            return []
        with open(dm_poll.DM_INBOX, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def state(self):
        return dm_poll.load_dm_state()

    def cleanup(self):
        for (mod, name), val in self._saved.items():
            setattr(mod, name, val)
        shutil.rmtree(self.base, ignore_errors=True)


def test_classification():
    ev = dm_event()
    check("user DM event (im, Ata authorization) goes to the DM path",
          listener.is_user_dm_event(user_payload(ev), ev) is True)
    mp = dm_event(channel=GROUP_CONV, channel_type="mpim")
    check("user group DM event (mpim) goes to the DM path",
          listener.is_user_dm_event(user_payload(mp), mp) is True)
    check("bot DM event (bot authorization) stays on the main path",
          listener.is_user_dm_event(bot_payload(ev), ev) is False)
    ch = dm_event(channel="C0EXAMPLE001", channel_type="channel")
    check("channel message stays on the main path even with a user "
          "authorization",
          listener.is_user_dm_event(user_payload(ch), ch) is False)
    grp = dm_event(channel="C0EXAMPLE002", channel_type="group")
    check("private channel message stays on the main path",
          listener.is_user_dm_event(user_payload(grp), grp) is False)
    bare = {"event_id": "EvX", "event": ev}
    check("envelope without authorizations stays on the main path",
          listener.is_user_dm_event(bare, ev) is False)
    bot_flagged = payload(ev, [{"user_id": ATA, "is_bot": True}])
    check("an authorization for Ata's id flagged as bot does not "
          "count as a user authorization",
          listener.is_user_dm_event(bot_flagged, ev) is False)


def test_handle_appends_with_poll_shape():
    rig = Rig()
    try:
        ev = dm_event()
        outcome = rig.handle(user_payload(ev))
        check("user DM is appended", outcome == "appended",
              f"outcome={outcome}")
        lines = rig.dm_inbox()
        check("dm_inbox holds exactly one record", len(lines) == 1,
              f"lines={lines}")
        if lines:
            rec = lines[0]
            expected = dm_poll.to_record(
                ev, {"id": CONV, "channel_type": "im"},
                rec["received_at"])
            check("record shape is dm_poll's shape (same keys)",
                  set(rec.keys()) == set(expected.keys()),
                  f"keys={sorted(rec.keys())} vs {sorted(expected.keys())}")
            check("record is stamped socket_push with the event data",
                  rec.get("source") == "socket_push"
                  and rec.get("channel") == CONV
                  and rec.get("conversation") == CONV
                  and rec.get("channel_type") == "im"
                  and rec.get("user") == OTHER
                  and rec.get("text") == "hello there"
                  and rec.get("ts") == "5000.000001",
                  f"rec={rec}")
        check("main inbox is never touched by the DM path",
              not os.path.exists(listener.INBOX_PATH))
        st = rig.state()
        check("watermark advanced to the pushed ts",
              st.get(CONV, {}).get("watermark") == "5000.000001",
              f"state={st}")
    finally:
        rig.cleanup()


def test_own_and_subtype_skips():
    rig = Rig()
    try:
        dm_poll.save_dm_state({CONV: {"watermark": "4000.000000",
                                      "updated_at": "seed"}})
        own = dm_event(user=ATA, ts="6000.000001")
        outcome = rig.handle(user_payload(own, "EvOWN0000001"))
        check("Ata's own DM message is skipped",
              outcome == "skipped_own", f"outcome={outcome}")
        check("own message appends nothing", rig.dm_inbox() == [])
        check("own message does not advance the watermark",
              rig.state()[CONV]["watermark"] == "4000.000000",
              f"state={rig.state()}")
        for subtype in ("message_changed", "message_deleted",
                        "message_replied"):
            ev = dm_event(ts="6001.000001", subtype=subtype)
            outcome = rig.handle(
                user_payload(ev, f"EvSUB{subtype}"))
            check(f"subtype {subtype} is skipped like the main path",
                  outcome == "skipped_subtype", f"outcome={outcome}")
        check("subtype skips append nothing", rig.dm_inbox() == [])
        check("subtype skips do not advance the watermark",
              rig.state()[CONV]["watermark"] == "4000.000000")
    finally:
        rig.cleanup()


def test_watermark_forward_only():
    rig = Rig()
    try:
        check("first advance moves the watermark",
              listener.advance_dm_watermark(CONV, "100.500000") is True)
        check("watermark stored in dm_poll's format",
              rig.state()[CONV]["watermark"] == "100.500000",
              f"state={rig.state()}")
        check("an older ts does not move it backward",
              listener.advance_dm_watermark(CONV, "99.000000") is False)
        check("the same ts is not an advance",
              listener.advance_dm_watermark(CONV, "100.500000") is False)
        check("watermark unchanged after backward/equal attempts",
              rig.state()[CONV]["watermark"] == "100.500000")
        check("a newer ts advances again",
              listener.advance_dm_watermark(CONV, "101.000000") is True
              and rig.state()[CONV]["watermark"] == "101.000000")
        check("unparseable or missing values are refused",
              listener.advance_dm_watermark(CONV, "not-a-ts") is False
              and listener.advance_dm_watermark(CONV, None) is False
              and listener.advance_dm_watermark(None, "1.0") is False)
        check("other conversations are untouched",
              set(rig.state().keys()) == {CONV})
    finally:
        rig.cleanup()


def test_poll_does_not_reingest_pushed_message():
    rig = Rig()
    try:
        pushed = dm_event(ts="5000.000001")
        outcome = rig.handle(user_payload(pushed))
        check("push appends before the simulated poll",
              outcome == "appended", f"outcome={outcome}")
        newer = {"ts": "5001.000002", "user": OTHER,
                 "text": "second message"}
        history = [newer, dict(pushed)]  # newest first, like Slack
        orig_fetch = dm_poll.fetch_history
        dm_poll.fetch_history = lambda helper, cid, oldest=None, \
            limit=200: list(history)
        try:
            state = dm_poll.load_dm_state()
            records, err = dm_poll.poll_conversation(
                "unused-helper", state,
                {"id": CONV, "channel_type": "im"},
                "2026-11-14T13:00:00+00:00")
        finally:
            dm_poll.fetch_history = orig_fetch
        check("simulated poll runs clean", err is None, f"err={err}")
        check("poll returns only the newer message (no double ingest)",
              len(records) == 1
              and records[0].get("ts") == "5001.000002",
              f"records={records}")
        check("poll advances the watermark past the newer message",
              state[CONV]["watermark"] == "5001.000002",
              f"state={state}")
    finally:
        rig.cleanup()


def test_dedupe_on_redelivery():
    rig = Rig()
    try:
        pl = user_payload(dm_event())
        first = rig.handle(pl)
        second = rig.handle(pl)
        check("first delivery is appended", first == "appended",
              f"first={first}")
        check("redelivery of the same event_id is a duplicate",
              second == "duplicate", f"second={second}")
        check("duplicate appends nothing", len(rig.dm_inbox()) == 1)
        fresh_envelope = user_payload(dm_event(), "EvUSER000002")
        third = rig.handle(fresh_envelope)
        check("same channel:ts under a new event_id is still a "
              "duplicate", third == "duplicate", f"third={third}")
        check("inbox still holds exactly one record",
              len(rig.dm_inbox()) == 1)
    finally:
        rig.cleanup()


def main():
    test_classification()
    test_handle_appends_with_poll_shape()
    test_own_and_subtype_skips()
    test_watermark_forward_only()
    test_poll_does_not_reingest_pushed_message()
    test_dedupe_on_redelivery()
    print(f"\n{CHECKS[0]} passed, {CHECKS[1]} failed")
    return 1 if CHECKS[1] else 0


if __name__ == "__main__":
    sys.exit(main())
