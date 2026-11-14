#!/usr/bin/env python3
"""Poll あた's own Slack DMs through the user-token connector helper.

User tokens get no event stream, so the bot listener (Socket Mode,
bot token) can never see DMs addressed to あた themself. This poller
closes that gap: it asks the custom.slack-user-token connector helper
(~/workspace/custom-connectors/custom.slack-user-token/bin/
slack_user_api.py, which authenticates as あた, user U0EXAMPLEOWNER)
for the im/mpim conversation list and for each conversation's history
since a per-conversation watermark, and appends new messages from
other people to events/dm_inbox.jsonl in the inbox.jsonl record
shape (plus source "dm_poll" and the conversation id).

State: events/dm_state.json -- flat dict keyed by conversation id,
{"watermark": <latest seen ts>, "updated_at": <iso>}. Written
atomically (tmp+rename) and serialized with flock on
events/dm_state.lock, mirroring msgstate.py: the whole poll (state
load -> helper reads -> inbox append -> state save) runs under one
exclusive lock, so two overlapping poll runs can never double-append.
Inbox lines are appended BEFORE the state save inside the lock, so a
crash between the two re-delivers on the next run (at-least-once)
instead of losing messages; downstream dedupe by channel:ts (the
msgstate key fallback) absorbs the duplicate.

First run for a conversation seeds the watermark to its current
latest message ts (or "0.000000" when the conversation is empty) and
appends nothing: old history is never replayed. Later runs fetch
with --oldest = watermark - LOOKBACK_SECS (Slack's oldest is
exclusive; the margin covers boundary/clock skew, like reply_once's
windowed read) and keep only messages with ts strictly greater than
the watermark. A watermark of 0 fetches without --oldest instead:
Slack rejects oldest=0 as invalid_ts_oldest. Messages from あた themself (user == ATA_USER) are
never appended, but they DO advance the watermark -- the watermark
always moves to the newest ts seen in the fetched page, whoever
wrote it. One page of HISTORY_LIMIT messages per conversation per
run; a burst larger than that between polls would skip the middle
(accepted: DM volume is a handful of messages a day, and the newest
page is what matters).

A failure for one conversation (helper error, ok:false) is counted
and reported, its watermark is left untouched, and the run continues
with the others. Exit is non-zero only when every conversation
failed (or the conversation list itself could not be read).

Usage:
  dm_poll.py [--helper PATH]   # one poll; prints a one-line summary

Helper resolution: --helper > $DM_POLL_HELPER > the connector helper
above. The token never passes through this script: the helper holds
the credential via the connector's surrogate exchange.
"""
import fcntl
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timezone

BASE = os.environ.get("SLACK_LISTENER_BASE") or os.path.dirname(os.path.abspath(__file__))
EVENTS = os.path.join(BASE, "events")
DM_STATE = os.path.join(EVENTS, "dm_state.json")
DM_LOCK = os.path.join(EVENTS, "dm_state.lock")
DM_INBOX = os.path.join(EVENTS, "dm_inbox.jsonl")

DEFAULT_HELPER = os.path.expanduser(
    "/path/to/slack-user-helper/slack_user_api.py")

ATA_USER = "U0EXAMPLEOWNER"  # あた's own Slack user id in muse取締役会

# Seconds before the watermark a history fetch starts (Slack's
# oldest is exclusive, so the margin only re-reads a small overlap;
# the strict ts > watermark filter decides what is new).
LOOKBACK_SECS = 60
# One page per conversation per run (see module docstring).
HISTORY_LIMIT = 200
LIST_LIMIT = 200
HELPER_TIMEOUT = 90


@contextmanager
def dm_lock():
    """Hold the exclusive flock for one whole poll run.

    Same shape as msgstate.state_lock: flock is released if the
    holder dies, so a crashed poller never leaves a stale lock.
    """
    os.makedirs(EVENTS, exist_ok=True)
    fd = os.open(DM_LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def load_dm_state():
    try:
        with open(DM_STATE, "r", encoding="utf-8") as f:
            state = json.load(f)
        return state if isinstance(state, dict) else {}
    except Exception:
        return {}


def save_dm_state(state):
    os.makedirs(EVENTS, exist_ok=True)
    tmp = DM_STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, DM_STATE)


def run_helper(helper, args):
    """Run one helper subcommand and return its parsed JSON stdout.

    Raises RuntimeError on a non-zero exit, non-JSON output, or a
    Slack ok:false body -- the caller treats all three as "this call
    failed" and moves on (or aborts, for the conversation list).
    """
    r = subprocess.run([sys.executable, helper] + args,
                       capture_output=True, text=True, timeout=HELPER_TIMEOUT)
    if r.returncode != 0:
        raise RuntimeError(
            f"helper {' '.join(args[:1])} exited {r.returncode}: "
            f"{r.stderr.strip()[:200]}")
    try:
        data = json.loads(r.stdout)
    except Exception:
        raise RuntimeError(
            f"helper {' '.join(args[:1])} gave non-JSON output: "
            f"{r.stdout.strip()[:200]}")
    if isinstance(data, dict) and data.get("ok") is False:
        raise RuntimeError(
            f"helper {' '.join(args[:1])} ok:false: {data.get('error')}")
    return data


