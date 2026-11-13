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
QUESTIONER = "U0QUESTIONER"
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
           kind="history_unavailable", failed_at="2026-11-13T14:00:00+00:00"):
    return ra.record_pending_resend("C0TEST", "111.1", target, message,
                                    questioner, kind, failed_at)


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
        # 15. main() runs the pending sweep under the same hourly gate.
        record(tmp, target="222.32")
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
