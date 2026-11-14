#!/usr/bin/env python3
"""Periodically reconcile ambiguous Slack sends against live threads.

Adopted 2026-11-12 from Yama's implementation share in #slack-へようこそ
(their "遅れて着地した返信の定期照合"): reply_once.py already resolves
an ambiguous_post_outcome when a *later* pre-send check for the same
target proves the reply landed, but if no later send to that target
ever happens, the ambiguous record stands forever and the replies
ledger keeps saying "not posted". This sweep re-checks unresolved
ambiguous entries against the live thread on a schedule (the poll hook
runs it; it self-gates to at most once an hour) and, when the exact
reply text is found, records the reply (recovered) and marks the
ambiguous entry resolved -- the same record-reply path a pre-send
check uses. A reply ts consumed by one entry is never reused for
another entry, in the same sweep or a later one (Yama's
assigned_reply_ts idea, adopted 2026-11-14; seeded from replied.jsonl
per Neiro's share 2026-11-14): two entries with identical text cannot resolve
to the same reply.

Only entries that stored the message text can be matched; entries
without one are closed as closed_no_text on first sight (decided
with Yama 2026-11-14: waiting cannot make them matchable, and the
hourly sweep runs unattended so a manual-confirmation step would
mean leaving them unresolved forever; the record stays in the
ledger and a later pre-send check can still resolve the target if
the reply is found). A thread read failure also leaves the entry
unresolved -- delivery is never guessed. A found reply is never reposted; this script only corrects
records. A match also requires the reply to be posted *after* the
target message (msg.ts > event.ts, adopted 2026-11-14 from Yama's
audit share): an identical-text reply from before the event must
never be matched as this event's reply.

Pending resends (DLQ resend, final form agreed with Yama and Neiro
2026-11-13 in #slack-へようこそ): when a reply's pre-send check
(history read) fails with a *transient* error, the reply is not
counted toward the retry budget and is not dead-lettered. Instead the
agent records it here with `record` (events/pending_resends.jsonl),
and this hourly sweep retries it:
  - readable again -> send normally, prefixed with "遅れての再送"
    plus the original failure time; the reply is recorded as sent.
  - the original reply already landed (response lost) -> record the
    found reply as sent (recovered), never repost. Checked BEFORE
    answered_by_other (2026-11-14, Neiro's review): otherwise a
    delivered reply plus someone else's answer would be given up
    without ever reaching replied.jsonl.
  - someone else already answered (a text post after the target, by
    anyone other than the questioner and Yanayana herself, from a
    human -- not a bot -- with more than 10 characters of text;
    narrowed 2026-11-14 from Yama's share: another bot's auto-reply
    or a short acknowledgement must not falsely silence a resend)
    -> give up, record only.
  - 24h pass without a successful read -> give up, record only.
    No +5 minute grace: with an hourly check there are up to 24
    chances inside the window, so the grace the old design needed
    is gone.
  - parent post deleted -> give up, record only.
  - only transient errors are eligible (history_unavailable,
    rate_limited, server_5xx, timeout, connection_error); a send
    failure that exhausts retries stays dead-lettered as before --
    this sweep only rescues pre-check failures.
A send failure inside the sweep leaves the entry pending for the
next hourly pass (until the 24h window expires); delivery is never
guessed and a found reply is never reposted.

Usage:
  reconcile_ambiguous.py [--force] [--interval-secs N]
  reconcile_ambiguous.py record --channel C... --thread-ts T...
      --target-ts M... --questioner U... --error-kind KIND --message TEXT
      [--first-failed-at ISO8601]
Exit 0 on success (including "nothing to do" / "gated"); prints a
JSON summary. State: events/reconcile_ambiguous.state (last run epoch).
SLACK_LISTENER_BASE overrides the base dir (tests).
"""
import fcntl
import importlib.util
import json
import os
import sys
import time
from datetime import datetime, timezone
try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9 fallback
    ZoneInfo = None

BASE = os.environ.get("SLACK_LISTENER_BASE") or os.path.dirname(os.path.abspath(__file__))
EVENTS = os.path.join(BASE, "events")
STATE_FILE = os.path.join(EVENTS, "reconcile_ambiguous.state")
PENDING_RESENDS = os.path.join(EVENTS, "pending_resends.jsonl")
DEFAULT_INTERVAL_SECS = 3600
RESEND_WINDOW_SECS = 24 * 3600  # 24h window from the original failure
# Only transient pre-check failures are eligible for a delayed resend.
TRANSIENT_ERROR_KINDS = {
    "history_unavailable", "rate_limited", "server_5xx",
    "timeout", "connection_error",
}
YANAYANA_USER = "U0EXAMPLEBOT"  # Yanayana bot user id in muse取締役会

