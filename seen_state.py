#!/usr/bin/env python3
"""Shared load/save for the seen-event-ids file (events/seen_event_ids.json).

Both listener.py and backfill.py keep their duplicate suppression in
this one file: recent Slack event_ids plus channel:ts keys, newest
1000. It is a single point of failure, so it gets the same .bak
treatment auto_join_channels.py gives auto_join_state.json (Yama's
2026-11-18 .bak idea, adopted for auto-join the same day):

- load_seen: the main file first; when it is missing or corrupt, the
  backup copy (seen_event_ids.json.bak) kept by save_seen is tried,
  and a state recovered from the backup is written back to the main
  file at once, so the next run does not depend on the .bak again.
- save_seen: before replacing the main file, the previous good main
  is copied to .bak (raw bytes). Only a main that parses as a list
  of strings is backed up: a corrupt main must never overwrite a
  good backup. A valid-but-empty main must not clobber a non-empty
  .bak either (same guard as auto_join's save_state).
- When both files exist but neither parses, load_seen returns []
  instead of crashing: the listener must stay up, and the damage
  is bounded -- msgstate.py dedupes by event_id so a re-admitted
  event is never replied to twice, and backfill.py falls back to a
  "now" floor when the seen list is empty, so no old history is
  re-admitted. A lost seen file is still worth noticing, so callers
  that can log should note the recovery; the load itself stays silent
  because both callers run in hot paths (listener startup, backfill).

Writes are atomic (tmp + os.replace), so a crash mid-write leaves
either the old file or the new one, never a torn one.
"""
import json
import os


def _read_seen_file(path):
    """Parse one seen file; a list of strings, or None when unusable.

    Missing file, bad JSON, or a JSON value that is not a list all
    mean "unusable" here. Entries are coerced with str(), matching
    the old inline loads in listener.py / backfill.py.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    if not isinstance(data, list):
        return None
    return [str(x) for x in data]


def load_seen(path):
    """Load the seen list, falling back to the .bak copy when needed."""
    data = _read_seen_file(path)
    if data is not None:
        return data
    data = _read_seen_file(path + ".bak")
    if data is not None:
        try:
            save_seen(data, path)
        except Exception:
            pass  # recovery already succeeded; a failed write-back
            # must not turn this run into a failure
        return data
    return []


def union_seen(disk, own):
    """Union of two seen lists: disk order first, then own-only keys.

    Both writers of the seen file (listener.py, backfill.py) used to
    save their own full snapshot, so whichever saved last silently
    dropped keys the other had added in between -- and a dropped
    ts: key lets a later redelivery be admitted a second time.
    Merging with the on-disk list before saving makes every save
    union-only: keys are never lost, only added. Callers trim the
    result to their SEEN_MAX.
    """
    out = list(disk)
    have = set(out)
    for key in own:
        if key not in have:
            have.add(key)
            out.append(key)
    return out


def save_seen(order, path):
    """Persist the seen list, keeping the previous good one as .bak."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    # Keep the previous good state as a backup before replacing it.
    # Only a file that parses as a list of strings is backed up, and
    # a valid-but-empty main must not clobber a backup that still
    # holds keys (same guards as auto_join_channels.save_state).
    current = _read_seen_file(path)
    if current is not None:
        backup = _read_seen_file(path + ".bak")
        if not (current == [] and backup):
            try:
                with open(path, encoding="utf-8") as f:
                    previous = f.read()
                with open(path + ".bak", "w", encoding="utf-8") as f:
                    f.write(previous)
            except Exception:
                pass  # a failed backup must not block the save itself
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(list(order), f)
    os.replace(tmp, path)
