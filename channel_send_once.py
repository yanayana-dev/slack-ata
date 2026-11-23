#!/usr/bin/env python3
"""Send a Slack channel (parent) post at most once.

Adopted 2026-11-12 from Neiro's share in #slack-へようこそ: Neiro took
Yanayana's proposal (bounded check: look at only the most recent 50
channel messages for the same text) into their slack-send-once.sh.
Until now Yanayana had that protection only for thread replies
(reply_once.py); a parent post sent by a re-executed run could still
duplicate, because nothing checked the channel before sending. This
helper closes that gap, mirroring Neiro's design:

  * Before sending, check the local record (events/channel_posts.jsonl)
    and the live channel: one `slackcli conversations read --limit 50`
    call returns the latest 50 channel messages (ascending within the
    page), and look for an identical Yanayana post among them. If
    found, nothing is sent. (Simplified 2026-11-12 from Yama's
    question in #slack-へようこそ: he pointed out one limit=50 call
    gives the latest 50 directly, and a live check confirmed it --
    limit=20 returned the newest 20, and --oldest past the newest
    returned nothing. The earlier 20-page oldest-first scan was
    unnecessary; "oldest-first" is only the order inside the
    returned page, not the window.)
  * The same live-channel read is also compared against other
    authors' posts: if the outgoing text is identical to another
    author's recent post, refuse to send (parroting guard, exit 4).
    This mirrors reply_once.py's duplicate_of_other for thread
    replies (incident 2026-11-14, where a reply went out identical
    to Yama's post from 21 seconds earlier). Extended to parent
    posts 2026-11-14 from Yama's question in #slack-へようこそ:
    their check was own-replies-only too, and the incident showed
    the draft path can pick up another author's text, so own-only
    is not enough on either side.
  * If the history read fails after retries, refuse to send blind
    (cannot-verify, exit 2). This is where we differ from Neiro on
    purpose: they keep fail-open ("a stuck reply is worse"), we keep
    fail-closed, the same rule as reply_once.py. The 2026-11-12
    fail-open/fail-closed thread settled the criterion behind the
    split: notification-type parent posts (daily summaries etc.) weigh
    the cost of a missed post and go fail-open; discussion-type parent
    posts weigh the cost of a visible duplicate and go fail-closed.
    Our parent posts are discussion-type, so fail-closed stands;
    if we start posting notification-type parents, revisit fail-open.
  * If the send response is lost, run a recovery check on the live
    channel before deciding anything; if delivery cannot be proven,
    record ambiguous_post_outcome and do not repost blindly.

  * Stamping (2026-11-23, Yama's candidate 28, adopted): a parent
    post has no originating event, so it is sent with an
    auto-generated request_id stamp via post_metadata.py (direct
    chat.postMessage with metadata, no thread_ts) instead of going
    out unstamped through slackcli -- the explicit "付けない宣言":
    the same two-choice rule as the thread-reply path (event_id or
    an explicit declaration, never unstamped). The request_id is a
    duplicate-prevention nonce, not a provenance claim, and it is
    recorded in channel_posts.jsonl alongside the post_ts so a
    later reader can tell which stamp proves the post.

Usage:
  channel_send_once.py --channel C0... --message "..."
  channel_send_once.py --channel C0... --message "..." --check-only
    # prints "already-sent" or "not-sent", sends nothing
  channel_send_once.py --prune [--older-than-days N] [--dry-run]
    # drop channel-post records older than N days (default 90),
    # archiving them to events/channel_posts-archive.jsonl;
    # --dry-run only reports the counts, writes nothing

Exit codes: 0 = sent, already sent (no-op), or check completed;
1 = usage error; 2 = send failed / channel could not be read and no
record exists to fall back on (refuses to send blind);
3 = ambiguous_post_outcome: the send response was lost and the
recovery confirmation could not prove delivery -- the outcome is
recorded and the caller must not repost blindly (a later attempt's
pre-send check decides from the live channel / record).
4 = duplicate-text: the outgoing message is identical to another
author's existing post in the channel (parroting guard, added
2026-11-14); nothing was sent.
"""
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
SLACKCLI = os.path.join(BASE, "bin", "slackcli")
EVENTS = os.path.join(BASE, "events")
POSTS = os.path.join(EVENTS, "channel_posts.jsonl")
AMBIGUOUS = os.path.join(EVENTS, "ambiguous_channel_posts.jsonl")

YANAYANA_USER = "U0EXAMPLEBOT"  # Yanayana bot user id in muse取締役会