def list_conversations(helper):
    data = run_helper(helper, ["conversations-list", "--types", "im,mpim",
                               "--limit", str(LIST_LIMIT)])
    convs = []
    for ch in data.get("channels", []):
        if ch.get("is_im"):
            ctype = "im"
        elif ch.get("is_mpim"):
            ctype = "mpim"
        else:
            continue  # helper was asked for im,mpim only; stay strict
        if ch.get("id"):
            convs.append({"id": ch["id"], "channel_type": ctype})
    return convs


def fetch_history(helper, conv_id, oldest=None, limit=HISTORY_LIMIT):
    args = ["history", "--channel", conv_id, "--limit", str(limit)]
    if oldest is not None:
        args += ["--oldest", oldest]
    data = run_helper(helper, args)
    return data.get("messages", [])


def _ts(msg):
    try:
        return float(msg.get("ts") or 0)
    except (TypeError, ValueError):
        return 0.0


def to_record(msg, conv, received_at):
    """One dm_inbox.jsonl line, mirroring the inbox.jsonl shape."""
    return {
        "received_at": received_at,
        "source": "dm_poll",
        "channel": conv["id"],
        "conversation": conv["id"],
        "channel_type": conv["channel_type"],
        "user": msg.get("user"),
        "bot_id": msg.get("bot_id"),
        "app_id": msg.get("app_id"),
        "text": msg.get("text") or "",
        "ts": msg.get("ts"),
        "thread_ts": msg.get("thread_ts"),
        "subtype": msg.get("subtype"),
    }


def poll_conversation(helper, state, conv, received_at):
    """Poll one conversation; returns (records, error_or_None).

    On success the conversation's watermark in `state` is advanced
    in place (the caller saves once, after appending). On error the
    state entry is untouched, so the next run retries the same span.
    """
    cid = conv["id"]
    entry = state.get(cid)
    if entry is None:
        # First ever run: seed the watermark at the current latest
        # message and append nothing (never replay old history).
        try:
            msgs = fetch_history(helper, cid, limit=1)
        except Exception as e:
            return [], str(e)
        latest = max((_ts(m) for m in msgs), default=0.0)
        state[cid] = {"watermark": f"{latest:.6f}", "updated_at": received_at}
        return [], None

    watermark = float(entry.get("watermark") or 0)
    # Slack rejects oldest=0 (invalid_ts_oldest, verified live
    # 2026-11-14 on the empty conversations seeded at watermark 0),
    # so a watermark within the lookback of the epoch means "fetch
    # the latest page unfiltered" -- the strict ts > watermark
    # filter below decides what is new either way.
    oldest = (None if watermark <= LOOKBACK_SECS
              else f"{watermark - LOOKBACK_SECS:.6f}")
    try:
        msgs = fetch_history(helper, cid, oldest=oldest)
    except Exception as e:
        return [], str(e)
    fresh = [m for m in msgs if _ts(m) > watermark]
    if fresh:
        newest = max(_ts(m) for m in fresh)
        entry["watermark"] = f"{newest:.6f}"
        entry["updated_at"] = received_at
    records = [to_record(m, conv, received_at)
               for m in sorted(fresh, key=_ts)
               if m.get("user") != ATA_USER and m.get("ts")]
    return records, None


def poll(helper):
    """One full poll. Returns (stats, exit_code).

    stats: {"checked": int, "appended": int, "failed": int,
            "errors": {conv_id: message}}. Exit code is 1 only when
    the conversation list failed or every conversation failed.
    """
    stats = {"checked": 0, "appended": 0, "failed": 0, "errors": {}}
    with dm_lock():
        state = load_dm_state()
        try:
            convs = list_conversations(helper)
        except Exception as e:
            stats["errors"]["<list>"] = str(e)
            return stats, 1
        received_at = datetime.now(timezone.utc).isoformat()
        records = []
        for conv in convs:
            stats["checked"] += 1
            recs, err = poll_conversation(helper, state, conv, received_at)
            if err is not None:
                stats["failed"] += 1
                stats["errors"][conv["id"]] = err
                continue
            records.extend(recs)
        if records:
            os.makedirs(EVENTS, exist_ok=True)
            with open(DM_INBOX, "a", encoding="utf-8") as f:
                for rec in records:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            stats["appended"] = len(records)
        save_dm_state(state)
    if stats["checked"] and stats["failed"] == stats["checked"]:
        return stats, 1
    return stats, 0


def main(argv):
    helper = (os.environ.get("DM_POLL_HELPER") or DEFAULT_HELPER)
    i = 0
    while i < len(argv):
        if argv[i] == "--helper" and i + 1 < len(argv):
            helper = argv[i + 1]
            i += 2
        else:
            print(f"unknown argument: {argv[i]}", file=sys.stderr)
            return 2
    stats, code = poll(helper)
    for cid, err in stats["errors"].items():
        print(f"dm_poll: {cid}: {err}", file=sys.stderr)
    print(f"dm_poll: conversations={stats['checked']} "
          f"new={stats['appended']} failed={stats['failed']}")
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
