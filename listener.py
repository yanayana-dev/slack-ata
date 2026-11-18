#!/usr/bin/env python3
"""Slack Socket Mode listener for workspace muse取締役会.

Connects with the app-level token (xapp-...), receives message events,
acknowledges each envelope, and appends one JSON line per message event to
events/inbox.jsonl. A separate hook polls that file every 30 seconds and
wakes the agent when new lines arrive.

Tokens live only in config.json next to this file (permissions 0600).
They are never printed, logged, or written to the inbox file.
"""
import json
import os
import sys
import time
from datetime import datetime, timezone

import dm_poll
import seen_state

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config.json")
INBOX_PATH = os.path.join(BASE, "events", "inbox.jsonl")
DM_INBOX_PATH = os.path.join(BASE, "events", "dm_inbox.jsonl")
HEARTBEAT_PATH = os.path.join(BASE, "events", "heartbeat.json")
SEEN_PATH = os.path.join(BASE, "events", "seen_event_ids.json")
SEEN_MAX = 1000

ATA_USER_ID = "U0EXAMPLEOWNER"  # あた, the app's authorizing user
DM_CHANNEL_TYPES = ("im", "mpim")
SKIP_SUBTYPES = {"message_changed", "message_deleted", "message_replied"}


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    for key in ("bot_token", "app_token"):
        val = cfg.get(key, "")
        if not val or "REPLACE-ME" in val:
            print(f"config.json: {key} is not set", file=sys.stderr)
            sys.exit(2)
    return cfg


