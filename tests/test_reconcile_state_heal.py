#!/usr/bin/env python3
"""Tests for reconcile_ambiguous.py reconcile_state_heal.

The state-heal sweep (2026-11-23, the bookkeeping seam) closes the
two shapes the atomic send path cannot:
  1. A reply that landed with our event_id metadata stamp but was
     never recorded in any ledger (the sender process died between
     the Slack POST and the ledger write). The sweep finds the
     stamped reply in the thread, records it (recovered) into
     replied.jsonl, and never reposts.
  2. A reply recorded in a ledger whose msgstate entry never
     reached done -- healed by msgstate's ledger heal in the same
     sweep.

Candidates are pending/failed inbox items only: processing entries
belong to a live worker, items younger than STATE_HEAL_MIN_AGE_SECS
may still be mid-finalization, and items already in a ledger skip
the thread read entirely.

Plain stdlib script, no pytest. Run:
  python3 tests/test_reconcile_state_heal.py
Exit 0 when every check passes; each check prints PASS/FAIL.

The metadata reader is stubbed and all files live in a temp dir, so
no real Slack call is made and the real events/ directory is never
touched.
"""
import importlib.util
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
SPEC = importlib.util.spec_from_file_location(
    "reconcile_ambiguous", os.path.join(ROOT, "reconcile_ambiguous.py"))
ra = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ra)

CHECKS = [0, 0]  # [passed, failed]


def check(name, cond, detail=""):
    CHECKS[0 if cond else 1] += 1
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not cond else ""))
    return cond


CH = "C0EXAMPLE001"
OLD = "2026-11-23T00:00:00+00:00"       # > 600s before NOW
NOW = datetime(2026, 11, 23, 1, 0, tzinfo=timezone.utc)


def write_jsonl(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def read_jsonl(path):
    try:
        with open(path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        return []


def setup(tmp):
    events = os.path.join(tmp, "events")
    os.makedirs(events, exist_ok=True)
    for mod, names in ((ra, ("BASE", "EVENTS")),
                       (ra.ro, ("BASE", "EVENTS", "REPLIED", "AMBIGUOUS")),
                       (ra.ms, ("BASE", "EVENTS", "INBOX", "STATE", "DEAD",
                                "LOCK", "STATS", "REPLIES_LEDGER",
                                "REPLIED_LEDGER", "HEAL_LOG"))):
        for name in names:
            if name == "BASE":
                setattr(mod, name, tmp)
            elif name == "EVENTS":
                setattr(mod, name, events)
            else:
                setattr(mod, name, os.path.join(events, {
                    "INBOX": "inbox.jsonl", "STATE": "message_state.json",
                    "DEAD": "dead_letter.jsonl", "LOCK": "message_state.lock",
                    "STATS": "inbox_stats.json",
                    "REPLIES_LEDGER": "replies.jsonl",
                    "REPLIED_LEDGER": "replied.jsonl",
                    "HEAL_LOG": "state_heal.jsonl",
                    "REPLIED": "replied.jsonl",
                    "AMBIGUOUS": "ambiguous_posts.jsonl",
                }[name]))
    return events


def msg(event_id, ts, received=OLD):
    return {"event_id": event_id, "channel": CH, "ts": ts,
            "thread_ts": "1000.000000", "user": "U0OTHER",
            "text": f"text for {ts}", "received_at": received}


def main():
    tmp = tempfile.mkdtemp(prefix="reconcile-heal-test-")
    events = setup(tmp)

    # G: pending, no ledger row, stamped reply exists in the thread
    # H: failed, recorded in replied.jsonl (ledger heal only)
    # I: processing, stamped reply exists -> not a candidate
    # J: pending but too young -> not a candidate yet
    # K: pending, thread read fails -> skipped, untouched
    inbox = [msg("EvG", "2007.000007"), msg("EvH", "2008.000008"),
             msg("EvI", "2009.000009"),
             msg("EvJ", "2010.000010", received="2026-11-23T00:59:30+00:00"),
             msg("EvK", "2011.000011")]
    write_jsonl(os.path.join(events, "inbox.jsonl"), inbox)
    state = {"EvG": {"status": "pending", "attempts": 1},
             "EvH": {"status": "failed", "attempts": 2},
             "EvI": {"status": "processing", "attempts": 1},
             "EvJ": {"status": "pending", "attempts": 0},
             "EvK": {"status": "pending", "attempts": 1}}
    with open(os.path.join(events, "message_state.json"), "w",
              encoding="utf-8") as f:
        json.dump(state, f)
    write_jsonl(os.path.join(events, "replied.jsonl"), [
        {"channel": CH, "target_ts": "2008.000008", "reply_ts": "3008.1",
         "thread_ts": "1000.000000"}])
    write_jsonl(os.path.join(events, "replies.jsonl"), [])

    def fake_reader(channel, thread_ts, event_id):
        if event_id == "EvG":
            return [{"ts": "3007.1", "text": "answer for G"}]
        if event_id == "EvK":
            raise RuntimeError("read failed")
        return []

    summary = ra.reconcile_state_heal(metadata_reader=fake_reader, now=NOW)
    check("one stamped reply recovered", summary["stamped_recovered"] == 1,
          f"got {summary['stamped_recovered']}")
    check("checked counts G and K only", summary["checked"] == 2,
          f"got {summary['checked']}")
    check("read failure skipped once", summary["skipped_read_failed"] == 1)
    check("healed counts G and H", summary["healed"] == 2,
          f"got {summary['healed']}")

    with open(os.path.join(events, "message_state.json"), encoding="utf-8") as f:
        st = json.load(f)
    check("G done after stamped recovery", st["EvG"]["status"] == "done")
    check("H done after ledger heal", st["EvH"]["status"] == "done")
    check("I still processing", st["EvI"]["status"] == "processing")
    check("J still pending (too young)", st["EvJ"]["status"] == "pending")
    check("K still pending (read failed)", st["EvK"]["status"] == "pending")

    replied = read_jsonl(os.path.join(events, "replied.jsonl"))
    g = [r for r in replied if r.get("target_ts") == "2007.000007"]
    check("G recorded recovered in replied.jsonl",
          len(g) == 1 and g[0].get("recovered") is True
          and g[0].get("reply_ts") == "3007.1",
          f"got {g}")

    # Second sweep: G and H are done, so only K is checked again and
    # nothing new is recorded.
    summary2 = ra.reconcile_state_heal(metadata_reader=fake_reader, now=NOW)
    check("second sweep recovers nothing new",
          summary2["stamped_recovered"] == 0 and summary2["healed"] == 0,
          f"got {summary2}")
    check("second sweep checks only K", summary2["checked"] == 1,
          f"got {summary2['checked']}")

    print(f"\n{CHECKS[0]} passed, {CHECKS[1]} failed")
    return 1 if CHECKS[1] else 0


if __name__ == "__main__":
    sys.exit(main())
