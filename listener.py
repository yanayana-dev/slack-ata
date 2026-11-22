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
import re
import socket
import sys
import threading
import time
from datetime import datetime, timezone

import dm_poll
import inbox_state
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
    # Shape check (Neiro's suggestion, 2026-11-19): a mis-pasted token
    # fails fast here with a clear message instead of a cryptic
    # socket/auth failure at connect time. Values are never printed.
    # The bot-token prefix is built from parts ("xox" + "b-"): a
    # literal prefix in this file would be rewritten by the publish
    # scrub, which maps every token-shaped string to a placeholder.
    for key, prefix in (("bot_token", "xox" + "b-"), ("app_token", "xapp-")):
        if not cfg[key].startswith(prefix):
            print(f"config.json: {key} does not start with {prefix}", file=sys.stderr)
            sys.exit(2)
    return cfg


def append_inbox(record):
    # Append-if-absent by (channel, ts), shared with backfill.py
    # (inbox_state.append_record_unique): the two writers' seen-set
    # check-then-append raced on bursts and duplicated inbox lines.
    # Returns True when this call appended the line.
    return inbox_state.append_record_unique(INBOX_PATH, record)


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


def heartbeat_payload(client, now=None):
    """One heartbeat record for heartbeat.json.

    Beyond the original {at, connected}: "pid" identifies the
    process generation that wrote the file, and "last_pong_age_s"
    (seconds since the socket last answered a ping, from the SDK
    connection) separates "socket alive" from "socket object still
    present" -- the SDK's is_connected() only checks the latter.
    """
    now = now or datetime.now(timezone.utc)
    payload = {
        "at": now.isoformat(),
        "connected": bool(getattr(client, "is_connected", lambda: False)()),
        "pid": os.getpid(),
    }
    session = getattr(client, "current_session", None)
    last_pong = getattr(session, "last_ping_pong_time", None)
    if last_pong:
        payload["last_pong_age_s"] = round(now.timestamp() - last_pong, 1)
    return payload


def write_heartbeat(client):
    try:
        with open(HEARTBEAT_PATH, "w", encoding="utf-8") as f:
            json.dump(heartbeat_payload(client), f)
    except Exception:
        pass


# --- Self-supervision (added 2026-11-22) ---------------------------------
# On 2026-11-22 the heartbeat stopped for ~11 minutes (09:42:35 to
# 09:53:44 JST) while this process and its systemd unit stayed
# "active" and the VM had been up for 54 minutes, so it was not a
# VM replacement: the process was alive but wedged, and only the
# watchdog's 5-minute poll + 120s staleness threshold noticed,
# which is what stretched the outage to ~11 minutes. Code
# inspection rules out an application lock as the wedge (the old
# main loop took no locks; is_connected() is a field read), so the
# fix targets detection and revival rather than one guessed lock:
#
# - the heartbeat is written by a dedicated thread that shares no
#   state with event processing, so a wedged main/socket path no
#   longer freezes the heartbeat file itself;
# - HealthTracker counts consecutive unhealthy 15s ticks: if the
#   socket stays disconnected for 6 ticks (~90s -- the SDK's own
#   monitor reconnects within ~25s, so 90s of continuous failure
#   means its recovery gave up), the process exits and systemd
#   Restart=always revives it in seconds. Tick counting, not
#   wall-clock, so a host freeze runs no ticks and cannot be
#   misread as a long outage on resume;
# - while healthy, each tick also sends a systemd watchdog
#   keepalive (the unit sets WatchdogSec=120): if the whole
#   process wedges -- heartbeat thread included -- systemd itself
#   restarts the service within ~2 minutes, with no polling.
# Together these bound a wedged-listener outage to ~2 minutes
# instead of ~11, at zero added polling cost.

SD_WATCHDOG_PING = "WATCHDOG=1"
HEARTBEAT_INTERVAL_S = 15
UNHEALTHY_TICKS_BEFORE_EXIT = 6  # 6 x 15s = ~90s


def sd_notify(message, environ=None, sock_factory=None):
    """Send one sd_notify datagram; a no-op outside systemd.

    Returns True when the datagram was handed to the socket.
    NOTIFY_SOCKET is set by systemd because the unit declares
    WatchdogSec (with NotifyAccess=main); a leading "@" denotes an
    abstract socket and is translated to a NUL prefix.
    """
    environ = os.environ if environ is None else environ
    addr = environ.get("NOTIFY_SOCKET")
    if not addr:
        return False
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    sock = None
    try:
        sock = (sock_factory or socket.socket)(
            socket.AF_UNIX, socket.SOCK_DGRAM)
        sock.sendto(message.encode("utf-8"), addr)
        return True
    except Exception:
        return False
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