BOUNDED = 50      # only the most recent 50 channel messages are checked
                  # (one slackcli read with --limit 50 returns exactly them)

# Ledger retention (2026-11-12, from Yama's question in #slack-へようこそ
# about rotating old ledger entries): same rationale as replylog.py --
# a duplicate post is only ever attempted by a re-executed run within
# hours/days of the original send, so 90 days is far beyond any
# re-run window. Entries without a parseable timestamp are kept.
DEFAULT_RETENTION_DAYS = 90


def _parse_ts(value):
    try:
        dt = datetime.fromisoformat(str(value))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _archive_path(path):
    """Sibling archive file for pruned records (e.g. channel_posts-archive.jsonl).

    Adopted 2026-11-12 from Neiro's share in #slack-へようこそ: they
    move pruned ledger rows to slack-send-log-archive.jsonl instead of
    deleting them outright, so nothing is lost for good.
    """
    if path.endswith(".jsonl"):
        return path[: -len(".jsonl")] + "-archive.jsonl"
    return path + "-archive"


def prune_record(path=None, days=DEFAULT_RETENTION_DAYS, now=None, dry_run=False):
    """Drop channel-post records older than `days`. Returns (pruned, kept).

    Same fail-safe as replylog.prune_log: entries without a parseable
    `at` timestamp are kept, malformed lines are dropped. Pruned
    records are appended to the sibling archive file (see
    _archive_path) instead of being deleted outright. With
    dry_run=True nothing is written, only the counts are returned
    (adopted 2026-11-12 from Neiro's share in #slack-へようこそ).
    """
    path = path or POSTS
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    try:
        with open(path, "r", encoding="utf-8") as f:
            entries = []
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except Exception:
                        continue
    except FileNotFoundError:
        return 0, 0
    kept = [e for e in entries
            if (ts := _parse_ts(e.get("at"))) is None or ts >= cutoff]
    dropped = [e for e in entries
               if (ts := _parse_ts(e.get("at"))) is not None and ts < cutoff]
    pruned = len(entries) - len(kept)
    if pruned and not dry_run:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for e in kept:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        os.replace(tmp, path)
        if dropped:
            archive = _archive_path(path)
            os.makedirs(os.path.dirname(archive), exist_ok=True)
            with open(archive, "a", encoding="utf-8") as f:
                for e in dropped:
                    f.write(json.dumps(e, ensure_ascii=False) + "\n")
    return pruned, len(kept)


def load_record():
    """Read the channel-post ledger.

    One corrupt line must not discard the whole ledger: bad lines
    are skipped and good records stay. A file that cannot be read
    at all raises (missing is fine -> []); callers treat that as
    cannot-verify. Same lesson as Neiro's 中-1 (2026-11-21): an
    exception while reading a dedupe ledger must not read as
    "no duplicate" and let a send proceed.
    """
    try:
        with open(POSTS, "r", encoding="utf-8") as f:
            out = []
            for lineno, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    # 2026-11-21, Yama's question in #slack-へようこそ
                    # (same point as for reply_once.load_record):
                    # a skipped corrupt line used to vanish silently,
                    # so ledger loss went unnoticed. Log the skip
                    # (line number + truncated content) to stderr;
                    # good lines still load.
                    print(f"load_record: skipping corrupt line {lineno} "
                          f"in {POSTS}: {line[:120]!r}", file=sys.stderr)
                    # 2026-11-22, Yama's proposal (same thread): note
                    # the sighting for the daily summary (ledger_health),
                    # never a dedicated log file. Lazy import so a
                    # missing/broken helper can never break the load.
                    try:
                        if BASE not in sys.path:
                            sys.path.insert(0, BASE)
                        from ledger_health import note_corrupt_line
                        note_corrupt_line(POSTS, lineno)
                    except Exception:
                        pass
                    continue
            return out
    except FileNotFoundError:
        return []


def in_record(message):
    return any(r.get("text") == message for r in load_record())


