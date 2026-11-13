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

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config.json")
INBOX_PATH = os.path.join(BASE, "events", "inbox.jsonl")
HEARTBEAT_PATH = os.path.join(BASE, "events", "heartbeat.json")
SEEN_PATH = os.path.join(BASE, "events", "seen_event_ids.json")
SEEN_MAX = 1000


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

    SKIP_SUBTYPES = {"message_changed", "message_deleted", "message_replied"}

    # Event-ID dedupe (agy review point 2): Slack can redeliver events;
    # remember recent event_ids plus channel:ts keys on disk so a restart
    # does not re-admit duplicates.
    seen_order = []
    seen_set = set()
    try:
        with open(SEEN_PATH, "r", encoding="utf-8") as f:
            seen_order = [str(x) for x in json.load(f)][-SEEN_MAX:]
            seen_set = set(seen_order)
    except Exception:
        seen_order, seen_set = [], set()

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
            with open(SEEN_PATH, "w", encoding="utf-8") as f:
                json.dump(seen_order, f)
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
            with open(SEEN_PATH, "r", encoding="utf-8") as f:
                disk = [str(x) for x in json.load(f)]
        except Exception:
            return
        for key in disk:
            if key not in seen_set:
                seen_set.add(key)
                seen_order.append(key)
        while len(seen_order) > SEEN_MAX:
            seen_set.discard(seen_order.pop(0))

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
