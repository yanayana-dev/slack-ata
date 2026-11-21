#!/usr/bin/env python3
"""Backfill missed Slack messages from channel history (Yama's design).

When the Socket Mode listener is down (e.g. the VM was replaced and the
systemd units vanished), messages posted during the downtime never reach
events/inbox.jsonl. This script compares recent Slack history against
events/seen_event_ids.json and appends any missed message as an inbox
record (source: "backfill"), so the normal per-message state machine
(msgstate.py) picks it up. Only messages newer than the newest message
the listener has already seen are admitted: anything older predates the
watcher (or was already handled by hand) and is skipped, so a first run
never replays a whole day of history.

Called from the slack-inbox hook right after it repairs the listener,
and can also be run manually. Safe to run repeatedly: messages already
in the seen set are skipped, and records use the same ts:channel:ts key
the state machine uses, so re-adding is a no-op. The listener's own
dedupe (same seen file) also keeps it from re-admitting a backfilled
message if Slack redelivers it.

Usage: backfill.py [--limit N] [--floor-epoch SECONDS] [--window-minutes N] [--channel ID ...]
Prints a JSON summary: {"recovered": n, "checked": n, "errors": [...]}
--floor-epoch sets the floor explicitly (epoch seconds; messages at or
below it are skipped). Without it, an empty seen file means "first run":
the floor defaults to now, so the first run never replays history --
backfill only recovers messages missed during downtime, and on a first
run there is no downtime to recover from (adopted 2026-11-14 from
Neiro's review of the published code).
--window-minutes sets the floor to now minus N minutes instead of the
newest seen message (adopted 2026-11-18 from Neiro's fix
neiro-slack-system 96f8597): for a just-joined channel the gap posts
are newer than the window even when another channel has a newer seen
message, which the global newest-seen floor would wrongly skip. The
auto-join path passes 15 (watchdog interval is 5 minutes, so 15 covers
the gap with margin). Explicit --floor-epoch still wins over the
window. Already-seen messages are skipped either way.
--channel restricts the run to the given channel(s) (repeatable); this
is how auto_join_channels.py recovers messages posted in a new channel
between its creation and the bot joining it (up to one 5-minute
watchdog interval later) -- Yama's 2026-11-18 point that the gap would
otherwise be missed. Without --channel, CHANNELS above is used.
Unlike Neiro's v1 seed, this backfill also reads thread replies
(messages with reply_count are followed into their threads), so a
gap-time thread reply is recovered too. Thread re-reads pass
--oldest <floor> (adopted 2026-11-18 from Neiro's
neiro-slack-system 6980acd): without it a thread with more than 100
replies returns only one page and newer replies past the page can be
dropped; with the floor, only replies newer than the floor come back.
Channel history itself is paged (MAX_HISTORY_PAGES, Neiro's 2026-11-21
proposal): without paging, a long outage with more than --limit posts
past the cutoff strands the middle ones, because the newest page
advances max_ts past them.
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

import inbox_state
import seen_state

BASE = os.path.dirname(os.path.abspath(__file__))
EVENTS = os.path.join(BASE, "events")
INBOX = os.path.join(EVENTS, "inbox.jsonl")
SEEN_PATH = os.path.join(EVENTS, "seen_event_ids.json")
SEEN_MAX = 1000
SLACKCLI = os.path.join(BASE, "bin", "slackcli")

# History is read in pages (Neiro's 2026-11-21 proposal, adopted the
# same day): a single conversations read returns only --limit messages,
# so a long outage with more than that many posts past the cutoff
# would strand the middle ones forever (max_ts advances past them on
# the newest page). Pages walk backwards with --latest set to the
# oldest ts of the previous page (--latest is inclusive, so the
# boundary message repeats; admit() dedupes it by seen key). The cap
# bounds the read; hitting it is reported, never silent.
MAX_HISTORY_PAGES = 3

# Channels the bot has joined (see README.md).
CHANNELS = [
    "C0EXAMPLE001",  # #slack-へようこそ
    "C0EXAMPLE002",  # #all-muse取締役会
    "C0EXAMPLE004",  # #yamaguchi勉強応援
]

# Yanayana itself: its posts must never enter the inbox (bot-loop guard,
# same rule as listener.py).
OWN_USER_ID = "U0EXAMPLEBOT"
OWN_BOT_ID = "B0EXAMPLEBOT"

SKIP_SUBTYPES = {
    "channel_join", "channel_leave", "channel_topic",
    "message_changed", "message_deleted", "message_replied",
}

# History items carry no "subtype" for join/leave notices, so match the
# display text as well (workspace language is Japanese).
import re
SKIP_TEXT_PATTERNS = [
    re.compile(r"さんがチャンネルに参加しました\s*$"),
    re.compile(r"さんがチャンネルから退出しました\s*$"),
]


def run_slackcli(args, timeout=60):
    """Run slackcli, return parsed JSON (stdout has non-JSON lead lines)."""
    out = subprocess.run(
        [SLACKCLI] + args, capture_output=True, text=True, timeout=timeout)
    if out.returncode != 0:
        raise RuntimeError(
            f"slackcli {' '.join(args)} failed: {out.stderr.strip()[:300]}")
    text = out.stdout
    i = text.find("{")
    if i < 0:
        raise RuntimeError(f"slackcli {' '.join(args)}: no JSON in output")
    return json.loads(text[i:])


def read_history_pages(channel, limit, floor):
    """Read a channel's history across pages, newest pages first.

    Returns (messages, capped): messages is every message read, oldest
    first within each page, pages in newest-to-oldest order; capped is
    True when MAX_HISTORY_PAGES ran out while has_more was still set,
    i.e. older messages may remain unread. Stops early when a page has
    no more, or its newest message is at or below the floor (older
    pages can only be older, so nothing past the floor is missed).
    Raises on a slackcli failure so the caller can record the error.
    """
    messages = []
    latest = None
    capped = False
    for _ in range(MAX_HISTORY_PAGES):
        args = ["conversations", "read", channel,
                "--limit", limit, "--json"]
        if latest is not None:
            args += ["--latest", latest]
        data = run_slackcli(args)
        page = data.get("messages") or []
        if not page:
            break
        messages.extend(page)
        if not data.get("has_more"):
            break
        try:
            if float(page[-1].get("ts")) <= floor:
                break
        except (TypeError, ValueError):
            break
        latest = page[0].get("ts")
        if latest is None:
            break
    else:
        capped = True
    return messages, capped


def load_seen():
    # Seen file with .bak fallback (seen_state.py): a corrupt or
    # deleted main file recovers from the backup copy instead of
    # silently starting empty.
    return seen_state.load_seen(SEEN_PATH)[-SEEN_MAX:]


def save_seen(order):
    # Keeps the previous good file as .bak before replacing it.
    seen_state.save_seen(order[-SEEN_MAX:], SEEN_PATH)


def compute_floor(seen_order, floor_arg=None, window_minutes=None, now=None):
    """Floor (epoch seconds) below which messages are skipped.

    Precedence: explicit --floor-epoch wins; then --window-minutes
    (now minus the window); then the newest ts in the seen file; then
    now on a first run (no seen ts keys). The window matters for a
    just-joined channel: the global newest-seen floor is set by
    whatever channel was busiest, so it can sit above the joined
    channel's gap posts and skip them. A time window relative to now
    does not have that cross-channel leak.
    """
    if floor_arg is not None:
        return floor_arg
    if now is None:
        now = time.time()
    if window_minutes is not None:
        return now - window_minutes * 60
    floor = 0.0
    for key in seen_order:
        if key.startswith("ts:"):
            try:
                floor = max(floor, float(key.rsplit(":", 1)[1]))
            except ValueError:
                pass
    if floor == 0.0:
        # No seen ts keys (first run, or the seen file was lost): the
        # old floor=0 admitted the last 100 messages wholesale.
        # Default to now instead -- there is no downtime window to
        # recover, so nothing is re-admitted. Pass --floor-epoch
        # explicitly to recover from a known point in time.
        floor = now
    return floor


def append_inbox(record):
    # Append-if-absent by (channel, ts), shared with listener.py
    # (inbox_state.append_record_unique): this run's seen snapshot
    # can predate a socket admission of the same message, and the
    # plain append then duplicated the inbox line. Returns True
    # when this call appended the line.
    return inbox_state.append_record_unique(INBOX, record)


def main():
    limit = "100"
    if "--limit" in sys.argv:
        i = sys.argv.index("--limit")
        limit = sys.argv[i + 1] if i + 1 < len(sys.argv) else limit
    floor_arg = None
    if "--floor-epoch" in sys.argv:
        i = sys.argv.index("--floor-epoch")
        try:
            floor_arg = float(sys.argv[i + 1]) if i + 1 < len(sys.argv) else None
        except ValueError:
            floor_arg = None
    window_minutes = None
    if "--window-minutes" in sys.argv:
        i = sys.argv.index("--window-minutes")
        try:
            window_minutes = float(sys.argv[i + 1]) if i + 1 < len(sys.argv) else None
        except ValueError:
            window_minutes = None
    channels = CHANNELS
    if "--channel" in sys.argv:
        channels = [a for j, a in enumerate(sys.argv)
                    if j > 0 and sys.argv[j - 1] == "--channel" and a]
        if not channels:
            channels = CHANNELS

    seen_order = load_seen()
    seen_set = set(seen_order)
    # Inbox .bak snapshot (Neiro's 2026-11-18 share, inbox_state.py):
    # backfill time is the snapshot moment -- only when every line
    # parses, so a corrupt body never overwrites a good backup.
    # Forced refresh here (max_age_secs=0); the poll path throttles
    # the same snapshot to once a day.
    inbox_state.snapshot_inbox(INBOX, max_age_secs=0)
    # Recovery mode: the inbox body was restored from its .bak, and
    # the marker carries the .bak's newest ts. The seen file keeps
    # advancing past the snapshot, so without rewinding the floor to
    # the marker the gap between snapshot and corruption would be
    # excluded from backfill forever (agy review point, 2026-11-18).
    marker = inbox_state.read_recovery_marker(INBOX)
    recovery_floor = None
    present_keys = set()
    if marker is not None:
        recovery_floor = marker["bak_newest_ts"]
        parsed = inbox_state._parse_lines(INBOX)
        if parsed is not None:
            present_keys = inbox_state.inbox_keys(parsed[0])
    # Floor: only messages newer than the floor can be "missed during
    # downtime". Anything older predates the watcher (or was already
    # handled by hand) and must not be re-added.
    floor = compute_floor(seen_order, floor_arg, window_minutes)
    if recovery_floor is not None:
        floor = min(floor, recovery_floor)
    recovered = 0
    checked = 0
    errors = []

    def admit(channel, msg):
        """Append msg to the inbox unless already seen / own / skippable."""
        nonlocal recovered, checked
        checked += 1
        text = msg.get("text") or ""
        if (msg.get("subtype") in SKIP_SUBTYPES
                or not text.strip()
                or any(p.search(text) for p in SKIP_TEXT_PATTERNS)):
            return
        if msg.get("user") == OWN_USER_ID or msg.get("bot_id") == OWN_BOT_ID:
            return
        ts = msg.get("ts")
        if not ts:
            return
        try:
            if float(ts) <= floor:
                return
        except ValueError:
            return
        key = f"ts:{channel}:{ts}"
        if key in seen_set:
            # Recovery mode: the seen set already holds the gap
            # messages (the listener admitted them before the inbox
            # body was lost), so the plain seen check would skip
            # exactly the messages the restore needs back. Re-admit
            # only messages actually missing from the restored inbox
            # and newer than the .bak snapshot; anything already in
            # the inbox, or at/below the snapshot, stays skipped.
            if recovery_floor is None:
                return
            try:
                if float(ts) <= recovery_floor:
                    return
            except ValueError:
                return
            if (channel, ts) in present_keys:
                return
        record = {
            "event_id": None,
            "received_at": datetime.now(timezone.utc).isoformat(),
            "channel": channel,
            "channel_type": "channel",
            "user": msg.get("user"),
            "bot_id": msg.get("bot_id"),
            "app_id": msg.get("app_id"),
            "text": msg.get("text"),
            "ts": ts,
            "thread_ts": msg.get("thread_ts"),
            "subtype": msg.get("subtype"),
            "source": "backfill",
        }
        if append_inbox(record):
            recovered += 1
        # The key is remembered either way: when the append was a
        # no-op the message is already in the inbox (the listener
        # admitted it first), which is exactly what the seen set
        # exists to record.
        seen_set.add(key)
        seen_order.append(key)

    for channel in channels:
        try:
            messages, capped = read_history_pages(channel, limit, floor)
        except Exception as e:
            errors.append(f"{channel}: {e}")
            continue
        if capped:
            errors.append(
                f"{channel}: history page cap ({MAX_HISTORY_PAGES} pages) "
                "reached; older messages past the cutoff may be missed")
        threads = []
        for msg in messages:
            admit(channel, msg)
            if msg.get("reply_count"):
                threads.append(msg["ts"])
        for ts in threads:
            try:
                # --oldest <floor> (adopted 2026-11-18 from Neiro's
                # neiro-slack-system 6980acd): without it a thread
                # with more than 100 replies returns only one page,
                # and new replies past the page can be dropped. The
                # floor is exclusive, matching admit()'s ts <= floor
                # skip, so no reply is lost or double-counted.
                tdata = run_slackcli(
                    ["conversations", "read", channel,
                     "--thread-ts", ts, "--oldest", str(floor), "--json"])
            except Exception as e:
                errors.append(f"{channel} thread {ts}: {e}")
                continue
            for msg in tdata.get("messages") or []:
                admit(channel, msg)

    if recovered:
        # Merge with the on-disk list before saving (union_seen):
        # the listener may have added keys while this run was
        # reading history, and saving this run's snapshot alone
        # would drop them (a dropped ts: key re-admits a redelivery).
        save_seen(seen_state.union_seen(load_seen(), seen_order))

    print(json.dumps(
        {"recovered": recovered, "checked": checked, "errors": errors},
        ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
