#!/usr/bin/env python3
"""Tests for the pending-resend (DLQ resend) sweep in reconcile_ambiguous.py.

Final form agreed with Yama and Neiro 2026-11-13 in #slack-へようこそ:
a reply whose pre-send check (history read) fails with a transient
error is held, not counted toward the retry budget; the hourly sweep
resends it (prefixed "遅れての再送" + original failure time) when the
thread is readable again, and gives up (record only) when someone else
already answered, 24h pass, or the parent post is gone.

Plain stdlib script, no pytest (same style as test_reconcile_ambiguous.py).
Run:  python3 tests/test_pending_resend.py
Exit 0 when every check passes; each check prints PASS/FAIL.

The thread reader and sender are stubbed and all record files live in
a temp dir, so no real Slack call is made and the real events/
directory is never touched.
"""
import importlib.util
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SPEC = importlib.util.spec_from_file_location(
    "reconcile_ambiguous", os.path.join(ROOT, "reconcile_ambiguous.py"))
ra = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ra)

CHECKS = [0, 0]  # [passed, failed]
YANAYANA = "U0EXAMPLEBOT"
QUESTIONER = "U0EXAMPLEASKER"
OTHER = "U0OTHER"


def check(name, cond, detail=""):
    CHECKS[0 if cond else 1] += 1
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not cond else ""))
    return cond