def channel_read_page(channel):
    """One read of the channel history: the latest BOUNDED messages.

    slackcli `conversations read --limit N` returns the latest N
    messages, ordered oldest-first within the page (verified live
    2026-11-12: --limit 20 returned the newest 20, and --oldest past
    the newest message returned none). So a single call is the whole
    bounded window -- no paging to the end of history is needed.
    """
    cmd = [SLACKCLI, "conversations", "read", channel,
           "--limit", str(BOUNDED), "--json"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    out = r.stdout
    i = out.find("{")
    if i < 0:
        raise RuntimeError(f"slackcli channel read gave no JSON: {r.stderr.strip()[:200]}")
    return json.loads(out[i:])


def channel_read_page_retry(channel, attempts=3):
    """channel_read_page with retries (same rationale as reply_once.py:
    a failed read is often transient, so retry before concluding
    cannot-verify; fail-closed is kept)."""
    last_exc = None
    for n in range(attempts):
        try:
            return channel_read_page(channel)
        except Exception as e:
            last_exc = e
            if n + 1 < attempts:
                time.sleep(2)
    raise last_exc


def channel_last_yanayana_texts(channel):
    """Texts of Yanayana's posts in the live channel, last BOUNDED only.

    A single read already returns exactly the latest BOUNDED messages,
    so there is no tail to keep: a duplicate of a just-sent post would
    be among them, and older history is irrelevant -- that is the
    bounded check Neiro adopted.
    """
    data = channel_read_page_retry(channel)
    return [m.get("text") or "" for m in data.get("messages", [])
            if m.get("user") == YANAYANA_USER]


def channel_duplicate_of_other(channel, message):
    """Ts of another author's channel message whose text is identical
    to the outgoing message, else None.

    Parroting guard for parent posts, mirroring reply_once.py's
    duplicate_of_other (incident 2026-11-14): a reply went out
    identical to Yama's post from 21 seconds earlier, because the
    draft path had picked up the newest post's text. The thread
    side already compares the outgoing text against other authors'
    posts, but this channel side only looked at Yanayana's own
    posts, so the same accident could still go out as a parent
    post. The comparison is exact after stripping surrounding
    whitespace, over the latest BOUNDED channel messages.
    """
    if not message or not message.strip():
        return None
    wanted = message.strip()
    data = channel_read_page_retry(channel)
    for m in data.get("messages", []):
        if m.get("user") == YANAYANA_USER:
            continue
        if (m.get("text") or "").strip() == wanted:
            return m.get("ts")
    return None


def recovery_check(channel, message, attempts=3):
    """After a lost send response, re-read the channel to see whether
    the post actually landed. Returns True (found), False (reads
    succeeded and the text is absent -- confirmed not delivered), or
    None (reads themselves failed -- could not verify)."""
    for n in range(attempts):
        try:
            return message in channel_last_yanayana_texts(channel)
        except Exception as e:
            print(f"warning: recovery check attempt {n + 1} failed ({e})",
                  file=sys.stderr)
            if n + 1 < attempts:
                time.sleep(2)
    return None


def record_post(channel, message, post_ts, recovered=False,
                request_id=None):
    entry = {
        "channel": channel, "text": message, "post_ts": post_ts,
        "recovered": recovered,
        "at": datetime.now(timezone.utc).isoformat(),
    }
    if request_id is not None:
        # The explicit no-event declaration for this post (2026-11-23
        # candidate 28): the request_id stamp it carries, so the
        # ledger proves the post was sent stamped, not unstamped.
        entry["request_id"] = request_id
    os.makedirs(EVENTS, exist_ok=True)
    with open(POSTS, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def record_ambiguous(channel, message, reason):
    os.makedirs(EVENTS, exist_ok=True)
    with open(AMBIGUOUS, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "outcome": "ambiguous_post_outcome",
            "kind": "channel",
            "channel": channel, "text": message, "reason": reason,
            "at": datetime.now(timezone.utc).isoformat(),
        }, ensure_ascii=False) + "\n")


def already_sent(channel, message):
    try:
        if in_record(message):
            return "record"
    except Exception as e:
        # Ledger unreadable: cannot verify, refuse to send blind
        # rather than risk a duplicate parent post.
        print(f"warning: record check failed ({e}); cannot verify",
              file=sys.stderr)
        return "unknown"
    try:
        if message in channel_last_yanayana_texts(channel):
            return "channel"
    except Exception as e:
        # Channel read failed: fall back to the record only, and say so.
        # If the record has no proof we cannot verify; refuse to send
        # blind rather than risk a duplicate.
        print(f"warning: channel check failed ({e}); record-only check", file=sys.stderr)
        return "unknown"
    return None


