#!/usr/bin/env python3
"""Per-message processing state for the Slack watcher.

Replaces line-count offsets (agy review point 1 / Neiro's state model):
each inbox message has a state keyed by event_id (fallback channel:ts):
  (absent)     -> pending
  processing   -> a worker started handling it (attempts incremented)
  done         -> handled successfully (including "no action needed")
  failed       -> last attempt failed; retried on later wakes
  dead         -> attempts exceeded MAX_ATTEMPTS; moved to dead letter

Stale recovery (adopted 2026-11-12 from Neiro's slack-poll.py design,
shared in #slack-へようこそ): every poll (cmd_pending / cmd_count)
sweeps entries stuck in "processing" whose updated_at is older than
TIMEOUT_SECS -- attempts < MAX_ATTEMPTS go back to pending for a
re-wake, attempts >= MAX_ATTEMPTS go to dead + dead letter. No process
is killed; like Neiro's, this is "re-execute the stuck work", which is
safe only because replies are idempotent (reply_once.py checks the
thread before sending, so a re-run never double-posts).

Usage:
  msgstate.py pending [--limit N | --all]
                             # JSON list of pending/failed messages,
                             # oldest first, capped at
                             # DEFAULT_PENDING_LIMIT unless --limit N
                             # or --all is given (see below)
  msgstate.py touch KEY      # refresh a processing entry's
                             # updated_at (claim heartbeat; attempts
                             # unchanged) so long single-item work is
                             # not swept as stale mid-flight
  msgstate.py count-pending  # just the number
  msgstate.py oldest-pending-age  # seconds since the oldest pending
                                  # message arrived (empty when none)
  msgstate.py stats          # skipped-row counts for the inbox file
  msgstate.py mark KEY STATUS [--error TEXT]
  msgstate.py key-of         # read one inbox JSON line on stdin, print its key
  msgstate.py reconcile-done [--dry-run]  # clear stale last_error from done entries

State: events/message_state.json ; dead letters: events/dead_letter.jsonl

Concurrency (adopted 2026-11-12 from Neiro's + Yama's replies to
Yanayana's question about double-claiming the same message):
state read-modify-write is serialized with flock on
events/message_state.lock -- LOCK_EX for mark, LOCK_SH for reads.
This is Neiro's "same lock file for state updates" and Yama's layer 1
(flock) + layer 2 (atomic claim) in one: while the lock is held, the
pending->processing claim and the save are atomic, so two workers can
never grab the same event. tmp+rename in save_state (already present)
keeps the file itself never torn. Single VM, so flock suffices.
"""
import fcntl
import json
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timezone

import inbox_state

BASE = os.environ.get("SLACK_LISTENER_BASE") or os.path.dirname(os.path.abspath(__file__))
EVENTS = os.path.join(BASE, "events")
INBOX = os.path.join(EVENTS, "inbox.jsonl")
STATE = os.path.join(EVENTS, "message_state.json")
DEAD = os.path.join(EVENTS, "dead_letter.jsonl")
LOCK = os.path.join(EVENTS, "message_state.lock")
# Skipped-row counts for the inbox file (adopted 2026-11-12 from Neiro's
# suggestion in #slack-へようこそ): read_inbox() skips blank and
# malformed rows silently, but the *count* of skipped rows is kept here
# so a writer bug producing bad rows repeatedly can be noticed later.
# Row contents are never stored -- counts only.
STATS = os.path.join(EVENTS, "inbox_stats.json")
MAX_ATTEMPTS = 3
# Stale-processing timeout, mirroring Neiro's TIMEOUT_SECS = 600:
# a worker that claimed a message but never finished within 10 minutes
# is presumed crashed; the poller reclaims it on the next poll.
TIMEOUT_SECS = 600
# Per-wake batch cap (burst fix, 2026-11-21): `pending` hands a worker
# at most this many messages, in priority order (see _priority_tier).
# The uncapped list made one wake try to chew a whole burst of long
# posts in a single agent run; when that run died or was cut off, its
# claims sat in processing until the 600s sweep, burned attempts, and
# eventually dead-lettered (the 2026-11-12 / 2026-11-18 dead letters
# all have this shape). Leftovers stay pending and re-wake on the next
# 30s poll, so a burst drains over several bounded runs instead of one
# monster run. count-pending / oldest-pending-age always see the full
# backlog.
DEFAULT_PENDING_LIMIT = 3