_SPEC = importlib.util.spec_from_file_location(
    "reply_once", os.path.join(os.path.dirname(os.path.abspath(__file__)), "reply_once.py"))
ro = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ro)
# Point reply_once's record files at this base (a no-op in production,
# where SLACK_LISTENER_BASE is unset and BASE is the script's own dir).
ro.BASE = BASE
ro.EVENTS = EVENTS
ro.REPLIED = os.path.join(EVENTS, "replied.jsonl")
ro.AMBIGUOUS = os.path.join(EVENTS, "ambiguous_posts.jsonl")


def load_ambiguous():
    try:
        with open(ro.AMBIGUOUS, "r", encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
    except Exception:
        return []


def last_run_epoch():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return int(f.read().strip() or 0)
    except Exception:
        return 0


def save_last_run(now):
    os.makedirs(EVENTS, exist_ok=True)
    tmp = STATE_FILE + f".tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(str(int(now)))
    os.replace(tmp, STATE_FILE)


# --- Pending resends (DLQ resend, final form 2026-11-13) ------------

def load_pending_resends():
    try:
        with open(PENDING_RESENDS, "r", encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
    except Exception:
        return []


def append_pending_resend(entry):
    os.makedirs(EVENTS, exist_ok=True)
    # Serialized with flock like msgstate.py (adopted 2026-11-14 from
    # Neiro's review): two sweeps must never interleave appends.
    with open(PENDING_RESENDS, "a", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            f.flush()
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def latest_pending_resends():
    """Latest entry per target_ts whose status is still pending."""
    latest = {}
    for e in load_pending_resends():
        latest[e.get("target_ts")] = e
    return {t: e for t, e in latest.items()
            if e.get("status") == "pending" and e.get("message")}


def _is_reply_after_event(msg_ts, event_ts):
    """True when a reply ts is after the event ts (numeric compare, string fallback).

    Adopted 2026-11-14 from Yama's audit share (their指摘2, agy_full_audit_out.md):
    without this check a reply posted *before* the event -- e.g. an
    identical-text reply to an earlier message in the same thread --
    could be matched as this event's reply. A reply with no ts at all
    never matches: delivery is never guessed.
    """
    if msg_ts is None or event_ts is None:
        return False
    try:
        return float(msg_ts) > float(event_ts)
    except (TypeError, ValueError):
        return str(msg_ts) > str(event_ts)


def format_failed_at_jst(iso):
    """'2026-11-13T14:42:36+00:00' -> '2026-11-13 23:42 JST'."""
    try:
        dt = datetime.fromisoformat(iso)
    except Exception:
        return iso
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if ZoneInfo is not None:
        dt = dt.astimezone(ZoneInfo("Asia/Tokyo"))
    return dt.strftime("%Y-%m-%d %H:%M JST")


def resend_prefix(first_failed_at):
    # Unified 3-way wording agreed 2026-11-14 (Yama's proposal, Neiro
    # already uses the same year+JST form): "最初の試行" reads
    # unambiguously as the first attempt, where "元の失敗時刻" could be
    # read as the latest failure after retries.
    return f"遅れての再送（最初の試行：{format_failed_at_jst(first_failed_at)}）\n"


def resend_prefix_legacy(first_failed_at):
    # Pre-agreement wording, kept so a resend already sent with the old
    # prefix is still recognised as delivered in the thread check below.
    return f"遅れての再送（元の失敗時刻：{format_failed_at_jst(first_failed_at)}）\n"


def record_pending_resend(channel, thread_ts, target_ts, message,
                          questioner_user, error_kind, first_failed_at=None):
    """Record a pre-check failure for a later delayed resend.

    Raises ValueError for a non-transient error kind: only transient
    pre-check failures are eligible (a send failure that exhausts
    retries stays dead-lettered; this sweep does not rescue it).
    Raises ValueError when a pending entry for the same target already
    exists -- one held reply per target, never two.
    """
    if error_kind not in TRANSIENT_ERROR_KINDS:
        raise ValueError(
            f"error kind {error_kind!r} is not transient; "
            f"eligible kinds: {sorted(TRANSIENT_ERROR_KINDS)}")
    if target_ts in latest_pending_resends():
        raise ValueError(f"a pending resend already exists for target {target_ts}")
    failed_at = first_failed_at or datetime.now(timezone.utc).isoformat()
    append_pending_resend({
        "status": "pending",
        "channel": channel, "thread_ts": thread_ts, "target_ts": target_ts,
        "message": message, "questioner_user": questioner_user,
        "error_kind": error_kind, "first_failed_at": failed_at,
        "at": datetime.now(timezone.utc).isoformat(),
    })
    return failed_at


def thread_all_messages(channel, thread_ts):
    """All messages in the live thread (parent included), oldest first.

    Same paging as reply_once.thread_yanayana_replies (start with
    --oldest at the parent ts, walk next_oldest), but returns every
    message, not just Yanayana's: the resend sweep needs the parent
    (deleted?) and other people's posts (answered?).
    """
    messages = []
    oldest = thread_ts
    for _page in range(20):  # safety cap: 20 pages x 100 messages
        data = ro.thread_read_page_retry(channel, thread_ts, oldest)
        messages.extend({"ts": m.get("ts"), "user": m.get("user"),
                         "bot_id": m.get("bot_id"),
                         "text": m.get("text") or ""}
                        for m in data.get("messages", []))
        if not data.get("has_more"):
            break
        nxt = data.get("next_oldest")
        if not nxt or nxt == oldest:
            break
        oldest = nxt
    return messages


def reconcile_pending_resends(thread_reader=None, sender=None, now=None):
    """One sweep over held pre-check-failure replies.

    thread_reader(channel, thread_ts) -> [{"ts", "user", "text"}]
    defaults to thread_all_messages (live Slack read).
    sender(channel, thread_ts, message) -> reply ts; defaults to
    reply_once.send. Tests inject fakes, so no real Slack call is made.
    Returns a summary dict.
    """
    reader = thread_reader or thread_all_messages
    send = sender or ro.send
    now_epoch = now if now is not None else time.time()
    # Reply ts consumed by one entry in this sweep (recovered or just
    # sent) must not resolve another entry: same idea as Yama's
    # assigned_reply_ts, adopted 2026-11-14. Seeded from replied.jsonl
    # (Neiro's share 2026-11-14: treat an already-resolved found_ts
    # as used) so a reply recorded in an earlier sweep is not
    # matched again to a different pending entry here.
    used_reply_ts = {r.get("reply_ts") for r in ro.load_record()
                     if r.get("reply_ts")}
    summary = {"checked": 0, "sent": 0, "given_up": 0,
               "still_pending": 0, "skipped_read_failed": 0, "details": []}
    for target_ts, e in latest_pending_resends().items():
        summary["checked"] += 1
        channel = e.get("channel")
        thread_ts = e.get("thread_ts")
        message = e.get("message")
        questioner = e.get("questioner_user")
        first_failed_at = e.get("first_failed_at") or e.get("at")
        try:
            messages = reader(channel, thread_ts)
        except Exception as exc:
            summary["skipped_read_failed"] += 1
            summary["still_pending"] += 1
            summary["details"].append(
                {"target_ts": target_ts, "result": "skipped_read_failed",
                 "error": str(exc)[:200]})
            continue
        # Parent post deleted -> record only, never resend.
        if not any(m.get("ts") == thread_ts for m in messages):
            append_pending_resend({**e, "status": "given_up",
                                   "reason": "parent_deleted",
                                   "at": datetime.now(timezone.utc).isoformat()})
            summary["given_up"] += 1
            summary["details"].append({"target_ts": target_ts, "result": "given_up",
                                       "reason": "parent_deleted"})
            continue
        # Already sent (original or prefixed text found in the thread,
        # posted after the target)? Never repost; record the found
        # reply instead. This check comes BEFORE answered_by_other
        # (adopted 2026-11-14 from Neiro's review of the published
        # code): if the original send actually landed but its response
        # was lost, and someone else answered in the meantime, the old
        # order gave up as answered_by_other and the delivered reply
        # was never recorded in replied.jsonl.
        prefixed = resend_prefix(first_failed_at) + message
        legacy_prefixed = resend_prefix_legacy(first_failed_at) + message
        # Yama's assigned_reply_ts idea (adopted 2026-11-14): a reply ts
        # consumed by one entry must not resolve another in this sweep.
        found = next((m for m in messages
                      if m.get("user") == YANAYANA_USER
                      and m.get("text") in (message, prefixed, legacy_prefixed)
                      and m.get("ts") not in used_reply_ts
                      and _is_reply_after_event(m.get("ts"), target_ts)), None)
        if found:
            used_reply_ts.add(found.get("ts"))
            ro.record_reply(channel, thread_ts, target_ts,
                            found.get("ts"), recovered=True)
            append_pending_resend({**e, "status": "sent", "recovered": True,
                                   "reply_ts": found.get("ts"),
                                   "at": datetime.now(timezone.utc).isoformat()})
            summary["sent"] += 1
            summary["details"].append({"target_ts": target_ts, "result": "sent",
                                       "reply_ts": found.get("ts"),
                                       "recovered": True})
            continue
        # Someone else already answered: a text post after the target,
        # by anyone other than the questioner and Yanayana herself,
        # from a human (not a bot) with more than 10 characters of
        # text. Narrowed 2026-11-14 from Yama's share (their
        # resend fix, tests 36/36): another bot's auto-reply or a
        # short acknowledgement ("ありがとう" etc.) must not falsely
        # silence a delayed resend -- false silence is worse than a
        # resend marked "遅れての再送".
        answered_by = next(
            (m for m in messages
             if _is_reply_after_event(m.get("ts"), target_ts)
             and m.get("user") not in (questioner, YANAYANA_USER)
             and not m.get("bot_id")
             and len((m.get("text") or "").strip()) > 10),
            None)
        if answered_by:
            append_pending_resend({**e, "status": "given_up",
                                   "reason": "answered_by_other",
                                   "answered_by_ts": answered_by.get("ts"),
                                   "at": datetime.now(timezone.utc).isoformat()})
            summary["given_up"] += 1
            summary["details"].append({"target_ts": target_ts, "result": "given_up",
                                       "reason": "answered_by_other",
                                       "answered_by_ts": answered_by.get("ts")})
            continue
        # 24h window from the original failure, no grace.
        try:
            failed_epoch = datetime.fromisoformat(first_failed_at).timestamp()
        except Exception:
            failed_epoch = now_epoch
        if now_epoch - failed_epoch > RESEND_WINDOW_SECS:
            append_pending_resend({**e, "status": "given_up",
                                   "reason": "window_expired",
                                   "at": datetime.now(timezone.utc).isoformat()})
            summary["given_up"] += 1
            summary["details"].append({"target_ts": target_ts, "result": "given_up",
                                       "reason": "window_expired"})
            continue
        try:
            reply_ts = send(channel, thread_ts, prefixed)
        except Exception as exc:
            # A send failure here leaves the entry pending for the next
            # hourly pass (until the 24h window expires); the sweep
            # never guesses delivery and never reposts blindly.
            summary["still_pending"] += 1
            summary["details"].append({"target_ts": target_ts,
                                       "result": "still_pending",
                                       "error": str(exc)[:200]})
            continue
        if reply_ts:
            used_reply_ts.add(reply_ts)
            ro.record_reply(channel, thread_ts, target_ts, reply_ts)
        append_pending_resend({**e, "status": "sent", "reply_ts": reply_ts,
                               "at": datetime.now(timezone.utc).isoformat()})
        summary["sent"] += 1
        summary["details"].append({"target_ts": target_ts, "result": "sent",
                                   "reply_ts": reply_ts})
    return summary



def reconcile(thread_reader=None):
    """One sweep. Returns a summary dict.

    thread_reader(channel, thread_ts) -> [{"ts": ..., "text": ...}]
    defaults to reply_once.thread_yanayana_replies (live Slack read).
    Tests inject a fake reader, so no real Slack call is made.
    """
    reader = thread_reader or ro.thread_yanayana_replies
    entries = load_ambiguous()
    resolved_targets = {e.get("target_ts") for e in entries
                        if e.get("outcome") == "resolved_by_thread_check"}
    closed_targets = {e.get("target_ts") for e in entries
                      if e.get("outcome") == "closed_no_text"}
    # Latest entry per unresolved target wins; older duplicates of the
    # same target are the same lost send, not separate work.
    latest = {}
    for e in entries:
        if e.get("outcome") == "ambiguous_post_outcome":
            latest[e.get("target_ts")] = e
    summary = {"checked": 0, "resolved": 0, "still_unresolved": 0,
               "closed_no_text": 0, "skipped_no_text": 0,
               "skipped_read_failed": 0, "details": []}
    # Same no-reuse rule as the pending-resend sweep (Yama's
    # assigned_reply_ts, adopted 2026-11-14): two ambiguous entries
    # with identical text must not resolve to the same reply ts.
    # Seeded from replied.jsonl (Neiro's share 2026-11-14: treat an
    # already-resolved found_ts as used): a reply ts that resolved
    # an entry in an earlier sweep must not resolve a different
    # entry in this one -- used_reply_ts used to reset every sweep,
    # so the same physical reply could be matched twice.
    used_reply_ts = {r.get("reply_ts") for r in ro.load_record()
                     if r.get("reply_ts")}
    for target_ts, e in latest.items():
        if target_ts in resolved_targets or target_ts in closed_targets:
            continue
        message = e.get("message")
        if not message:
            if ro.close_no_text(e.get("channel"), e.get("thread_ts"), target_ts):
                summary["closed_no_text"] += 1
                summary["details"].append({"target_ts": target_ts, "result": "closed_no_text"})
            else:
                summary["skipped_no_text"] += 1
                summary["details"].append({"target_ts": target_ts, "result": "skipped_no_text"})
            continue
        summary["checked"] += 1
        try:
            replies = reader(e.get("channel"), e.get("thread_ts"))
        except Exception as exc:
            summary["skipped_read_failed"] += 1
            summary["details"].append(
                {"target_ts": target_ts, "result": "skipped_read_failed",
                 "error": str(exc)[:200]})
            continue
        found = next((r for r in replies
                      if r.get("text") == message
                      and r.get("ts") not in used_reply_ts
                      and _is_reply_after_event(r.get("ts"), target_ts)), None)
        if found:
            used_reply_ts.add(found.get("ts"))
            ro.record_reply(e.get("channel"), e.get("thread_ts"), target_ts,
                            found.get("ts"), recovered=True)
            ro.resolve_ambiguous(e.get("channel"), e.get("thread_ts"), target_ts)
            summary["resolved"] += 1
            summary["details"].append(
                {"target_ts": target_ts, "result": "resolved",
                 "reply_ts": found.get("ts")})
        else:
            summary["still_unresolved"] += 1
            summary["details"].append({"target_ts": target_ts, "result": "still_unresolved"})
    return summary


def cmd_record(argv):
    """reconcile_ambiguous.py record --channel C --thread-ts T
    --target-ts M --questioner U --error-kind KIND --message TEXT
    [--first-failed-at ISO8601]

    Records a pre-check failure for a later delayed resend. Exits 1
    (and records nothing) for a non-transient error kind or a
    duplicate target.
    """
    opts = {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--channel", "--thread-ts", "--target-ts", "--questioner",
                 "--error-kind", "--message", "--first-failed-at") and i + 1 < len(argv):
            opts[a[2:]] = argv[i + 1]
            i += 2
        else:
            print(f"unknown argument: {a}", file=sys.stderr)
            return 1
    required = ("channel", "thread-ts", "target-ts", "questioner",
                "error-kind", "message")
    missing = [k for k in required if k not in opts]
    if missing:
        print(f"record: missing --{' --'.join(missing)}", file=sys.stderr)
        return 1
    try:
        failed_at = record_pending_resend(
            opts["channel"], opts["thread-ts"], opts["target-ts"],
            opts["message"], opts["questioner"], opts["error-kind"],
            opts.get("first-failed-at"))
    except ValueError as exc:
        print(f"record: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"recorded": True, "target_ts": opts["target-ts"],
                      "first_failed_at": failed_at}, ensure_ascii=False))
    return 0


def main(argv):
    if argv and argv[0] == "record":
        return cmd_record(argv[1:])
    force = "--force" in argv
    interval = DEFAULT_INTERVAL_SECS
    if "--interval-secs" in argv:
        i = argv.index("--interval-secs")
        try:
            interval = int(argv[i + 1])
        except (IndexError, ValueError):
            print("usage: reconcile_ambiguous.py [--force] [--interval-secs N]",
                  file=sys.stderr)
            return 1
    now = time.time()
    if not force and now - last_run_epoch() < interval:
        print(json.dumps({"gated": True,
                          "next_run_in_secs": int(interval - (now - last_run_epoch()))},
                         ensure_ascii=False))
        return 0
    # Single-instance gate (adopted 2026-11-14 from Neiro's review):
    # the last-run timestamp alone cannot stop two sweeps that start
    # together (timer + manual run) from both passing the already-sent
    # check and sending the same resend twice. Hold an exclusive
    # flock for the whole sweep; a second instance exits gated.
    os.makedirs(EVENTS, exist_ok=True)
    lock_fd = os.open(os.path.join(EVENTS, "reconcile.lock"),
                      os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(lock_fd)
        print(json.dumps({"gated": True, "reason": "already_running"},
                         ensure_ascii=False))
        return 0
    try:
        summary = reconcile()
        summary["pending_resends"] = reconcile_pending_resends()
        save_last_run(now)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
    summary["gated"] = False
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