def send(channel, message):
    """Send a parent post, stamped with an auto-generated request_id.

    Returns (post_ts, request_id). A parent post has no originating
    event, so the stamp is the explicit "付けない宣言" (2026-11-23
    candidate 28, adopted): the post goes out through
    post_metadata.py (direct chat.postMessage with metadata, no
    thread_ts) instead of slackcli's unstamped `messages send`, so
    no send through this helper is ever unstamped. The request_id
    is a duplicate-prevention nonce, not a provenance claim -- an
    event_id is never invented. Raises RuntimeError when the send
    fails, so the caller runs the same recovery flow as before
    (a lost response may mean the post landed; never repost blind).
    The import is lazy: this module is loaded by tests via
    importlib without its directory on sys.path, and tests stub
    send() outright, so the import must only happen on the real
    send path (same pattern as the lazy ledger_health import in
    load_record).
    """
    import post_metadata  # lazy, see docstring
    request_id = str(uuid.uuid4())
    ok, result = post_metadata.send_reply(
        channel, None, message, request_id=request_id)
    if not ok:
        raise RuntimeError(f"post_metadata send failed: {result}")
    ts = result.get("ts") if isinstance(result, dict) else None
    return ts, request_id


def main(argv):
    channel = message = None
    check_only = False
    prune = False
    prune_days = DEFAULT_RETENTION_DAYS
    prune_dry_run = False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--channel" and i + 1 < len(argv):
            channel = argv[i + 1]; i += 2
        elif a == "--message" and i + 1 < len(argv):
            message = argv[i + 1]; i += 2
        elif a == "--check-only":
            check_only = True; i += 1
        elif a == "--prune":
            prune = True; i += 1
        elif a == "--dry-run":
            prune_dry_run = True; i += 1
        elif a == "--older-than-days" and i + 1 < len(argv):
            try:
                prune_days = int(argv[i + 1])
            except ValueError:
                print(f"invalid --older-than-days: {argv[i + 1]}", file=sys.stderr)
                return 1
            i += 2
        else:
            print(f"unknown argument: {a}", file=sys.stderr)
            return 1
    if prune:
        pruned, kept = prune_record(days=prune_days, dry_run=prune_dry_run)
        prefix = "dry-run " if prune_dry_run else ""
        print(f"{prefix}pruned {pruned} kept {kept}")
        return 0
    if not channel or (not check_only and not message):
        print("usage: channel_send_once.py --channel C... "
              "[--message TEXT] [--check-only]", file=sys.stderr)
        return 1

    found = already_sent(channel, message)
    if found == "unknown":
        print("cannot-verify: channel read failed and no record entry; "
              "refusing to send", file=sys.stderr)
        return 2
    if found:
        print(f"already-sent ({found})")
        return 0
    if check_only:
        print("not-sent")
        return 0

    try:
        dup_ts = channel_duplicate_of_other(channel, message)
    except Exception as e:
        # Same fail-closed stance as the already-sent check: when
        # the channel cannot be read, do not send blind.
        print(f"cannot-verify: duplicate-text check failed ({e}); "
              "refusing to send", file=sys.stderr)
        return 2
    if dup_ts:
        print(f"duplicate-text: outgoing message is identical to "
              f"another author's post ({dup_ts}); refusing to send",
              file=sys.stderr)
        return 4

    post_ts = None
    request_id = None
    try:
        post_ts, request_id = send(channel, message)
    except Exception as e:
        # The send response was lost: the post may still have landed.
        # Confirm from the live channel before deciding anything.
        print(f"warning: send failed ({e}); running recovery check",
              file=sys.stderr)
        outcome = recovery_check(channel, message)
        if outcome is True:
            record_post(channel, message, None, recovered=True)
            print("sent (recovered: post found in channel after lost send response)")
            return 0
        reason = ("send-response-lost; recovery check could not verify delivery"
                  if outcome is None else
                  "send-response-lost; recovery check confirmed post absent")
        record_ambiguous(channel, message, reason)
        print(f"ambiguous_post_outcome: {reason}; not reposting blindly",
              file=sys.stderr)
        return 3
    if not post_ts:
        # Send reported success but gave no ts: same recovery flow.
        print("warning: send returned no ts; running recovery check",
              file=sys.stderr)
        outcome = recovery_check(channel, message)
        if outcome is True:
            record_post(channel, message, None, recovered=True)
            print("sent (recovered: post found in channel, send gave no ts)")
            return 0
        reason = ("send gave no ts; recovery check could not verify delivery"
                  if outcome is None else
                  "send gave no ts; recovery check confirmed post absent")
        record_ambiguous(channel, message, reason)
        print(f"ambiguous_post_outcome: {reason}; not reposting blindly",
              file=sys.stderr)
        return 3
    record_post(channel, message, post_ts, request_id=request_id)
    print(f"sent post_ts={post_ts}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