# Priority tiers for the per-wake batch (Neiro's burst fix, shared
# 2026-11-21 in #slack-へようこそ): a burst must not starve the
# messages that need a fast reply -- DM first, then messages addressed
# to Yanayana or to あた, then everything else. Oldest first inside
# each tier (stable sort over inbox order), so no tier starves
# forever; the watchdog's oldest-pending-age still watches the
# backlog as a whole. IDs are scrubbed to placeholders on publish
# (see scrub.sed). Down-tier starvation under a sustained flood
# (aging) is a known open point, same as on Neiro's side.
DM_CHANNEL_TYPES = ("im", "mpim")
YANAYANA_USER_ID = "U0EXAMPLEBOT"
ATA_USER_ID = "U0EXAMPLEOWNER"


def _priority_tier(rec):
    """0 = DM, 1 = addressed to Yanayana/あた, 2 = everything else."""
    if rec.get("channel_type") in DM_CHANNEL_TYPES:
        return 0
    text = rec.get("text") or ""
    lowered = text.lower()
    if ("やなやな" in lowered or "yanayana" in lowered
            or f"<@{YANAYANA_USER_ID}>" in text
            or f"<@{ATA_USER_ID}>" in text):
        return 1
    return 2


@contextmanager
def state_lock(exclusive):
    """Hold flock on the state lock file for one read or read-modify-write.

    flock is released automatically if the holder dies, so a crashed
    worker can never leave a stale lock behind. Blocking wait: critical
    sections are milliseconds, and a worker would rather wait briefly
    than skip or double-claim a message.
    """
    os.makedirs(EVENTS, exist_ok=True)
    fd = os.open(LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def key_of(rec):
    if rec.get("event_id"):
        return rec["event_id"]
    return f"ts:{rec.get('channel')}:{rec.get('ts')}"


def load_state():
    try:
        with open(STATE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    os.makedirs(EVENTS, exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE)


def _write_inbox_stats(total, skipped_blank, skipped_malformed):
    """Persist snapshot counts of skipped inbox rows (counts only).

    Snapshot, not cumulative: read_inbox() re-reads the whole file on
    every poll, so accumulating would double-count the same rows.
    The counts describe the rows currently in the file -- if a writer
    bug starts producing malformed rows, the number grows with the
    file, which is the trail Neiro asked for. tmp+rename keeps the
    file never torn. Every read_inbox() call site runs under the
    exclusive state lock, so concurrent writes cannot interleave.
    """
    os.makedirs(EVENTS, exist_ok=True)
    stats = {
        "total_lines": total,
        "skipped_blank": skipped_blank,
        "skipped_malformed": skipped_malformed,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    tmp = STATS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATS)


def read_inbox():
    # Inbox .bak (Neiro's 2026-11-18 share, inbox_state.py): a missing
    # or wholly-unparseable body is restored from the backup before
    # anything reads it, and the backup itself is refreshed (throttled
    # to once a day from this poll path; backfill refreshes it on every
    # run) whenever the body parses fully. Both are no-ops on a healthy
    # file -- restore only acts on a missing/wholly-bad body, snapshot
    # only when the .bak is stale.
    inbox_state.restore_inbox(INBOX)
    inbox_state.snapshot_inbox(INBOX)
    if not os.path.exists(INBOX):
        return []
    out = []
    skipped_blank = 0
    skipped_malformed = 0
    total = 0
    with open(INBOX, "r", encoding="utf-8") as f:
        for line in f:
            total += 1
            line = line.strip()
            if not line:
                skipped_blank += 1
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                skipped_malformed += 1
                continue
    _write_inbox_stats(total, skipped_blank, skipped_malformed)
    return out


def pending_items(state=None, limit=None):
    if state is None:
        state = load_state()
    # Group duplicate inbox lines for the same Slack message. A message
    # can land in the inbox twice under different keys (e.g. a backfill
    # copy with event_id null and a later live redelivery with an
    # event_id -- observed 2026-11-12). The message is the unit of
    # work, not the delivery: if any copy is done/dead the message is
    # handled; otherwise it is presented once, under the copy that is
    # furthest along.
    groups = {}
    order = []
    for rec in read_inbox():
        ident = (rec.get("channel"), rec.get("ts")) if rec.get("ts") else (None, key_of(rec))
        if ident not in groups:
            groups[ident] = []
            order.append(ident)
        groups[ident].append(rec)
    items = []
    for ident in order:
        recs = groups[ident]
        sts = [(key_of(r), state.get(key_of(r)) or {}) for r in recs]
        if any(st.get("status") in ("done", "dead") for _, st in sts):
            continue
        key, st = next(
            ((k, s) for k, s in sts if s.get("status") in ("processing", "failed")),
            sts[0])
        if st.get("status") == "processing":
            # In-flight work does not wake: a worker has claimed this
            # message and is handling it. If that worker dies, the
            # stale sweep (600s) moves it back to pending and it wakes
            # again. Listing it here would re-wake a second worker
            # while the first is still working (double-claim).
            continue
        rec = next(r for r in recs if key_of(r) == key)
        items.append({
            "key": key,
            "status": st.get("status", "pending"),
            "attempts": max(s.get("attempts", 0) for _, s in sts),
            "message": rec,
        })
    # Priority order (stable: oldest first inside each tier). The cap
    # below keeps the first N of that order; the deferred rest re-wake
    # on the next poll. --all also returns priority order so every
    # consumer sees the same sequence.
    items.sort(key=lambda it: _priority_tier(it["message"]))
    if limit is not None:
        items = items[:limit]
    return items


def _parse_ts(s):
    """Parse an ISO timestamp; naive values are read as UTC.

    Naive input used to crash the stale sweep with TypeError
    (aware `now` minus naive `updated_at`) -- the same hole Neiro's
    agy review found in their received_at handling (2026-11-21):
    an external/legacy timestamp without a timezone must be read
    as UTC, not left naive. All timestamps we write are aware UTC,
    so this only matters for legacy or foreign input.
    """
    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def sweep_stale_locked(state, now=None):
    """Reclaim processing entries stuck longer than TIMEOUT_SECS.

    Must be called holding the exclusive state lock. Returns True if
    any entry changed. Attempts < MAX_ATTEMPTS -> back to pending
    (the next poll re-wakes a worker); attempts >= MAX_ATTEMPTS ->
    dead + dead-letter line, mirroring Neiro's "3回を超えたら dead".
    """
    if now is None:
        now = datetime.now(timezone.utc)
    changed = False
    for key, st in list(state.items()):
        if st.get("status") != "processing":
            continue
        updated = _parse_ts(st.get("updated_at") or "")
        if updated is None:
            continue
        age = (now - updated).total_seconds()
        if age < TIMEOUT_SECS:
            continue
        at = now.isoformat()
        if st.get("attempts", 0) >= MAX_ATTEMPTS:
            rec = next((r for r in read_inbox() if key_of(r) == key), {"key": key})
            os.makedirs(EVENTS, exist_ok=True)
            with open(DEAD, "a", encoding="utf-8") as f:
                f.write(json.dumps(
                    {"key": key, "at": at, "attempts": st.get("attempts"),
                     "last_error": "stale processing reclaimed after "
                     f"{int(age)}s (timeout {TIMEOUT_SECS}s)",
                     "message": rec}, ensure_ascii=False) + "\n")
            st["status"] = "dead"
        else:
            st["status"] = "pending"
        st["updated_at"] = at
        state[key] = st
        changed = True
    if changed:
        save_state(state)
    return changed


def cmd_stats():
    try:
        with open(STATS, "r", encoding="utf-8") as f:
            print(json.dumps(json.load(f), ensure_ascii=False, indent=1))
    except Exception:
        print(json.dumps({"total_lines": 0, "skipped_blank": 0,
                          "skipped_malformed": 0, "updated_at": None},
                         ensure_ascii=False, indent=1))


def cmd_pending(argv):
    limit = DEFAULT_PENDING_LIMIT
    if "--all" in argv:
        limit = None
    elif "--limit" in argv:
        i = argv.index("--limit")
        try:
            limit = int(argv[i + 1])
            if limit < 1:
                raise ValueError
        except (IndexError, ValueError):
            print("usage: pending [--limit N | --all]", file=sys.stderr)
            return 2
    with state_lock(exclusive=True):
        state = load_state()
        sweep_stale_locked(state)
        items = pending_items(state, limit=limit)
    print(json.dumps(items, ensure_ascii=False, indent=1))
    return 0


def cmd_touch(argv):
    """Refresh a processing entry's updated_at (claim heartbeat).

    A worker on a legitimately long single item (implementing a
    shared fix, running tests) calls this between steps so the 600s
    stale sweep does not reclaim the claim mid-flight and hand the
    same message to a second worker. Attempts are NOT incremented --
    this is not a new try, just proof of life. Anything that is not
    currently processing (pending/done/dead/absent) is left exactly
    as it is and its status is printed.
    """
    if len(argv) < 1:
        print("usage: touch KEY", file=sys.stderr)
        return 2
    key = argv[0]
    with state_lock(exclusive=True):
        state = load_state()
        st = state.get(key)
        if not st or st.get("status") != "processing":
            print(st.get("status") if st else "absent")
            return 0
        st["updated_at"] = datetime.now(timezone.utc).isoformat()
        state[key] = st
        save_state(state)
    print("touched")
    return 0


def cmd_count():
    with state_lock(exclusive=True):
        state = load_state()
        sweep_stale_locked(state)
        n = len(pending_items(state))
    print(n)


def oldest_pending_age(items=None, now=None):
    """Age in seconds of the oldest pending/failed item, or None.

    The watchdog's stall warning (agy review 2026-11-18, thread
    1795006461.907139) needs this: when the hook itself has stopped,
    unprocessed messages pile up, and only a timer-driven check can
    notice. The age is measured from received_at -- how long the
    message has waited for processing. Items currently being
    processed are excluded by pending_items(), so in-flight work
    never trips the warning; a worker that died mid-processing is
    reclaimed by the stale sweep (600s) before the age is read.
    """
    if items is None:
        items = pending_items()
    if not items:
        return None
    if now is None:
        now = datetime.now(timezone.utc)
    oldest = None
    for item in items:
        received = _parse_ts(item.get("message", {}).get("received_at") or "")
        if received is None:
            continue
        if received.tzinfo is None:
            received = received.replace(tzinfo=timezone.utc)
        if oldest is None or received < oldest:
            oldest = received
    if oldest is None:
        return None
    return max(0, int((now - oldest).total_seconds()))


def cmd_oldest_pending_age():
    with state_lock(exclusive=True):
        state = load_state()
        sweep_stale_locked(state)
        age = oldest_pending_age(pending_items(state))
    print(age if age is not None else "")


def cmd_reconcile_done(argv):
    """Clear stale last_error from done entries.

    Neiro's slack-reconcile-done.sh, shared in #slack-へようこそ
    2026-11-12, adapted to message_state.json. Neiro found 35 done
    entries still carrying last_error on their side (22x "max attempts
    exceeded", 13x old bot-guard notes) -- their reconcile-replies
    filter ('reply'/'ambiguous') would have caught 0 of them, so the
    check is deliberately filter-free: done means resolved, whatever
    the old error said. Records themselves are never deleted; only
    last_error is cleared. The dead-letter file is untouched.
    """
    dry_run = "--dry-run" in argv
    with state_lock(exclusive=True):
        state = load_state()
        cleared = 0
        for st in state.values():
            if st.get("status") == "done" and st.get("last_error"):
                st.pop("last_error", None)
                cleared += 1
        if cleared and not dry_run:
            save_state(state)
    print(f"done entries with stale last_error cleared: {cleared} (dry_run={dry_run})")
    return 0


def cmd_mark(argv):
    if len(argv) < 2:
        print("usage: mark KEY STATUS [--error TEXT]", file=sys.stderr)
        return 2
    key, status = argv[0], argv[1]
    error = None
    if "--error" in argv:
        i = argv.index("--error")
        error = argv[i + 1] if i + 1 < len(argv) else ""
    if status not in ("processing", "done", "failed", "dead"):
        print(f"bad status: {status}", file=sys.stderr)
        return 2
    # Whole claim (load -> modify -> save, incl. dead-lettering) runs
    # under one exclusive lock, so it is atomic across workers.
    with state_lock(exclusive=True):
        return _mark_locked(key, status, error)


def _mark_locked(key, status, error):
    state = load_state()
    st = state.get(key) or {"status": "pending", "attempts": 0}
    if st.get("status") == "dead":
        # Dead is terminal: later marks (e.g. a failed call racing the
        # dead-lettering processing call) must not resurrect the message.
        print("dead")
        return 0
    now = datetime.now(timezone.utc).isoformat()
    if status == "processing":
        st["attempts"] = st.get("attempts", 0) + 1
        if st["attempts"] > MAX_ATTEMPTS:
            # Too many attempts: dead-letter the original message.
            rec = next((r for r in read_inbox() if key_of(r) == key), {"key": key})
            os.makedirs(EVENTS, exist_ok=True)
            with open(DEAD, "a", encoding="utf-8") as f:
                f.write(json.dumps(
                    {"key": key, "at": now, "attempts": st["attempts"],
                     "last_error": st.get("last_error"), "message": rec},
                    ensure_ascii=False) + "\n")
            st["status"] = "dead"
            st["updated_at"] = now
            state[key] = st
            save_state(state)
            print("dead")
            return 0
    st["status"] = status
    st["updated_at"] = now
    if error is not None:
        st["last_error"] = error
    elif status == "done":
        # DONE means resolved: a leftover error reads as "still
        # failing". Clear it unless the caller explicitly recorded a
        # new error. Adopted 2026-11-12 from Yama's test-17 fix
        # (commit clears error_message on DONE) and Neiro's
        # slack-reconcile-done.sh share in #slack-へようこそ.
        st.pop("last_error", None)
    state[key] = st
    save_state(state)
    print(status)
    return 0


def cmd_key_of():
    rec = json.load(sys.stdin)
    print(key_of(rec))


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    cmd = sys.argv[1]
    if cmd == "pending":
        return cmd_pending(sys.argv[2:])
    if cmd == "touch":
        return cmd_touch(sys.argv[2:])
    if cmd == "count-pending":
        cmd_count()
        return 0
    if cmd == "oldest-pending-age":
        cmd_oldest_pending_age()
        return 0
    if cmd == "stats":
        cmd_stats()
        return 0
    if cmd == "mark":
        return cmd_mark(sys.argv[2:])
    if cmd == "reconcile-done":
        return cmd_reconcile_done(sys.argv[2:])
    if cmd == "key-of":
        cmd_key_of()
        return 0
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