def append_inbox(record):
    os.makedirs(os.path.dirname(INBOX_PATH), exist_ok=True)
    with open(INBOX_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def is_user_dm_event(payload, event):
    """True when a message event is a DM addressed to あた.

    Since the app subscribed to message.im / message.mpim on behalf
    of users (2026-11-14), the same socket also delivers events for
    あた's own DMs. The envelope says on whose behalf an event is
    delivered in payload["authorizations"]: a user event carries an
    entry for the authorizing user (あた, is_bot false), while bot
    events carry only the bot's authorization. The channel_type
    check keeps channel messages out even if a user authorization
    is present. A missing authorizations list is treated as a bot
    event, so the main path is unchanged for envelopes without one.
    """
    if (event or {}).get("channel_type") not in DM_CHANNEL_TYPES:
        return False
    for auth in (payload or {}).get("authorizations") or []:
        if auth.get("user_id") == ATA_USER_ID and not auth.get("is_bot"):
            return True
    return False


def dm_record_from_event(event, received_at):
    """One dm_inbox.jsonl line for a pushed DM event.

    Built by dm_poll.to_record so the shape is identical to the
    poller's records by construction; only the source differs.
    """
    conv = {"id": event.get("channel"),
            "channel_type": event.get("channel_type")}
    record = dm_poll.to_record(event, conv, received_at)
    record["source"] = "socket_push"
    return record


def append_dm_inbox(record):
    os.makedirs(os.path.dirname(DM_INBOX_PATH), exist_ok=True)
    with open(DM_INBOX_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def advance_dm_watermark(conv_id, ts):
    """Move dm_poll's watermark for conv_id forward to ts.

    A pushed DM is already in dm_inbox.jsonl; advancing the shared
    watermark past it keeps the 5-minute poller from ingesting the
    same message a second time. Uses dm_poll's own lock and state
    helpers, so listener and poller serialize on the same flock and
    write the same file shape. Forward only: a late or redelivered
    event never moves the watermark backward. Returns True when the
    watermark moved.
    """
    if not conv_id or not ts:
        return False
    try:
        ts_val = float(ts)
    except (TypeError, ValueError):
        return False
    with dm_poll.dm_lock():
        state = dm_poll.load_dm_state()
        entry = state.get(conv_id)
        raw = entry.get("watermark") if isinstance(entry, dict) else entry
        try:
            current = float(raw or 0)
        except (TypeError, ValueError):
            current = 0.0
        if ts_val <= current:
            return False
        new_entry = dict(entry) if isinstance(entry, dict) else {}
        new_entry["watermark"] = f"{ts_val:.6f}"
        new_entry["updated_at"] = datetime.now(timezone.utc).isoformat()
        state[conv_id] = new_entry
        dm_poll.save_dm_state(state)
        return True


def handle_user_dm_event(payload, event, is_duplicate, mark_seen):
    """Route one user DM event to the DM inbox; returns the outcome.

    is_duplicate(event_id, ts_key) -> bool and mark_seen(*keys) are
    the listener's existing seen-set discipline (refresh from disk,
    check, remember), injected so this stays testable without a
    socket. Outcomes: "skipped_subtype", "skipped_own", "duplicate",
    "appended". The record is appended BEFORE the watermark moves,
    mirroring dm_poll's at-least-once order: a crash in between is
    absorbed by the shared seen set / watermark check, never by
    losing the message.
    """
    if event.get("subtype") in SKIP_SUBTYPES:
        return "skipped_subtype"
    if event.get("user") == ATA_USER_ID:
        # あた's own messages advance the watermark when the poller
        # sees them in history; a push must not append or advance.
        return "skipped_own"
    event_id = (payload or {}).get("event_id")
    ts_key = f"ts:{event.get('channel')}:{event.get('ts')}"
    if is_duplicate(event_id, ts_key):
        return "duplicate"
    record = dm_record_from_event(
        event, datetime.now(timezone.utc).isoformat())
    append_dm_inbox(record)
    mark_seen(event_id, ts_key)
    advance_dm_watermark(event.get("channel"), event.get("ts"))
    return "appended"


def write_heartbeat(client):
    try:
        with open(HEARTBEAT_PATH, "w", encoding="utf-8") as f:
            json.dump(
                {"at": datetime.now(timezone.utc).isoformat(),
                 "connected": bool(getattr(client, "is_connected", lambda: False)())},
                f,
            )
    except Exception:
        pass


def main():
    from slack_sdk.socket_mode import SocketModeClient
    from slack_sdk.socket_mode.response import SocketModeResponse
    from slack_sdk.socket_mode.request import SocketModeRequest
    from slack_sdk import WebClient

    cfg = load_config()
    web_client = WebClient(token=cfg["bot_token"])
    client = SocketModeClient(app_token=cfg["app_token"], web_client=web_client)

    # Identify this bot so its own posts never enter the inbox
    # (prevents the agent from reacting to itself / bot loops).
    own_user_id = None
    own_bot_id = None
    try:
        me = web_client.auth_test()
        own_user_id = me.get("user_id")
        own_bot_id = me.get("bot_id")
    except Exception as e:
        print(f"auth_test failed: {e}", file=sys.stderr, flush=True)

    # SKIP_SUBTYPES is module-level: the user-DM path skips the same
    # subtypes as the main path.
    # Event-ID dedupe (agy review point 2): Slack can redeliver events;
    # remember recent event_ids plus channel:ts keys on disk so a restart
    # does not re-admit duplicates.
    # Seen file with .bak fallback (seen_state.py): a corrupt or
    # deleted main file recovers from the backup copy instead of
    # silently starting empty.
    seen_order = seen_state.load_seen(SEEN_PATH)[-SEEN_MAX:]
    seen_set = set(seen_order)

    def remember(*keys):
        changed = False
        for key in keys:
            if key and key not in seen_set:
                seen_set.add(key)
                seen_order.append(key)
                changed = True
        if not changed:
            return
        while len(seen_order) > SEEN_MAX:
            seen_set.discard(seen_order.pop(0))
        try:
            # Atomic write that also keeps the previous good file as
            # .bak (seen_state.py).
            seen_state.save_seen(seen_order, SEEN_PATH)
        except Exception:
            pass

    def refresh_seen_from_disk():
        # backfill.py also writes the seen file while this listener is
        # running (it admits messages the listener missed). Merge any
        # keys it added, otherwise a later Slack redelivery of a
        # backfilled message would be admitted a second time under its
        # event_id (observed 2026-11-12: same ts in the inbox twice,
        # once with event_id null from backfill, once with an event_id).
        try:
            disk = seen_state.load_seen(SEEN_PATH)
        except Exception:
            return
        for key in disk:
            if key not in seen_set:
                seen_set.add(key)
                seen_order.append(key)
        while len(seen_order) > SEEN_MAX:
            seen_set.discard(seen_order.pop(0))

    def seen_contains(event_id, ts_key):
        # The main path's dedupe discipline as a predicate, for the
        # user-DM path: refresh from disk first (backfill may have
        # admitted the message), then check both keys.
        refresh_seen_from_disk()
        return bool((event_id and event_id in seen_set)
                    or ts_key in seen_set)

    def process(client, req: SocketModeRequest):
        # Always ack first so Slack does not retry the envelope.
        client.send_socket_mode_response(
            SocketModeResponse(envelope_id=req.envelope_id)
        )
        if req.type != "events_api":
            return
        event = (req.payload or {}).get("event") or {}
        if event.get("type") != "message":
            return
        # User DM events (DMs addressed to あた, delivered on behalf
        # of the authorizing user) belong to the DM inbox and the DM
        # reporting flow, never to the main inbox: the main hook
        # would treat them as channel traffic and could auto-reply
        # as the bot in あた's private conversations.
        if is_user_dm_event(req.payload, event):
            handle_user_dm_event(req.payload, event,
                                 seen_contains, remember)
            return
        if event.get("subtype") in SKIP_SUBTYPES:
            return
        if own_user_id and event.get("user") == own_user_id:
            return
        if own_bot_id and event.get("bot_id") == own_bot_id:
            return
        event_id = (req.payload or {}).get("event_id")
        ts_key = f"ts:{event.get('channel')}:{event.get('ts')}"
        refresh_seen_from_disk()
        if (event_id and event_id in seen_set) or ts_key in seen_set:
            return
        record = {
            "event_id": event_id,
            "received_at": datetime.now(timezone.utc).isoformat(),
            "channel": event.get("channel"),
            "channel_type": event.get("channel_type"),
            "user": event.get("user"),
            "bot_id": event.get("bot_id"),
            "app_id": event.get("app_id"),
            "text": event.get("text"),
            "ts": event.get("ts"),
            "thread_ts": event.get("thread_ts"),
            "subtype": event.get("subtype"),
        }
        # Edited/deleted messages carry the inner message; keep its text too.
        inner = event.get("message") or {}
        if inner:
            record["inner_text"] = inner.get("text")
            record["inner_user"] = inner.get("user")
        append_inbox(record)
        remember(event_id, ts_key)

    client.socket_mode_request_listeners.append(process)
    client.connect()
    print("listener connected (Socket Mode)", flush=True)
    try:
        while True:
            time.sleep(15)
            write_heartbeat(client)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
