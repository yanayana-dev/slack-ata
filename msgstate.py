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

Total claim lifetime (2026-11-22, touch-loophole fix discussed in
#slack-へようこそ thread 1795333580.961869): a claim also goes stale
when it is older than MAX_CLAIM_SECS from its first mark processing,
however often touch refreshed updated_at in between. touch alone
never consumed attempts, so a worker that kept touching could extend
a claim forever; the sweep now reclaims on claimed_at age too, and
each reclaim burns one attempt on the next claim, so the loop closes
at MAX_ATTEMPTS. 30 minutes fits our side: normal claims finish in
seconds to a few minutes and touch is only used between steps of long
implementation or test runs. (Neiro's side keeps a 3-hour startup
grace, so a wall-clock cap from first wake does not fit them the
same way; Yama's side counts reaps instead -- same principle,
different shape.)

Usage:
  msgstate.py pending [--limit N | --all]
                             # JSON list of pending/failed messages,
                             # oldest first, capped at
                             # DEFAULT_PENDING_LIMIT unless --limit N
                             # or --all is given (see below)
  msgstate.py heal-replied [--dry-run]
                             # mark done every inbox message that a
                             # reply ledger (replies.jsonl /
                             # replied.jsonl) proves was answered but
                             # whose state never reached done -- the
                             # bookkeeping seam heal (see
                             # heal_replied_locked). Also runs as part
                             # of pending/count/oldest-pending-age.
  msgstate.py touch KEY      # refresh a processing entry's
                             # updated_at (claim heartbeat; attempts
                             # unchanged) so long single-item work is
                             # not swept as stale mid-flight -- but
                             # only within MAX_CLAIM_SECS of the
                             # claim's start; past that it prints
                             # "expired" and does not extend
  msgstate.py count-pending [--json]  # pending count; --json
                                  # also prints the processing count
                                  # {"pending": N, "processing": M}
  msgstate.py oldest-pending-age  # seconds since the oldest pending
                                  # message arrived (empty when none)
  msgstate.py stats          # skipped-row counts for the inbox file
  msgstate.py mark KEY STATUS [--error TEXT]
  msgstate.py key-of         # read one inbox JSON line on stdin, print its key
  msgstate.py reconcile-done [--dry-run]  # clear stale last_error from done entries

State: events/message_state.json ; dead letters: events/dead_letter.jsonl
Rejected late marks (terminal-guard): events/mark_rejected.jsonl

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
# Reply ledgers consulted by the heal (heal_replied_locked):
# replies.jsonl is replylog.py's ledger, replied.jsonl is
# reply_once.py's. Either one proving a reply is enough.
REPLIES_LEDGER = os.path.join(EVENTS, "replies.jsonl")
REPLIED_LEDGER = os.path.join(EVENTS, "replied.jsonl")
HEAL_LOG = os.path.join(EVENTS, "state_heal.jsonl")
# Rejected late marks (2026-11-23, promised in #slack-へようこそ thread
# 1795420153.646849): _mark_locked terminal-guards done/dead, but the
# rejection path used to return silently -- a late write left no trace
# at all. Every rejection now appends one structured line here
# (timestamp, key, attempted transition, current state, reason) so the
# daily summary / a later audit can count how often late writes race
# a terminal state. Append-only, never deduped: each rejection is a
# distinct event. The log write is fail-open (see
# _log_mark_rejected): state integrity matters more than the log.
MARK_REJECTED_LOG = os.path.join(EVENTS, "mark_rejected.jsonl")
# Neiro さんの ops_alerts_archive.jsonl と同じ上限・世代数 (2026-11-23,
# #slack-へようこそ thread 1795420153.646849 での共有から取り込み)。
# 却下ログは追記専用で消費側がないため、上限なしでは際限なく育つ。
MARK_REJECTED_MAX_BYTES = 5 * 1024 * 1024
MARK_REJECTED_ROTATED = MARK_REJECTED_LOG + ".1"
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
# Total claim lifetime, see the module docstring: a claim is stale
# once it is older than this from its first mark processing, however
# often touch refreshed updated_at. Each reclaim burns one attempt
# on the next claim, so a touching-only worker dead-letters after
# MAX_ATTEMPTS reclaims at most.
MAX_CLAIM_SECS = 1800
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
# each tier (stable sort over inbox order). IDs are scrubbed to
# placeholders on publish (see scrub.sed).
#
# Queue aging (2026-11-23, Neiro's proven thresholds shared in
# #slack-へようこそ): tiers alone starve the bottom tier under a
# sustained flood -- on 2026-11-23 a tier-2 message waited ~22
# minutes while addressed posts kept arriving. A message waiting
# longer than AGING_PROMOTE_SECS rises one tier; longer than
# AGING_TOP_SECS it rises to the top tier (same as a DM). Neiro has
# run these exact values in production since 2026-11-21 with no
# starvation since; Yama needs no aging (FIFO, no overtaking), and
# thresholds need not be unified across camps. Age is measured from
# received_at, the same clock as oldest_pending_age; a message
# without a parseable received_at is never promoted.
AGING_PROMOTE_SECS = 300
AGING_TOP_SECS = 900
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


def _effective_tier(rec, now=None):
    """Base tier adjusted by waiting time (aging).

    Waiting > AGING_PROMOTE_SECS (5 min) rises one tier; waiting >
    AGING_TOP_SECS (15 min) rises to the top tier. Tier 0 never
    moves. Within an effective tier the sort is stable over inbox
    order, so oldest first. Unparseable/missing received_at means
    age unknown -- no promotion, never a crash.
    """
    tier = _priority_tier(rec)
    if tier == 0:
        return 0
    if now is None:
        now = datetime.now(timezone.utc)
    received = _parse_ts(rec.get("received_at") or "")
    if received is None:
        return tier
    if received.tzinfo is None:
        received = received.replace(tzinfo=timezone.utc)
    age = (now - received).total_seconds()
    if age > AGING_TOP_SECS:
        return 0
    if age > AGING_PROMOTE_SECS:
        return max(0, tier - 1)
    return tier


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
    # Priority order with aging (see _effective_tier): stable sort,
    # oldest first inside each effective tier. The cap below keeps
    # the first N of that order; the deferred rest re-wake on the
    # next poll. --all also returns priority order so every consumer
    # sees the same sequence. One clock for the whole sort so two
    # items straddling a threshold cannot flip mid-sort.
    now = datetime.now(timezone.utc)
    items.sort(key=lambda it: _effective_tier(it["message"], now))
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

    A claim is also stale once its total lifetime exceeds
    MAX_CLAIM_SECS from claimed_at, however recently touch refreshed
    updated_at -- that is the touch-loophole fix (2026-11-22):
    without it a worker that only touches never goes stale and never
    burns attempts. Entries recorded before claimed_at existed fall
    back to updated_at as the claim start.
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
        claimed = _parse_ts(st.get("claimed_at") or "") or updated
        age = (now - updated).total_seconds()
        total_age = (now - claimed).total_seconds()
        if age < TIMEOUT_SECS and total_age < MAX_CLAIM_SECS:
            continue
        at = now.isoformat()
        if total_age >= MAX_CLAIM_SECS:
            reason = (f"claim lifetime exceeded after {int(total_age)}s "
                      f"(max {MAX_CLAIM_SECS}s)")
        else:
            reason = (f"stale processing reclaimed after {int(age)}s "
                      f"(timeout {TIMEOUT_SECS}s)")
        if st.get("attempts", 0) >= MAX_ATTEMPTS:
            rec = next((r for r in read_inbox() if key_of(r) == key), {"key": key})
            os.makedirs(EVENTS, exist_ok=True)
            with open(DEAD, "a", encoding="utf-8") as f:
                f.write(json.dumps(
                    {"key": key, "at": at, "attempts": st.get("attempts"),
                     "last_error": reason,
                     "message": rec}, ensure_ascii=False) + "\n")
            st["status"] = "dead"
        else:
            st["status"] = "pending"
        st["updated_at"] = at
        st["last_error"] = reason
        state[key] = st
        changed = True
    if changed:
        save_state(state)
    return changed


def _ledger_targets():
    """(channel, target_ts) pairs a reply ledger proves were answered.

    Both ledgers are consulted: replies.jsonl (replylog.py) and
    replied.jsonl (reply_once.py). A send through send_reply_locked.py
    lands in both, but a send through either path alone still leaves
    one trace, and one trace is enough to prove the reply exists.
    Entries without a channel fall back to a ts-only set, matched
    against any channel (legacy rows; ts values are unique enough
    that a cross-channel collision is not a practical risk).
    Corrupt lines are skipped like every other ledger reader.
    """
    pairs = set()
    ts_only = set()
    for path in (REPLIES_LEDGER, REPLIED_LEDGER):
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                    except Exception:
                        continue
                    if not e.get("target_ts"):
                        continue
                    if e.get("channel"):
                        pairs.add((e["channel"], e["target_ts"]))
                    else:
                        ts_only.add(e["target_ts"])
        except FileNotFoundError:
            continue
    return pairs, ts_only


def heal_replied_locked(state, dry_run=False):
    """Mark done every inbox message a reply ledger proves answered.

    The bookkeeping seam (observed 2026-11-18, 2026-11-22 and
    2026-11-23): the hook flow finalizes a message in separate steps
    -- send, then replylog record, then mark done -- so a worker run
    that ends (timeout, cutoff, error) after the send but before the
    writes leaves the message pending/failed even though the reply is
    in the thread and at least one ledger recorded it. The next wake
    then re-processes an answered message: attempts burn on re-claims
    and, before this heal, only the re-processing worker's live
    thread check stood between it and a duplicate or a dead letter.

    This heal closes the gap deterministically from local records
    (no Slack read): any inbox message whose (channel, ts) appears as
    a target in either reply ledger is done, whatever its state says.
    Entries in "processing" are left alone -- a live worker holds
    that claim and finalizes it itself; if that worker dies, the
    stale sweep returns the entry to pending and the next heal closes
    it. "dead" is terminal and never resurrected. Healed entries get
    a "healed" stamp and a line in events/state_heal.jsonl so the
    daily summary can count how often the seam still opens.

    Must be called holding the exclusive state lock. Returns the list
    of healed {"key", "channel", "ts"} dicts.
    """
    pairs, ts_only = _ledger_targets()
    if not pairs and not ts_only:
        return []
    now = datetime.now(timezone.utc).isoformat()
    healed = []
    for rec in read_inbox():
        ts = rec.get("ts")
        if not ts:
            continue
        if (rec.get("channel"), ts) not in pairs and ts not in ts_only:
            continue
        key = key_of(rec)
        st = state.get(key)
        status = (st or {}).get("status", "pending")
        if status in ("done", "dead", "processing"):
            continue
        healed.append({"key": key, "channel": rec.get("channel"), "ts": ts})
        if dry_run:
            continue
        st = dict(st) if st else {"attempts": 0}
        st["status"] = "done"
        st["updated_at"] = now
        st.pop("last_error", None)
        st["healed"] = {"source": "reply-ledger", "at": now}
        state[key] = st
    if healed and not dry_run:
        save_state(state)
        os.makedirs(EVENTS, exist_ok=True)
        with open(HEAL_LOG, "a", encoding="utf-8") as f:
            for h in healed:
                f.write(json.dumps({**h, "at": now}, ensure_ascii=False) + "\n")
    return healed


def cmd_heal_replied(argv):
    dry_run = "--dry-run" in argv
    with state_lock(exclusive=True):
        state = load_state()
        healed = heal_replied_locked(state, dry_run=dry_run)
    print(json.dumps({"healed": healed, "count": len(healed),
                      "dry_run": dry_run}, ensure_ascii=False))
    return 0


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
        heal_replied_locked(state)
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

    The heartbeat only extends a claim within MAX_CLAIM_SECS of its
    start: past that, touch prints "expired" and leaves updated_at
    alone, so the next sweep reclaims the claim and the next claim
    burns an attempt. Without this bound a worker that only touches
    could extend a claim forever without ever consuming attempts
    (touch loophole, fixed 2026-11-22). Entries recorded before
    claimed_at existed get it stamped from their last heartbeat on
    the first touch under the new code.
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
        now = datetime.now(timezone.utc)
        claimed = _parse_ts(st.get("claimed_at") or "")
        if claimed is None:
            claimed = _parse_ts(st.get("updated_at") or "")
            if claimed is not None:
                st["claimed_at"] = claimed.isoformat()
        if claimed is not None and (now - claimed).total_seconds() >= MAX_CLAIM_SECS:
            state[key] = st
            save_state(state)
            print("expired")
            return 0
        st["updated_at"] = now.isoformat()
        state[key] = st
        save_state(state)
    print("touched")
    return 0


def cmd_count(argv=None):
    """Print the pending count, or pending+processing as JSON with --json.

    Plain `count-pending` keeps printing just the pending number:
    ~/hooks/scripts/slack-inbox.sh parses that bare number, so the
    format must not change. With --json it prints
    {"pending": N, "processing": M} from one snapshot (both counts
    read after the same sweep+heal, so they agree with each other).
    The watchdog's stall warning uses the JSON form so the warning
    can say whether anything is actually in flight -- "worker
    stopped" and "busy processing a long batch" used to look
    identical (the false-alarm confusion Neiro hit on 2026-11-24,
    fixed on their side the same day by adding their processing
    count). Processing here means state entries with status
    processing after the stale sweep, i.e. live claims;
    pending_items() excludes them by design, so they are counted
    from the state dict directly.
    """
    json_out = bool(argv) and "--json" in argv
    with state_lock(exclusive=True):
        state = load_state()
        sweep_stale_locked(state)
        heal_replied_locked(state)
        n = len(pending_items(state))
        if json_out:
            m = sum(1 for st in state.values()
                    if st.get("status") == "processing")
            print(json.dumps({"pending": n, "processing": m}))
            return
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
        heal_replied_locked(state)
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


def _rotate_mark_rejected_log():
    """5MBを超えたら1世代だけ残してローテーションする。

    Neiro さんのアーカイブと同じ形。失敗しても呼び出し元は止めない
    (fail-open): ローテーションに失敗したら上限超えのまま追記する。
    Never raises.
    """
    try:
        if (os.path.exists(MARK_REJECTED_LOG)
                and os.path.getsize(MARK_REJECTED_LOG) >= MARK_REJECTED_MAX_BYTES):
            if os.path.exists(MARK_REJECTED_ROTATED):
                os.remove(MARK_REJECTED_ROTATED)
            os.replace(MARK_REJECTED_LOG, MARK_REJECTED_ROTATED)
    except Exception:
        pass


def _log_mark_rejected(key, attempted_status, current_status, reason):
    """Record a terminal-guard rejection of a mark write.

    One JSON line per rejection in events/mark_rejected.jsonl:
    {"at", "key", "attempted_status", "current_status", "reason"}.
    Fail-open by design: a logging failure (read-only events dir,
    disk full, ...) must never break the mark operation itself --
    the rejection already happened and the state file is untouched
    either way. Never raises.
    """
    try:
        os.makedirs(EVENTS, exist_ok=True)
        _rotate_mark_rejected_log()
        rec = {
            "at": datetime.now(timezone.utc).isoformat(),
            "key": key,
            "attempted_status": attempted_status,
            "current_status": current_status,
            "reason": reason,
        }
        with open(MARK_REJECTED_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        return False
    return True


def _mark_locked(key, status, error):
    state = load_state()
    st = state.get(key) or {"status": "pending", "attempts": 0}
    if st.get("status") == "dead":
        # Dead is terminal: later marks (e.g. a failed call racing the
        # dead-lettering processing call) must not resurrect the message.
        # The rejection is logged (2026-11-23): a silent return left
        # no trace that a late write had raced the terminal state.
        _log_mark_rejected(key, status, "dead",
                           "terminal state dead: late mark rejected")
        print("dead")
        return 0
    if st.get("status") == "done" and status != "done":
        # Done is final too: a worker whose claim went stale and was
        # completed by another worker must not resurrect the message
        # with a late processing/failed mark. Yama's commit_event has
        # this exact hole (their candidate 19 in slack-yama
        # IMPROVEMENTS.md: FAILED committed onto a DONE event brings
        # it back to PENDING); our dead guard already existed and
        # done gets the same treatment. No legitimate flow marks a
        # done message anything but done -- pending_items never hands
        # out a message any of whose copies is done. The rejection
        # is logged, same as the dead guard above.
        _log_mark_rejected(key, status, "done",
                           "terminal state done: late mark rejected")
        print("done")
        return 0
    now = datetime.now(timezone.utc).isoformat()
    if status == "processing":
        st["attempts"] = st.get("attempts", 0) + 1
        # Fresh claim: the total-lifetime clock starts here. A later
        # touch may refresh updated_at but never moves claimed_at,
        # so the sweep's MAX_CLAIM_SECS bound cannot be dodged.
        st["claimed_at"] = now
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
        cmd_count(sys.argv[2:])
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
    if cmd == "heal-replied":
        return cmd_heal_replied(sys.argv[2:])
    if cmd == "key-of":
        cmd_key_of()
        return 0
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