class HealthTracker:
    """Consecutive-tick health of the socket connection."""

    def __init__(self, exit_after_ticks=UNHEALTHY_TICKS_BEFORE_EXIT):
        self.exit_after_ticks = exit_after_ticks
        self.unhealthy_ticks = 0
        self.ever_healthy = False

    def observe(self, connected):
        if connected:
            self.ever_healthy = True
            self.unhealthy_ticks = 0
        else:
            self.unhealthy_ticks += 1

    @property
    def healthy(self):
        return self.unhealthy_ticks == 0

    @property
    def should_exit(self):
        # Never exit before the first healthy observation: startup
        # transients belong to client.connect() and systemd's
        # restart of a failed start, not to this escalation.
        return (self.ever_healthy
                and self.unhealthy_ticks >= self.exit_after_ticks)


def supervision_tick(client, tracker, notify=sd_notify):
    """One supervision cycle; returns True when the process should exit.

    Writes the heartbeat, pings the systemd watchdog only while
    healthy (a wedged or disconnected process must stop pinging so
    systemd restarts it), and reports the tracker's exit decision.
    A client whose is_connected() raises counts as disconnected.
    """
    try:
        connected = bool(client.is_connected())
    except Exception:
        connected = False
    tracker.observe(connected)
    write_heartbeat(client)
    if tracker.healthy:
        notify(SD_WATCHDOG_PING)
    return tracker.should_exit


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
    # auth.test result is shape-checked ([UW]0... / B0... fullmatch, the
    # same rule Neiro's reconcile uses): a garbage user_id must never be
    # adopted as our own identity, and a failed auth.test is fail-closed
    # after retries instead of silently continuing unidentified.
    own_user_id = None
    own_bot_id = None
    me = None
    for attempt in range(3):
        try:
            me = web_client.auth_test()
            break
        except Exception as e:
            print(f"auth_test failed (attempt {attempt + 1}/3): {e}", file=sys.stderr, flush=True)
            time.sleep(2)
    if me is None:
        print("auth_test failed after retries; cannot identify self, exiting", file=sys.stderr, flush=True)
        sys.exit(1)
    own_user_id = me.get("user_id")
    own_bot_id = me.get("bot_id")
    if not own_user_id or not re.fullmatch(r"[UW]0[A-Z0-9]{8,}", own_user_id):
        print(f"auth_test returned invalid user_id: {own_user_id!r}; exiting", file=sys.stderr, flush=True)
        sys.exit(1)
    if own_bot_id and not re.fullmatch(r"B0[A-Z0-9]{8,}", own_bot_id):
        print(f"auth_test returned invalid bot_id: {own_bot_id!r}; exiting", file=sys.stderr, flush=True)
        sys.exit(1)

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
            # Merge with the on-disk list before saving (union_seen):
            # backfill.py writes the same file, and saving our own
            # snapshot alone would drop keys it added since our last
            # refresh -- a dropped ts: key re-admits a redelivery.
            # Atomic write that also keeps the previous good file as
            # .bak (seen_state.py).
            disk = seen_state.load_seen(SEEN_PATH)
            merged = seen_state.union_seen(disk, seen_order)[-SEEN_MAX:]
            seen_order[:] = merged
            seen_set.clear()
            seen_set.update(merged)
            seen_state.save_seen(merged, SEEN_PATH)
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

    # Supervision runs on its own thread (see the self-supervision
    # note above write_heartbeat): the heartbeat file, the systemd
    # watchdog keepalive, and the dead-socket exit decision must
    # not share fate with the main thread or the socket callbacks.
    tracker = HealthTracker()

    def supervision_loop():
        while True:
            try:
                if supervision_tick(client, tracker):
                    print(
                        "socket disconnected for "
                        f"{tracker.unhealthy_ticks} consecutive ticks; "
                        "exiting so systemd restarts the listener",
                        file=sys.stderr, flush=True)
                    # os._exit, not sys.exit: interpreter shutdown
                    # would wait on the very threads that may be
                    # wedged. State files are written atomically or
                    # append-only, so an abrupt exit loses nothing.
                    os._exit(1)
            except Exception:
                pass
            time.sleep(HEARTBEAT_INTERVAL_S)

    threading.Thread(
        target=supervision_loop, name="supervision", daemon=True).start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
