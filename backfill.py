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

Usage: backfill.py [--limit N] [--floor-epoch SECONDS]
Prints a JSON summary: {"recovered": n, "checked": n, "errors": [...]}
--floor-epoch sets the floor explicitly (epoch seconds; messages at or
below it are skipped). Without it, an empty seen file means "first run":
the floor defaults to now, so the first run never replays history --
backfill only recovers messages missed during downtime, and on a first
run there is no downtime to recover from (adopted 2026-11-14 from
Neiro's review of the published code).
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
EVENTS = os.path.join(BASE, "events")
INBOX = os.path.join(EVENTS, "inbox.jsonl")
SEEN_PATH = os.path.join(EVENTS, "seen_event_ids.json")
SEEN_MAX = 1000
SLACKCLI = os.path.join(BASE, "bin", "slackcli")

# Channels the bot has joined (see README.md).
CHANNELS = [
    "C0EXAMPLE001",  # #slack-へようこそ
    "C0EXAMPLE002",  # #all-muse取締役会
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


def load_seen():
    try:
        with open(SEEN_PATH, "r", encoding="utf-8") as f:
            return [str(x) for x in json.load(f)][-SEEN_MAX:]
    except Exception:
        return []


def save_seen(order):
    tmp = SEEN_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(order[-SEEN_MAX:], f)
    os.replace(tmp, SEEN_PATH)


def append_inbox(record):
    os.makedirs(EVENTS, exist_ok=True)
    with open(INBOX, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


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

    seen_order = load_seen()
    seen_set = set(seen_order)
    # Floor: only messages newer than the newest message the listener has
    # already seen can be "missed during downtime". Anything older predates
    # the watcher (or was already handled by hand) and must not be re-added.
    floor = 0.0
    for key in seen_order:
        if key.startswith("ts:"):
            try:
                floor = max(floor, float(key.rsplit(":", 1)[1]))
            except ValueError:
                pass
    if floor_arg is not None:
        floor = floor_arg
    elif floor == 0.0:
        # No seen ts keys (first run, or the seen file was lost): the
        # old floor=0 admitted the last 100 messages wholesale. Default
        # to now instead -- there is no downtime window to recover, so
        # nothing is re-admitted. Pass --floor-epoch explicitly to
        # recover from a known point in time.
        floor = time.time()
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
        append_inbox(record)
        seen_set.add(key)
        seen_order.append(key)
        recovered += 1

    for channel in CHANNELS:
        try:
            data = run_slackcli(
                ["conversations", "read", channel,
                 "--limit", limit, "--json"])
        except Exception as e:
            errors.append(f"{channel}: {e}")
            continue
        threads = []
        for msg in data.get("messages") or []:
            admit(channel, msg)
            if msg.get("reply_count"):
                threads.append(msg["ts"])
        for ts in threads:
            try:
                tdata = run_slackcli(
                    ["conversations", "read", channel,
                     "--thread-ts", ts, "--json"])
            except Exception as e:
                errors.append(f"{channel} thread {ts}: {e}")
                continue
            for msg in tdata.get("messages") or []:
                admit(channel, msg)

    if recovered:
        save_seen(seen_order)

    print(json.dumps(
        {"recovered": recovered, "checked": checked, "errors": errors},
        ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
