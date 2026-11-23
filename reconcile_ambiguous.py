#!/usr/bin/env python3
"""Periodically reconcile ambiguous Slack sends against live threads.

Adopted 2026-11-12 from Yama's implementation share in #slack-へようこそ
(their "遅れて着地した返信の定期照合"): reply_once.py already resolves
an ambiguous_post_outcome when a *later* pre-send check for the same
target proves the reply landed, but if no later send to that target
ever happens, the ambiguous record stands forever and the replies
ledger keeps saying "not posted". This sweep re-checks unresolved
ambiguous entries against the live thread on a schedule (the poll hook
runs it; it self-gates to at most once an hour) and, when the reply
text is found (compared after normalization, see _normalize_text --
adopted 2026-11-14 from Yama's share, slack-yama commit 2c25158),
records the reply (recovered) and marks the
ambiguous entry resolved -- the same record-reply path a pre-send
check uses. Both the ledger entry and the correction entry carry
the originating event_id when the ambiguous entry has one (added
2026-11-15 from Neiro's correction in #slack-へようこそ: their
fix keeps the mark on the correction entries too). Entries that carry an event_id are matched through a
metadata-aware read instead (added 2026-11-15 from Yama's
confirmation in #slack-へようこそ: their reconcile excludes replies
stamped with a different event_id at all four matching points):
a same-text reply stamped for another event must not resolve the
entry; unstamped replies stay text candidates. Entries without an
event_id keep the text-only match. A reply ts consumed by one entry is never reused for
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
    without ever reaching replied.jsonl. When the entry carries an
    event_id, the already-sent check reads the thread with metadata
    and drops our own replies stamped for another event (Neiro's
    share 2026-11-15, neiro-slack-system commit 2af573d); entries
    without one keep the text-only match, and a metadata read
    failure leaves the entry pending (no metadata-blind fallback,
    same stance as the ambiguous sweep).
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

State heal (added 2026-11-23, the bookkeeping seam -- a reply that
landed but whose finalize writes were lost with the worker run):
this sweep also runs reconcile_state_heal -- pending/failed inbox
items with no reply-ledger row are checked for a reply carrying
their own event_id stamp (recorded recovered when found, never
reposted), and msgstate's ledger heal then marks done everything
either reply ledger proves answered. See reconcile_state_heal.

Run log (Yama's share 2026-11-14, slack-yama commit 7188927): each
sweep appends a started/finished line to events/reconcile_runs.jsonl
and overwrites events/reconcile_last.json with that run's summary,
so a run cut off mid-sweep leaves a started line with no finished
line. A gated run (interval / already running) appends one gated
line instead, so a skipped run stays visible (Yama's question
2026-11-14). No per-target progress is logged (results already live
in the ledgers); log writes are best-effort and never stop the sweep.

Usage:
  reconcile_ambiguous.py [--force] [--interval-secs N]
  reconcile_ambiguous.py record --channel C... --thread-ts T...
      --target-ts M... --questioner U... --error-kind KIND --message TEXT
      [--first-failed-at ISO8601] [--event-id Ev...]
Exit 0 on success (including "nothing to do" / "gated"); prints a
JSON summary. State: events/reconcile_ambiguous.state (last run epoch).
SLACK_LISTENER_BASE overrides the base dir (tests).
"""
import fcntl
import html
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

_PM_SPEC = importlib.util.spec_from_file_location(
    "post_metadata", os.path.join(os.path.dirname(os.path.abspath(__file__)), "post_metadata.py"))
pm = importlib.util.module_from_spec(_PM_SPEC)
_PM_SPEC.loader.exec_module(pm)

_MS_SPEC = importlib.util.spec_from_file_location(
    "msgstate", os.path.join(os.path.dirname(os.path.abspath(__file__)), "msgstate.py"))
ms = importlib.util.module_from_spec(_MS_SPEC)
_MS_SPEC.loader.exec_module(ms)
# Same rebasing as reply_once above: msgstate's paths follow this
# base, not the module's own directory.
ms.BASE = BASE
ms.EVENTS = EVENTS
ms.INBOX = os.path.join(EVENTS, "inbox.jsonl")
ms.STATE = os.path.join(EVENTS, "message_state.json")
ms.DEAD = os.path.join(EVENTS, "dead_letter.jsonl")
ms.LOCK = os.path.join(EVENTS, "message_state.lock")
ms.STATS = os.path.join(EVENTS, "inbox_stats.json")
ms.REPLIES_LEDGER = os.path.join(EVENTS, "replies.jsonl")
ms.REPLIED_LEDGER = os.path.join(EVENTS, "replied.jsonl")
ms.HEAL_LOG = os.path.join(EVENTS, "state_heal.jsonl")


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


