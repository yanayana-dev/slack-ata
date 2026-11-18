#!/usr/bin/env python3
"""Shared snapshot/restore for the inbox file (events/inbox.jsonl).

Neiro's 2026-11-18 share (thread 1795006461.907139): inbox.jsonl is
the only copy of pending message texts. If the whole file becomes
unreadable, the pending messages are gone from view even though
message_state.json still holds their keys -- they can never be
processed. Neiro's inbox is 1.2MB / 854 lines, append-only, with a
monthly archive; Yanayana's is 968K / 764 lines, also append-only, so
the same shape fits:

- snapshot_inbox: copy inbox.jsonl to inbox.jsonl.bak, but only when
  every non-blank line parses as JSON. A corrupt body must never
  overwrite a good backup (same guard as seen_state.save_seen), and a
  valid-but-empty body must not clobber a non-empty .bak either.
  Snapshots happen at backfill time (Neiro's design: not on every
  append -- the listener appends per event, so a copy per append
  would race and waste) and, throttled to once a day, from the poll
  path, so the .bak does not go stale when backfill rarely runs
  (agy review point, 2026-11-18). Writes are atomic (tmp + replace),
  so a crash mid-copy leaves either the old .bak or the new one.
- restore_inbox: when the body is missing, or wholly unparseable (at
  least one non-blank line, zero parsed), copy the .bak back to the
  body and leave a recovery marker (events/inbox_recovery.json) with
  the .bak's newest ts. Partial corruption (some lines parse) is NOT
  restored: read_inbox already skips bad rows, and restoring would
  drop newer good rows -- the .bak is for "the whole file is gone".
- The marker matters because Yanayana's backfill floor comes from
  seen_event_ids.json, which keeps advancing past the .bak snapshot
  (agy review point, 2026-11-18): without a floor rewind, events
  between the snapshot and the corruption are excluded from backfill
  forever. backfill.py rewinds its floor to the marker's ts and
  re-admits messages missing from the restored inbox even when their
  ts: key is already in the seen set; the marker is consumed after a
  clean backfill run (kept when the run had errors, so the next run
  retries the rewind).

Known edge, accepted: re-admitted messages include ones already
processed before the corruption (their live inbox record was in the
lost part, so only the backfill copy exists). They come back as
pending under a ts: key and are reprocessed once; reply_once checks
the thread before sending, so no reply is ever double-posted, but a
task-type message could be re-executed. The window is bounded by the
snapshot age, and only a real corruption triggers this path at all.
"""
import json
import os
import time
from datetime import datetime, timezone

# Snapshot at most this often from the poll path (backfill time always
# refreshes: it passes max_age_secs=0). A day keeps the .bak fresh
# without copying a ~1MB file on every 30-second poll.
DEFAULT_MAX_AGE_SECS = 86400


def _parse_lines(path):
    """Read a .jsonl file; None when it cannot be opened at all.

    Returns (records, total_nonblank, malformed). Blank lines are
    ignored, not counted as malformed.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return None
    records = []
    total = 0
    malformed = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        total += 1
        try:
            records.append(json.loads(line))
        except Exception:
            malformed += 1
    return records, total, malformed


def newest_ts(records):
    """Newest message ts (epoch float) in parsed inbox records."""
    best = None
    for rec in records or []:
        try:
            ts = float(rec.get("ts"))
        except (TypeError, ValueError):
            continue
        if best is None or ts > best:
            best = ts
    return best


def inbox_keys(records):
    """Set of (channel, ts) pairs present in parsed inbox records."""
    return {(r.get("channel"), r.get("ts")) for r in records or []
            if r.get("ts")}


def _marker_path(inbox_path):
    return os.path.join(os.path.dirname(inbox_path),
                        "inbox_recovery.json")


def _atomic_write(path, data_bytes):
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data_bytes)
    os.replace(tmp, path)


def snapshot_inbox(path, max_age_secs=DEFAULT_MAX_AGE_SECS, now=None):
    """Copy a fully-parseable inbox body to path + '.bak'.

    Returns True when a snapshot was written. Skipped (False) when the
    body is missing, has any malformed line, is empty, or the existing
    .bak is still fresh (younger than max_age_secs). A corrupt or empty
    body never clobbers a good backup.
    """
    if now is None:
        now = time.time()
    parsed = _parse_lines(path)
    if parsed is None:
        return False
    records, total, malformed = parsed
    if total == 0 or malformed:
        return False
    bak = path + ".bak"
    try:
        if os.path.exists(bak) and now - os.path.getmtime(bak) < max_age_secs:
            return False
    except OSError:
        return False
    # Empty-body guard (same shape as seen_state): an empty body must
    # not clobber a non-empty .bak. total > 0 above already excludes
    # the empty case, so this only fires if the .bak itself is
    # somehow non-empty while the body parsed to nothing -- kept as a
    # belt-and-braces check on the copy itself.
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return False
    if not raw.strip():
        return False
    try:
        _atomic_write(bak, raw)
    except OSError:
        return False
    return True


def restore_inbox(path):
    """Restore the inbox body from its .bak when wholly unreadable.

    Returns (restored, bak_newest_ts). Restores only when the body is
    missing, or has non-blank lines but none of them parse -- and only
    when the .bak exists and parses fully. On restore, writes the
    recovery marker so backfill.py can rewind its floor to the .bak's
    newest ts (see module docstring).
    """
    parsed = _parse_lines(path)
    needs_restore = False
    if parsed is None:
        needs_restore = True
    else:
        _, total, malformed = parsed
        if total > 0 and malformed == total:
            needs_restore = True
    if not needs_restore:
        return False, None
    bak = path + ".bak"
    bak_parsed = _parse_lines(bak)
    if bak_parsed is None:
        return False, None
    bak_records, bak_total, bak_malformed = bak_parsed
    if bak_total == 0 or bak_malformed:
        return False, None
    try:
        with open(bak, "rb") as f:
            raw = f.read()
        _atomic_write(path, raw)
    except OSError:
        return False, None
    ts = newest_ts(bak_records)
    try:
        marker = {
            "restored_at": datetime.now(timezone.utc).isoformat(),
            "bak_newest_ts": ts,
        }
        _atomic_write(_marker_path(path),
                      json.dumps(marker, ensure_ascii=False).encode("utf-8"))
    except OSError:
        pass  # the restore itself succeeded; a missing marker only
        # means backfill will not rewind its floor for the gap
    return True, ts


def read_recovery_marker(inbox_path):
    """The recovery marker left by restore_inbox, or None."""
    try:
        with open(_marker_path(inbox_path), "r", encoding="utf-8") as f:
            marker = json.load(f)
    except Exception:
        return None
    if not isinstance(marker, dict):
        return None
    try:
        ts = float(marker.get("bak_newest_ts"))
    except (TypeError, ValueError):
        return None
    return {"bak_newest_ts": ts,
            "restored_at": marker.get("restored_at")}


def consume_recovery_marker(inbox_path):
    """Remove the recovery marker after a clean backfill run."""
    try:
        os.remove(_marker_path(inbox_path))
    except OSError:
        pass
