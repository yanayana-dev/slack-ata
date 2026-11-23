#!/usr/bin/env python3
"""Tests for msgstate.py heal-replied (the bookkeeping seam heal).

The seam (observed 2026-11-18, 2026-11-22, 2026-11-23): a reply was
sent and at least one reply ledger recorded it, but the worker run
ended before the mark-done write, so the answered message stayed
pending/failed and was re-processed on later wakes. The heal marks
done every inbox message either reply ledger (replies.jsonl /
replied.jsonl) proves answered -- except entries currently being
processed (a live worker finalizes those itself) and dead entries
(terminal, never resurrected).

Plain stdlib script, no pytest. Run:
  python3 tests/test_msgstate_heal.py
Exit 0 when every check passes; each check prints PASS/FAIL.

All state lives in a temp dir via SLACK_LISTENER_BASE, so the real
events/ directory is never touched.
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MSGSTATE = os.path.join(ROOT, "msgstate.py")

CHECKS = [0, 0]  # [passed, failed]


def check(name, cond, detail=""):
    CHECKS[0 if cond else 1] += 1
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not cond else ""))
    return cond


def run(args, base):
    env = dict(os.environ, SLACK_LISTENER_BASE=base)
    return subprocess.run([sys.executable, MSGSTATE] + args,
                          capture_output=True, text=True, env=env, timeout=30)


def write_jsonl(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def read_state(base):
    with open(os.path.join(base, "events", "message_state.json"),
              encoding="utf-8") as f:
        return json.load(f)


def read_jsonl(path):
    try:
        with open(path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        return []


CH = "C0EXAMPLE001"


def msg(event_id, ts):
    return {"event_id": event_id, "channel": CH, "ts": ts,
            "thread_ts": "1000.000000", "user": "U0OTHER",
            "text": f"text for {ts}",
            "received_at": "2026-11-23T00:00:00+00:00"}


def main():
    tmp = tempfile.mkdtemp(prefix="msgstate-heal-test-")
    events = os.path.join(tmp, "events")
    os.makedirs(events)

    # A: pending, recorded in replies.jsonl (replylog ledger)
    # B: failed, recorded in replied.jsonl (reply_once ledger)
    # C: processing, recorded in replies.jsonl -> must NOT heal
    # D: pending, no ledger record -> must NOT heal
    # E: no state entry at all, recorded in replies.jsonl -> heal creates done
    # F: dead, recorded in replies.jsonl -> stays dead
    inbox = [msg("EvA", "2001.000001"), msg("EvB", "2002.000002"),
             msg("EvC", "2003.000003"), msg("EvD", "2004.000004"),
             msg("EvE", "2005.000005"), msg("EvF", "2006.000006")]
    write_jsonl(os.path.join(events, "inbox.jsonl"), inbox)
    state = {
        "EvA": {"status": "pending", "attempts": 1},
        "EvB": {"status": "failed", "attempts": 2, "last_error": "boom"},
        "EvC": {"status": "processing", "attempts": 1},
        "EvD": {"status": "pending", "attempts": 0},
        "EvF": {"status": "dead", "attempts": 4},
    }
    with open(os.path.join(events, "message_state.json"), "w",
              encoding="utf-8") as f:
        json.dump(state, f)
    write_jsonl(os.path.join(events, "replies.jsonl"), [
        {"channel": CH, "target_ts": "2001.000001", "sent_ts": "3001.1",
         "thread_ts": "1000.000000"},
        {"channel": CH, "target_ts": "2003.000003", "sent_ts": "3003.1",
         "thread_ts": "1000.000000"},
        {"channel": CH, "target_ts": "2005.000005", "sent_ts": "3005.1",
         "thread_ts": "1000.000000"},
        {"channel": CH, "target_ts": "2006.000006", "sent_ts": "3006.1",
         "thread_ts": "1000.000000"},
    ])
    write_jsonl(os.path.join(events, "replied.jsonl"), [
        {"channel": CH, "target_ts": "2002.000002", "reply_ts": "3002.1",
         "thread_ts": "1000.000000"},
    ])

    # Dry run reports A, B, E but changes nothing.
    r = run(["heal-replied", "--dry-run"], tmp)
    check("dry-run exits 0", r.returncode == 0, r.stderr[:200])
    out = json.loads(r.stdout)
    healed_keys = {h["key"] for h in out["healed"]}
    check("dry-run finds exactly A, B, E",
          healed_keys == {"EvA", "EvB", "EvE"}, f"got {healed_keys}")
    st = read_state(tmp)
    check("dry-run leaves state untouched",
          st["EvA"]["status"] == "pending" and "EvE" not in st)

    # Real run heals A, B, E and logs them.
    r = run(["heal-replied"], tmp)
    check("heal exits 0", r.returncode == 0, r.stderr[:200])
    out = json.loads(r.stdout)
    check("heal count is 3", out["count"] == 3, f"got {out['count']}")
    st = read_state(tmp)
    check("A healed to done", st["EvA"]["status"] == "done")
    check("A carries the healed stamp",
          st["EvA"].get("healed", {}).get("source") == "reply-ledger")
    check("B healed to done, error cleared",
          st["EvB"]["status"] == "done" and "last_error" not in st["EvB"])
    check("C still processing (live claim untouched)",
          st["EvC"]["status"] == "processing")
    check("D still pending (no ledger proof)", st["EvD"]["status"] == "pending")
    check("E created as done", st.get("EvE", {}).get("status") == "done")
    check("F stays dead", st["EvF"]["status"] == "dead")
    log = read_jsonl(os.path.join(events, "state_heal.jsonl"))
    check("heal log has 3 lines", len(log) == 3, f"got {len(log)}")

    # Idempotent: a second run heals nothing new.
    r = run(["heal-replied"], tmp)
    out = json.loads(r.stdout)
    check("second heal is a no-op", out["count"] == 0, f"got {out['count']}")

    # pending no longer wakes the healed messages; only D remains
    # (C is processing, so it does not wake either).
    r = run(["pending", "--all"], tmp)
    items = json.loads(r.stdout)
    keys = {it["key"] for it in items}
    check("pending after heal is only D", keys == {"EvD"}, f"got {keys}")

    # Done is final: a late failed/processing mark from a stale
    # worker must not resurrect a completed message (Yama's
    # commit-guard hole, their candidate 19).
    r = run(["mark", "EvD", "done"], tmp)
    check("mark D done", r.returncode == 0 and r.stdout.strip() == "done",
          f"rc={r.returncode} out={r.stdout.strip()}")
    r = run(["mark", "EvD", "failed", "--error", "late failure"], tmp)
    check("late failed mark on done is refused",
          r.returncode == 0 and r.stdout.strip() == "done",
          f"rc={r.returncode} out={r.stdout.strip()}")
    r = run(["mark", "EvD", "processing"], tmp)
    check("late processing mark on done is refused",
          r.returncode == 0 and r.stdout.strip() == "done",
          f"rc={r.returncode} out={r.stdout.strip()}")
    st = read_state(tmp)
    check("D stays done with attempts untouched",
          st["EvD"]["status"] == "done" and st["EvD"]["attempts"] == 0,
          f"state={st['EvD']}")

    print(f"\n{CHECKS[0]} passed, {CHECKS[1]} failed")
    return 1 if CHECKS[1] else 0


if __name__ == "__main__":
    sys.exit(main())