def _normalize_text(text):
    """Normalize message text before comparing a stored send with a
    thread read (Yama's share 2026-11-14, slack-yama commit 2c25158):
    absorb whitespace / line-ending / escaping differences (e.g. a
    trailing space, CRLF, or `&amp;` in the Slack API text) so a
    delivered reply is not missed over a cosmetic difference. Both
    sides are normalized, so the comparison stays symmetric; the
    no-reuse and after-event guards still apply unchanged.
    """
    text = html.unescape(text or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    return "\n".join(lines).strip()


def _log_run(event, ok=None, error=None, reason=None):
    """Append one start/finish line for this sweep run (Yama's share
    2026-11-14, slack-yama commit 7188927): a run cut off mid-sweep
    leaves a started line with no finished line, so the interruption
    itself stays visible afterwards. Gated runs (interval / already
    running) also log one line, so a skipped run is distinguishable
    from a run that never started (Yama's question 2026-11-14).
    Best-effort: a log-write failure must never stop the sweep."""
    try:
        os.makedirs(EVENTS, exist_ok=True)
        line = {"event": event,
                "at": datetime.now(timezone.utc).isoformat()}
        if ok is not None:
            line["ok"] = ok
        if error:
            line["error"] = error
        if reason:
            line["reason"] = reason
        with open(os.path.join(EVENTS, "reconcile_runs.jsonl"), "a",
                  encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _save_last_summary(summary):
    """Overwrite events/reconcile_last.json with this run's summary
    (Yama's share 2026-11-14): only the latest run's output is kept.
    Best-effort, like _log_run."""
    try:
        with open(os.path.join(EVENTS, "reconcile_last.json"), "w",
                  encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False)
            f.write("\n")
    except Exception:
        pass


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
    # Unified 3-way wording, final form agreed 2026-11-14 (Yama's
    # revert, matching the original Neiro/Yanayana wording): "元の失敗時刻"
    # is the time the send first stalled, and a review found that
    # looking aligned while differing slightly is the most confusing
    # state. The interim "最初の試行" wording (used briefly on
    # 2026-11-14 after Yama's first proposal) is kept as legacy below
    # so a resend already sent with it is still recognised.
    return f"遅れての再送（元の失敗時刻：{format_failed_at_jst(first_failed_at)}）\n"


def resend_prefix_legacy(first_failed_at):
    # Interim wording, kept so a resend already sent with it is still
    # recognised as delivered in the thread check below.
    return f"遅れての再送（最初の試行：{format_failed_at_jst(first_failed_at)}）\n"


def record_pending_resend(channel, thread_ts, target_ts, message,
                          questioner_user, error_kind, first_failed_at=None,
                          event_id=None):
    """Record a pre-check failure for a later delayed resend.

    Raises ValueError for a non-transient error kind: only transient
    pre-check failures are eligible (a send failure that exhausts
    retries stays dead-lettered; this sweep does not rescue it).
    Raises ValueError when a pending entry for the same target already
    exists -- one held reply per target, never two.
    event_id (optional, the originating Slack event id) is stored so
    the sweep's already-sent check can read the thread with metadata
    and drop our own replies stamped for another event (Neiro's share
    2026-11-15); entries without one keep the text-only match.
    """
    if error_kind not in TRANSIENT_ERROR_KINDS:
        raise ValueError(
            f"error kind {error_kind!r} is not transient; "
            f"eligible kinds: {sorted(TRANSIENT_ERROR_KINDS)}")
    if target_ts in latest_pending_resends():
        raise ValueError(f"a pending resend already exists for target {target_ts}")
    failed_at = first_failed_at or datetime.now(timezone.utc).isoformat()
    entry = {
        "status": "pending",
        "channel": channel, "thread_ts": thread_ts, "target_ts": target_ts,
        "message": message, "questioner_user": questioner_user,
        "error_kind": error_kind, "first_failed_at": failed_at,
        "at": datetime.now(timezone.utc).isoformat(),
    }
    if event_id is not None:
        entry["event_id"] = event_id
    append_pending_resend(entry)
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


def _find_sent_reply(messages, meta_replies, wanted, used_reply_ts,
                     target_ts):
    """Already-sent reply in the thread (original/prefixed text, after target).

    meta_replies (event_id entries) already holds only our own
    metadata-filtered replies; otherwise the search is over our own
    messages in the thread. Pure search over already-fetched lists;
    the caller owns the reads (and their failures), so the same check
    can run twice: once after the sweep's read and once just before
    the send, under the send lock (Claude Code audit 2026-11-21).
    """
    if meta_replies is not None:
        return next((m for m in meta_replies
                     if _normalize_text(m.get("text")) in wanted
                     and m.get("ts") not in used_reply_ts
                     and _is_reply_after_event(m.get("ts"), target_ts)),
                    None)
    return next((m for m in messages
                 if m.get("user") == YANAYANA_USER
                 and _normalize_text(m.get("text")) in wanted
                 and m.get("ts") not in used_reply_ts
                 and _is_reply_after_event(m.get("ts"), target_ts)), None)


def _record_found_reply(e, found, used_reply_ts, summary):
    """Record an already-sent reply found in the thread as delivered."""
    used_reply_ts.add(found.get("ts"))
    ro.record_reply(e.get("channel"), e.get("thread_ts"),
                    e.get("target_ts"), found.get("ts"), recovered=True,
                    event_id=e.get("event_id"))
    append_pending_resend({**e, "status": "sent", "recovered": True,
                           "reply_ts": found.get("ts"),
                           "at": datetime.now(timezone.utc).isoformat()})
    summary["sent"] += 1
    summary["details"].append({"target_ts": e.get("target_ts"),
                               "result": "sent",
                               "reply_ts": found.get("ts"),
                               "recovered": True})


def reconcile_pending_resends(thread_reader=None, sender=None, now=None,
                              metadata_reader=None):
    """One sweep over held pre-check-failure replies.

    thread_reader(channel, thread_ts) -> [{"ts", "user", "text"}]
    defaults to thread_all_messages (live Slack read).
    sender(channel, thread_ts, message) -> reply ts; defaults to
    reply_once.send. metadata_reader(channel, thread_ts, event_id) ->
    [{"ts", "text"}] defaults to metadata_thread_replies: used for the
    already-sent check of entries that carry an event_id, so a
    same-text reply stamped for another event cannot resolve the
    entry. Tests inject fakes, so no real Slack call is made.
    Returns a summary dict.
    """
    reader = thread_reader or thread_all_messages
    send = sender or ro.send
    meta_reader = metadata_reader or metadata_thread_replies
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
        # 24h window from the original failure, no grace. Computed
        # here so it also bounds the read-failure paths below: an
        # entry whose thread can never be read must still expire
        # (Claude Code audit 2026-11-21 -- "readable-again" was the
        # only expiring path before).
        try:
            failed_epoch = datetime.fromisoformat(first_failed_at).timestamp()
        except Exception:
            failed_epoch = now_epoch

        def _expire_or_skip(exc):
            """Give up on window expiry, else leave pending. True if given up."""
            if now_epoch - failed_epoch > RESEND_WINDOW_SECS:
                append_pending_resend({**e, "status": "given_up",
                                       "reason": "window_expired",
                                       "at": datetime.now(timezone.utc).isoformat()})
                summary["given_up"] += 1
                summary["details"].append({"target_ts": target_ts,
                                           "result": "given_up",
                                           "reason": "window_expired"})
                return True
            summary["skipped_read_failed"] += 1
            summary["still_pending"] += 1
            summary["details"].append(
                {"target_ts": target_ts, "result": "skipped_read_failed",
                 "error": str(exc)[:200]})
            return False

        try:
            messages = reader(channel, thread_ts)
        except Exception as exc:
            _expire_or_skip(exc)
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
        wanted = (_normalize_text(message), _normalize_text(prefixed),
                  _normalize_text(legacy_prefixed))
        if e.get("event_id"):
            # Stamp guard (Neiro's share 2026-11-15,
            # neiro-slack-system commit 2af573d): a same-text reply
            # stamped for another event is that event's answer, not
            # this resend's, so the already-sent check reads the
            # thread with metadata and drops differently-stamped own
            # replies (metadata_thread_replies already keeps only
            # Yanayana's replies). A metadata read failure leaves
            # the entry pending -- no metadata-blind fallback, the
            # same stance as the ambiguous sweep above.
            try:
                meta_replies = meta_reader(channel, thread_ts,
                                           e["event_id"])
            except Exception as exc:
                _expire_or_skip(exc)
                continue
        else:
            meta_replies = None
        found = _find_sent_reply(messages, meta_replies, wanted,
                                 used_reply_ts, target_ts)
        if found:
            _record_found_reply(e, found, used_reply_ts, summary)
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
        # 24h window from the original failure, no grace (failed_epoch
        # computed at the top of this iteration).
        if now_epoch - failed_epoch > RESEND_WINDOW_SECS:
            append_pending_resend({**e, "status": "given_up",
                                   "reason": "window_expired",
                                   "at": datetime.now(timezone.utc).isoformat()})
            summary["given_up"] += 1
            summary["details"].append({"target_ts": target_ts, "result": "given_up",
                                       "reason": "window_expired"})
            continue
        # Send under the same send.lock the hook path holds
        # (send_reply_locked.py), with a fresh already-sent re-check
        # first (Claude Code audit 2026-11-21): a reply may have
        # landed between this sweep's thread read above and the send
        # -- without the lock and the re-check, a hook worker sending
        # to the same target in that window double-posts. A re-check
        # read failure leaves the entry pending; the sweep never
        # sends blind. The lock is released when the file closes,
        # even on crash, so no stale lock can strand later sends.
        os.makedirs(EVENTS, exist_ok=True)
        with open(os.path.join(EVENTS, "send.lock"), "a+",
                  encoding="utf-8") as lockf:
            fcntl.flock(lockf, fcntl.LOCK_EX)
            try:
                fresh = reader(channel, thread_ts)
                fresh_meta = None
                if e.get("event_id"):
                    fresh_meta = meta_reader(channel, thread_ts,
                                             e["event_id"])
            except Exception as exc:
                summary["skipped_read_failed"] += 1
                summary["still_pending"] += 1
                summary["details"].append(
                    {"target_ts": target_ts, "result": "skipped_read_failed",
                     "error": str(exc)[:200]})
                continue
            found = _find_sent_reply(fresh, fresh_meta, wanted,
                                     used_reply_ts, target_ts)
            if found:
                _record_found_reply(e, found, used_reply_ts, summary)
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
                ro.record_reply(channel, thread_ts, target_ts, reply_ts,
                                event_id=e.get("event_id"))
            append_pending_resend({**e, "status": "sent", "reply_ts": reply_ts,
                                   "at": datetime.now(timezone.utc).isoformat()})
            summary["sent"] += 1
            summary["details"].append({"target_ts": target_ts, "result": "sent",
                                       "reply_ts": reply_ts})
    return summary



def metadata_thread_replies(channel, thread_ts, event_id):
    """Yanayana's thread replies for one ambiguous entry, metadata-aware.

    Reads the thread with include_all_metadata and drops our own
    messages stamped with a *different* event_id: a same-text reply
    to another event is that event's answer, not this entry's, so it
    must not resolve the entry. Messages with no stamp (sent before
    stamping existed, or manual posts) stay as text candidates, as do
    messages stamped with this event_id.

    Added 2026-11-15 from Yama's confirmation in #slack-へようこそ:
    their reconcile excludes different-event_id posts at all four
    matching points, while ours still matched the periodic sweep by
    text alone -- the same gap Neiro had just corrected on their side.
    Raises RuntimeError when the token cannot be resolved or the read
    fails; the caller leaves the entry unresolved instead of deciding
    from a metadata-blind read.
    """
    token = pm.resolve_token()
    if not token:
        raise RuntimeError("metadata check: token not found")
    messages = pm.read_thread(channel, thread_ts, token)
    out = []
    for m in messages:
        if m.get("user") != YANAYANA_USER or m.get("ts") == thread_ts:
            continue
        stamped = pm.stamp_event_id(m)
        if stamped is not None and stamped != str(event_id):
            continue
        out.append({"ts": m.get("ts"), "text": m.get("text") or ""})
    return out


def _stamped_thread_replies(channel, thread_ts, event_id):
    """Our replies in the thread stamped with exactly this event_id.

    Stricter than metadata_thread_replies: an unstamped reply is NOT
    proof here. The state-heal sweep has no intended text to match
    against, so only the metadata stamp ties a reply to this event.
    Raises RuntimeError when the read cannot be made; the caller
    leaves the entry untouched.
    """
    token = pm.resolve_token()
    if not token:
        raise RuntimeError("state heal: token not found")
    out = []
    for m in pm.read_thread(channel, thread_ts, token):
        if m.get("user") != YANAYANA_USER:
            continue
        if pm.stamp_event_id(m) == str(event_id):
            out.append({"ts": m.get("ts"), "text": m.get("text") or ""})
    return out


# State heal (2026-11-23, the bookkeeping seam): do not race a live
# worker -- an item younger than this may still be mid-finalization.
STATE_HEAL_MIN_AGE_SECS = 600
STATE_HEAL_MAX_PER_RUN = 10


def reconcile_state_heal(metadata_reader=None, now=None):
    """Heal message state that never reached done after a reply.

    Two layers, cheapest first in effect:
    1. Stamped recovery: pending/failed inbox items (not processing,
       not already in a reply ledger) older than
       STATE_HEAL_MIN_AGE_SECS are checked against their thread for
       a reply carrying their own event_id stamp. A process killed
       between the Slack POST and reply_once's ledger write leaves
       exactly this shape: the reply is in the thread with the
       stamp, and no local record exists anywhere. A found reply is
       recorded (recovered) into replied.jsonl -- never reposted.
       Matching is stamp-only (no intended text is available for
       these items), after the target ts, and a reply ts already
       consumed by another record is never reused.
    2. Ledger heal: msgstate.heal_replied_locked marks done every
       inbox message either reply ledger proves answered, including
       the rows layer 1 just recorded.

    Yama's reconcile-replies (slack-yama) heals the same seam from
    the thread side for DONE/FAILED rows carrying a reply-ish error;
    our candidates are the mirror image (state still open, ledger
    empty) and the stamp replaces their intended-text match.
    Returns a summary dict.
    """
    reader = metadata_reader or _stamped_thread_replies
    summary = {"checked": 0, "stamped_recovered": 0, "healed": 0,
               "skipped_read_failed": 0, "details": []}
    if now is None:
        now = datetime.now(timezone.utc)
    with ms.state_lock(exclusive=True):
        state = ms.load_state()
        inbox = ms.read_inbox()
    pairs, ts_only = ms._ledger_targets()
    candidates = []
    for rec in inbox:
        ts = rec.get("ts")
        if not ts or not rec.get("event_id"):
            continue
        st = state.get(ms.key_of(rec)) or {}
        if st.get("status", "pending") not in ("pending", "failed"):
            continue
        if (rec.get("channel"), ts) in pairs or ts in ts_only:
            continue  # the ledger heal below closes these
        received = ms._parse_ts(rec.get("received_at") or "")
        if received is None or (now - received).total_seconds() < STATE_HEAL_MIN_AGE_SECS:
            continue
        candidates.append(rec)
    candidates.sort(key=lambda r: r.get("received_at") or "")
    used = {r.get("reply_ts") for r in ro.load_record() if r.get("reply_ts")}
    for rec in candidates[:STATE_HEAL_MAX_PER_RUN]:
        summary["checked"] += 1
        thread_ts = rec.get("thread_ts") or rec.get("ts")
        try:
            found = reader(rec.get("channel"), thread_ts, rec.get("event_id"))
        except Exception:
            summary["skipped_read_failed"] += 1
            summary["details"].append({"target_ts": rec.get("ts"),
                                        "result": "skipped_read_failed"})
            continue
        hit = next((m for m in found
                    if m.get("ts") and m["ts"] not in used
                    and _is_reply_after_event(m["ts"], rec.get("ts"))), None)
        if hit:
            ro.record_reply(rec.get("channel"), thread_ts, rec.get("ts"),
                            hit["ts"], recovered=True,
                            message=hit.get("text") or None,
                            event_id=rec.get("event_id"))
            used.add(hit["ts"])
            summary["stamped_recovered"] += 1
            summary["details"].append({"target_ts": rec.get("ts"),
                                        "result": "stamped_recovered",
                                        "reply_ts": hit["ts"]})
    with ms.state_lock(exclusive=True):
        state = ms.load_state()
        healed = ms.heal_replied_locked(state)
    summary["healed"] = len(healed)
    return summary


def reconcile(thread_reader=None, metadata_reader=None):
    """One sweep. Returns a summary dict.

    thread_reader(channel, thread_ts) -> [{"ts": ..., "text": ...}]
    defaults to reply_once.thread_yanayana_replies (live Slack read).
    metadata_reader(channel, thread_ts, event_id) -> same shape,
    defaults to metadata_thread_replies: used for entries that carry
    an event_id, so a same-text reply stamped for another event
    cannot resolve the entry. Entries without an event_id keep the
    old text-only reader. Tests inject fake readers, so no real Slack
    call is made.
    """
    reader = thread_reader or ro.thread_yanayana_replies
    meta_reader = metadata_reader or metadata_thread_replies
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
        # Whitespace-only counts as no text (Yama's point 2026-11-14:
        # `not text` does not catch " ", which could otherwise linger
        # as an unmatchable entry forever). Same strip() guard as
        # reply_once.py / channel_send_once.py already use.
        if not message or not message.strip():
            if ro.close_no_text(e.get("channel"), e.get("thread_ts"), target_ts):
                summary["closed_no_text"] += 1
                summary["details"].append({"target_ts": target_ts, "result": "closed_no_text"})
            else:
                summary["skipped_no_text"] += 1
                summary["details"].append({"target_ts": target_ts, "result": "skipped_no_text"})
            continue
        summary["checked"] += 1
        try:
            # Entries stamped with an event_id are matched through
            # the metadata-aware reader, which excludes replies
            # stamped for another event (Yama's confirmation
            # 2026-11-15). Entries without one (recorded before
            # stamping, or by a path that had no event_id) keep the
            # old text-only reader. A metadata read failure leaves
            # the entry unresolved rather than falling back to a
            # metadata-blind match.
            if e.get("event_id"):
                replies = meta_reader(e.get("channel"), e.get("thread_ts"),
                                      e.get("event_id"))
            else:
                replies = reader(e.get("channel"), e.get("thread_ts"))
        except Exception as exc:
            summary["skipped_read_failed"] += 1
            summary["details"].append(
                {"target_ts": target_ts, "result": "skipped_read_failed",
                 "error": str(exc)[:200]})
            continue
        found = next((r for r in replies
                      if _normalize_text(r.get("text")) == _normalize_text(message)
                      and r.get("ts") not in used_reply_ts
                      and _is_reply_after_event(r.get("ts"), target_ts)), None)
        if found:
            used_reply_ts.add(found.get("ts"))
            ro.record_reply(e.get("channel"), e.get("thread_ts"), target_ts,
                            found.get("ts"), recovered=True,
                            event_id=e.get("event_id"))
            ro.resolve_ambiguous(e.get("channel"), e.get("thread_ts"), target_ts,
                                 event_id=e.get("event_id"))
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
    [--first-failed-at ISO8601] [--event-id Ev...]

    Records a pre-check failure for a later delayed resend. Exits 1
    (and records nothing) for a non-transient error kind or a
    duplicate target.
    """
    opts = {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--channel", "--thread-ts", "--target-ts", "--questioner",
                 "--error-kind", "--message", "--first-failed-at",
                 "--event-id") and i + 1 < len(argv):
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
            opts.get("first-failed-at"), opts.get("event-id"))
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
        _log_run("gated", reason="interval")
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
        _log_run("gated", reason="already_running")
        print(json.dumps({"gated": True, "reason": "already_running"},
                         ensure_ascii=False))
        return 0
    _log_run("started")
    try:
        summary = reconcile()
        summary["pending_resends"] = reconcile_pending_resends()
        summary["state_heal"] = reconcile_state_heal()
        save_last_run(now)
    except Exception as exc:
        _log_run("finished", ok=False, error=str(exc)[:200])
        raise
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
    summary["gated"] = False
    _log_run("finished", ok=True)
    _save_last_summary(summary)
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