def read_jsonl(path):
    try:
        with open(path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
    except Exception:
        return []


def setup(tmp):
    ra.BASE = tmp
    ra.EVENTS = os.path.join(tmp, "events")
    ra.STATE_FILE = os.path.join(tmp, "events", "reconcile_ambiguous.state")
    ra.PENDING_RESENDS = os.path.join(tmp, "events", "pending_resends.jsonl")
    ra.ro.BASE = tmp
    ra.ro.EVENTS = os.path.join(tmp, "events")
    ra.ro.REPLIED = os.path.join(tmp, "events", "replied.jsonl")
    ra.ro.AMBIGUOUS = os.path.join(tmp, "events", "ambiguous_posts.jsonl")


def pending_path(tmp):
    return os.path.join(tmp, "events", "pending_resends.jsonl")


def record(tmp, target="222.2", message="hello", questioner=QUESTIONER,
           kind="history_unavailable", failed_at="2026-11-13T14:00:00+00:00",
           event_id=None):
    return ra.record_pending_resend("C0TEST", "111.1", target, message,
                                    questioner, kind, failed_at, event_id)


def thread_with(*msgs):
    # Parent (111.1) + target (222.2) always present unless a test
    # builds its own list.
    base = [{"ts": "111.1", "user": QUESTIONER, "text": "parent"},
            {"ts": "222.2", "user": QUESTIONER, "text": "question?"}]
    return lambda c, t: base + list(msgs)


def clear(tmp):
    """Drop pending-resend and reply records between numbered tests."""
    for name in ("pending_resends.jsonl", "replied.jsonl"):
        path = os.path.join(tmp, "events", name)
        if os.path.exists(path):
            os.remove(path)


def status_of(tmp, target):
    entries = read_jsonl(pending_path(tmp))
    latest = [e for e in entries if e.get("target_ts") == target]
    return latest[-1].get("status") if latest else None


def main():
    now = datetime(2026, 11, 13, 15, 0, 0, tzinfo=timezone.utc).timestamp()
    with tempfile.TemporaryDirectory() as tmp:
        setup(tmp)
        # 1. Readable again -> sends with the "遅れての再送" prefix and
        # the original failure time, records the reply, marks sent.
        record(tmp, target="222.21")
        sent = []
        s = ra.reconcile_pending_resends(
            thread_reader=thread_with(),
            sender=lambda c, t, m: sent.append(m) or "999.9",
            now=now)
        check("readable again resends once", s["sent"] == 1 and s["checked"] == 1, str(s))
        check("resend carries prefix + original failure time (JST)",
              len(sent) == 1 and sent[0].startswith(
                  "遅れての再送（元の失敗時刻：2026-11-13 23:00 JST）\nhello"),
              str(sent))
        replied = read_jsonl(os.path.join(tmp, "events", "replied.jsonl"))
        check("resent reply recorded with ts",
              len(replied) == 1 and replied[0]["reply_ts"] == "999.9"
              and replied[0]["target_ts"] == "222.21", str(replied))
        check("entry marked sent", status_of(tmp, "222.21") == "sent")
        # Re-run: a sent entry is not sent again.
        sent.clear()
        s = ra.reconcile_pending_resends(
            thread_reader=thread_with(),
            sender=lambda c, t, m: sent.append(m) or "999.9",
            now=now)
        check("sent entry is not resent", s["checked"] == 0 and sent == [], str(s))

        clear(tmp)
        # 2. Someone else answered -> give up, no send.
        record(tmp, target="222.22")
        s = ra.reconcile_pending_resends(
            thread_reader=thread_with(
                {"ts": "333.3", "user": OTHER, "text": "I can answer that"}),
            sender=lambda c, t, m: (_ for _ in ()).throw(AssertionError("must not send")),
            now=now)
        check("answered by other gives up", s["given_up"] == 1, str(s))
        check("given-up reason recorded",
              read_jsonl(pending_path(tmp))[-1].get("reason") == "answered_by_other")

        clear(tmp)
        # 3. Questioner's own later post does NOT count as answered.
        record(tmp, target="222.23")
        sent = []
        s = ra.reconcile_pending_resends(
            thread_reader=thread_with(
                {"ts": "333.3", "user": QUESTIONER, "text": "adding detail"}),
            sender=lambda c, t, m: sent.append(m) or "999.9",
            now=now)
        check("questioner's own post does not block resend",
              s["sent"] == 1 and len(sent) == 1, str(s))

        clear(tmp)
        # 4. Yanayana's own later post (different text) does not block.
        record(tmp, target="222.24")
        sent = []
        s = ra.reconcile_pending_resends(
            thread_reader=thread_with(
                {"ts": "333.3", "user": YANAYANA, "text": "something else"}),
            sender=lambda c, t, m: sent.append(m) or "999.9",
            now=now)
        check("own later post does not block resend",
              s["sent"] == 1 and len(sent) == 1, str(s))

        clear(tmp)
        # 5. An empty-text post does NOT count as answered.
        record(tmp, target="222.25")
        sent = []
        s = ra.reconcile_pending_resends(
            thread_reader=thread_with(
                {"ts": "333.3", "user": OTHER, "text": "   "}),
            sender=lambda c, t, m: sent.append(m) or "999.9",
            now=now)
        check("empty-text post does not count as answered",
              s["sent"] == 1 and len(sent) == 1, str(s))

        clear(tmp)
        # 6. 24h window expired -> give up, no send (no +5min grace).
        record(tmp, target="222.26", failed_at="2026-11-12T14:00:00+00:00")
        s = ra.reconcile_pending_resends(
            thread_reader=thread_with(),
            sender=lambda c, t, m: (_ for _ in ()).throw(AssertionError("must not send")),
            now=now)
        check("window expired gives up", s["given_up"] == 1, str(s))
        check("expired reason recorded",
              read_jsonl(pending_path(tmp))[-1].get("reason") == "window_expired")

        clear(tmp)
        # 7. Parent post deleted -> give up, no send.
        record(tmp, target="222.27")
        s = ra.reconcile_pending_resends(
            thread_reader=lambda c, t: [{"ts": "222.2", "user": QUESTIONER,
                                         "text": "question?"}],
            sender=lambda c, t, m: (_ for _ in ()).throw(AssertionError("must not send")),
            now=now)
        check("deleted parent gives up", s["given_up"] == 1, str(s))
        check("deleted-parent reason recorded",
              read_jsonl(pending_path(tmp))[-1].get("reason") == "parent_deleted")

        clear(tmp)
        # 8. Thread read failure -> stays pending, no send.
        record(tmp, target="222.28")

        def boom(c, t):
            raise RuntimeError("slack down")
        s = ra.reconcile_pending_resends(
            thread_reader=boom,
            sender=lambda c, t, m: (_ for _ in ()).throw(AssertionError("must not send")),
            now=now)
        check("read failure leaves entry pending",
              s["skipped_read_failed"] == 1 and s["still_pending"] == 1, str(s))
        check("entry still pending after read failure",
              status_of(tmp, "222.28") == "pending")

        # 9. Send failure -> stays pending for the next hourly pass.
        # (No clear here: this continues test 8's entry.)
        s = ra.reconcile_pending_resends(
            thread_reader=thread_with(),
            sender=lambda c, t, m: (_ for _ in ()).throw(RuntimeError("send down")),
            now=now)
        check("send failure leaves entry pending",
              s["still_pending"] == 1 and s["sent"] == 0, str(s))
        check("entry still pending after send failure",
              status_of(tmp, "222.28") == "pending")

        clear(tmp)
        # 10. Reply already in thread (original text) -> recorded as
        # sent (recovered), never reposted.
        record(tmp, target="222.29")
        sent = []
        s = ra.reconcile_pending_resends(
            thread_reader=thread_with(
                {"ts": "444.4", "user": YANAYANA, "text": "hello"}),
            sender=lambda c, t, m: sent.append(m) or "999.9",
            now=now)
        check("thread-found reply is not reposted",
              s["sent"] == 1 and sent == [], str(s))
        replied = read_jsonl(os.path.join(tmp, "events", "replied.jsonl"))
        check("thread-found reply recorded recovered",
              any(r.get("target_ts") == "222.29" and r.get("recovered") is True
                  and r.get("reply_ts") == "444.4" for r in replied),
              str(replied))

        clear(tmp)
        # 10b. Already-sent check also compares after normalization
        # (Yama's share 2026-11-14, slack-yama commit 2c25158): a
        # delivered reply differing only by Slack escaping (&amp;)
        # or trailing whitespace is still recognised, never reposted.
        record(tmp, target="222.50", message="a & b")
        sent = []
        s = ra.reconcile_pending_resends(
            thread_reader=thread_with(
                {"ts": "444.5", "user": YANAYANA, "text": "a &amp; b  "}),
            sender=lambda c, t, m: sent.append(m) or "999.9",
            now=now)
        check("normalized already-sent reply is not reposted",
              s["sent"] == 1 and sent == [], str(s))
        replied = read_jsonl(os.path.join(tmp, "events", "replied.jsonl"))
        check("normalized already-sent reply recorded recovered",
              any(r.get("target_ts") == "222.50" and r.get("recovered") is True
                  and r.get("reply_ts") == "444.5" for r in replied),
              str(replied))

        clear(tmp)
        # 11. Non-transient error kind is refused at record time.
        try:
            record(tmp, target="222.30", kind="channel_not_found")
            check("permanent error kind refused", False, "no exception raised")
        except ValueError:
            check("permanent error kind refused", True)
        check("refused record writes nothing for the target",
              status_of(tmp, "222.30") is None)

        clear(tmp)
        # 12. Duplicate record for the same target is refused.
        record(tmp, target="222.31")
        try:
            record(tmp, target="222.31")
            check("duplicate target refused", False, "no exception raised")
        except ValueError:
            check("duplicate target refused", True)
        entries = [e for e in read_jsonl(pending_path(tmp))
                   if e.get("target_ts") == "222.31"]
        check("duplicate record writes nothing extra", len(entries) == 1, str(entries))

        clear(tmp)
        # 13. Same-text Yanayana reply posted BEFORE the target is not
        # the resend: it must not count as already-sent, so the resend
        # still goes out. (Time-order check adopted 2026-11-14 from
        # Yama's audit share, their指摘2: msg.ts > event.ts.)
        record(tmp, target="222.33")
        sent = []
        s = ra.reconcile_pending_resends(
            thread_reader=thread_with(
                {"ts": "111.5", "user": YANAYANA, "text": "hello"}),
            sender=lambda c, t, m: sent.append(m) or "999.9",
            now=now)
        check("pre-target same-text reply is not already-sent",
              s["sent"] == 1 and len(sent) == 1, str(s))
        check("resend still sent when only a pre-target reply exists",
              status_of(tmp, "222.33") == "sent")

        clear(tmp)
        # 14. Already-sent check precedes answered_by_other (adopted
        # 2026-11-14 from Neiro's review of the published code): the
        # original reply landed but its response was lost, and someone
        # else answered in the meantime. The delivered reply must be
        # recorded in replied.jsonl, not given up as answered_by_other.
        record(tmp, target="222.34")
        sent = []
        s = ra.reconcile_pending_resends(
            thread_reader=thread_with(
                {"ts": "333.3", "user": OTHER, "text": "I can answer that"},
                {"ts": "444.4", "user": YANAYANA, "text": "hello"}),
            sender=lambda c, t, m: sent.append(m) or "999.9",
            now=now)
        check("delivered reply + other answer is recorded, not given up",
              s["sent"] == 1 and s["given_up"] == 0 and sent == [], str(s))
        replied = read_jsonl(os.path.join(tmp, "events", "replied.jsonl"))
        check("delivered reply recorded recovered despite other answer",
              any(r.get("target_ts") == "222.34" and r.get("recovered") is True
                  and r.get("reply_ts") == "444.4" for r in replied),
              str(replied))
        check("entry marked sent, not given up",
              status_of(tmp, "222.34") == "sent")

        clear(tmp)
        # 15. Two pending resends with identical text share one reply
        # in the thread: the first recovers it, the second must not
        # "recover" the same ts -- it resends instead. (Yama's
        # assigned_reply_ts, adopted 2026-11-14.)
        record(tmp, target="222.35")
        record(tmp, target="222.36")
        sent = []
        s = ra.reconcile_pending_resends(
            thread_reader=thread_with(
                {"ts": "444.4", "user": YANAYANA, "text": "hello"}),
            sender=lambda c, t, m: sent.append(m) or "999.9",
            now=now)
        check("one shared reply recovers only one resend",
              s["sent"] == 2 and len(sent) == 1, str(s))
        replied = read_jsonl(os.path.join(tmp, "events", "replied.jsonl"))
        check("first target recovered with the shared ts",
              any(r.get("target_ts") == "222.35" and r.get("recovered") is True
                  and r.get("reply_ts") == "444.4" for r in replied),
              str(replied))
        check("second target resent, not falsely recovered",
              any(r.get("target_ts") == "222.36" and r.get("recovered") is not True
                  and r.get("reply_ts") == "999.9" for r in replied),
              str(replied))

        # 15b. A reply ts consumed in an EARLIER sweep must not be
        # "recovered" again for a different pending entry (Neiro's
        # share 2026-11-14: already-resolved found_ts counts as used).
        # The 444.4 reply already recovered 222.35 above and sits in
        # replied.jsonl; a new pending entry with the same text must
        # resend with a fresh ts, not claim the old one.
        record(tmp, target="222.37")
        sent = []
        s = ra.reconcile_pending_resends(
            thread_reader=thread_with(
                {"ts": "444.4", "user": YANAYANA, "text": "hello"}),
            sender=lambda c, t, m: sent.append(m) or "999.8",
            now=now)
        check("reply ts from earlier sweep is not re-recovered",
              s["sent"] == 1 and len(sent) == 1, str(s))
        replied = read_jsonl(os.path.join(tmp, "events", "replied.jsonl"))
        check("new entry resent with a fresh ts, not the old one",
              any(r.get("target_ts") == "222.37" and r.get("recovered") is not True
                  and r.get("reply_ts") == "999.8" for r in replied),
              str(replied))

        clear(tmp)
        # 17. A bot's long post does NOT count as answered (narrowed
        # 2026-11-14 from Yama's resend share): another bot's
        # auto-reply must not falsely silence a delayed resend.
        record(tmp, target="222.38")
        sent = []
        s = ra.reconcile_pending_resends(
            thread_reader=thread_with(
                {"ts": "333.3", "user": OTHER, "bot_id": "B0EXAMPLEOTHER",
                 "text": "I can answer that in detail"}),
            sender=lambda c, t, m: sent.append(m) or "999.9",
            now=now)
        check("bot post does not count as answered",
              s["sent"] == 1 and len(sent) == 1, str(s))

        clear(tmp)
        # 18. A short human post (10 chars or fewer) does NOT count as
        # answered: a bare acknowledgement is not an answer.
        record(tmp, target="222.39")
        sent = []
        s = ra.reconcile_pending_resends(
            thread_reader=thread_with(
                {"ts": "333.3", "user": OTHER, "text": "ありがとうございます"}),
            sender=lambda c, t, m: sent.append(m) or "999.9",
            now=now)
        check("short post (10 chars) does not count as answered",
              s["sent"] == 1 and len(sent) == 1, str(s))

        clear(tmp)
        # 19. A long human post still counts as answered.
        record(tmp, target="222.40")
        s = ra.reconcile_pending_resends(
            thread_reader=thread_with(
                {"ts": "333.3", "user": OTHER,
                 "text": "こちらで回答できますよ"}),
            sender=lambda c, t, m: (_ for _ in ()).throw(AssertionError("must not send")),
            now=now)
        check("long human post still counts as answered",
              s["given_up"] == 1, str(s))
        check("long-human given-up reason recorded",
              read_jsonl(pending_path(tmp))[-1].get("reason") == "answered_by_other")

        clear(tmp)
        # 20. A resend already sent with the interim "最初の試行"
        # wording (used briefly on 2026-11-14 before Yama's revert to
        # "元の失敗時刻") is still recognised as delivered: recorded
        # recovered, never reposted.
        record(tmp, target="222.41")
        sent = []
        s = ra.reconcile_pending_resends(
            thread_reader=thread_with(
                {"ts": "444.4", "user": YANAYANA,
                 "text": "遅れての再送（最初の試行：2026-11-13 23:00 JST）\nhello"}),
            sender=lambda c, t, m: sent.append(m) or "999.9",
            now=now)
        check("interim-wording resend in thread is not reposted",
              s["sent"] == 1 and sent == [], str(s))
        replied = read_jsonl(os.path.join(tmp, "events", "replied.jsonl"))
        check("interim-wording resend recorded recovered",
              any(r.get("target_ts") == "222.41" and r.get("recovered") is True
                  and r.get("reply_ts") == "444.4" for r in replied),
              str(replied))
        check("interim-wording entry marked sent",
              status_of(tmp, "222.41") == "sent")

        clear(tmp)
        # 21. Stamp guard for the already-sent check (Neiro's share
        # 2026-11-15, neiro-slack-system commit 2af573d): an entry
        # recorded with an event_id whose thread holds a same-text
        # Yanayana reply stamped for ANOTHER event must not count
        # that reply as delivered -- it is the other event's answer.
        # The text-only thread read still sees it; the metadata read
        # (fake) drops it, so the resend still goes out.
        record(tmp, target="222.42", event_id="EvAAA")
        sent = []
        s = ra.reconcile_pending_resends(
            thread_reader=thread_with(
                {"ts": "444.4", "user": YANAYANA, "text": "hello"}),
            metadata_reader=lambda c, t, e: [],
            sender=lambda c, t, m: sent.append(m) or "999.9",
            now=now)
        check("different-event same-text reply is not already-sent",
              s["sent"] == 1 and len(sent) == 1, str(s))
        check("entry with event_id stores it",
              any(e.get("event_id") == "EvAAA"
                  for e in read_jsonl(pending_path(tmp))),
              str(read_jsonl(pending_path(tmp))))

        clear(tmp)
        # 22. Same-event reply found through the metadata read is
        # recorded recovered, never reposted.
        record(tmp, target="222.43", event_id="EvAAA")
        sent = []
        s = ra.reconcile_pending_resends(
            thread_reader=thread_with(
                {"ts": "444.4", "user": YANAYANA, "text": "hello"}),
            metadata_reader=lambda c, t, e: [{"ts": "444.4", "text": "hello"}],
            sender=lambda c, t, m: sent.append(m) or "999.9",
            now=now)
        check("same-event reply via metadata read is not reposted",
              s["sent"] == 1 and sent == [], str(s))
        replied = read_jsonl(os.path.join(tmp, "events", "replied.jsonl"))
        check("same-event reply recorded recovered",
              any(r.get("target_ts") == "222.43" and r.get("recovered") is True
                  and r.get("reply_ts") == "444.4" for r in replied),
              str(replied))

        clear(tmp)
        # 23. Metadata read failure leaves the entry pending (no
        # metadata-blind fallback, same stance as the ambiguous sweep).
        record(tmp, target="222.44", event_id="EvAAA")

        def meta_boom(c, t, e):
            raise RuntimeError("metadata down")
        s = ra.reconcile_pending_resends(
            thread_reader=thread_with(
                {"ts": "444.4", "user": YANAYANA, "text": "hello"}),
            metadata_reader=meta_boom,
            sender=lambda c, t, m: (_ for _ in ()).throw(AssertionError("must not send")),
            now=now)
        check("metadata read failure leaves entry pending",
              s["skipped_read_failed"] == 1 and s["still_pending"] == 1, str(s))
        check("entry still pending after metadata read failure",
              status_of(tmp, "222.44") == "pending")

        clear(tmp)
        # 16. main() runs the pending sweep under the same hourly gate.
        # The failure time must be recent in wall-clock terms: main()
        # uses real time (unlike the pinned `now` above), and the 24h
        # resend window is measured from it -- the fixed 2026-11-13
        # default expired a day after it was written (2026-11-15).
        recent = datetime.fromtimestamp(
            datetime.now(timezone.utc).timestamp() - 3600,
            timezone.utc).isoformat()
        record(tmp, target="222.32", failed_at=recent)
        ra.ro.thread_yanayana_replies = lambda c, t: []
        # Stub the live reader/sender used by main()'s defaults.
        orig_reader = ra.thread_all_messages
        orig_send = ra.ro.send
        ra.thread_all_messages = thread_with()
        ra.ro.send = lambda c, t, m: "999.9"
        try:
            rc = ra.main(["--force"])
        finally:
            ra.thread_all_messages = orig_reader
            ra.ro.send = orig_send
        check("main --force succeeds", rc == 0)
        check("main ran the pending sweep",
              status_of(tmp, "222.32") == "sent", status_of(tmp, "222.32"))

    print(f"\n{CHECKS[0]} passed, {CHECKS[1]} failed")
    return 1 if CHECKS[1] else 0


if __name__ == "__main__":
    sys.exit(main())
