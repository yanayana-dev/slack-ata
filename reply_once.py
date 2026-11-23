#!/usr/bin/env python3
"""Send a Slack thread reply at most once.

Adopted 2026-11-12 from Yama's duplicate-post fix (shared in
#slack-へようこそ): Yama's answer to Yanayana was posted twice because a
re-executed run did not check whether it had already replied. The same
gap exists here -- a worker could crash after sending but before marking
the message done, and the retry would reply again.

Guard: before sending, check both
  (a) the local sent-record events/replied.jsonl, and
  (b) the live thread, for an existing Yanayana reply.
If either shows a reply already exists, nothing is sent.

Metadata-first matching (added 2026-11-14, completing the
three-way agreement with Yama and Neiro in #slack-へようこそ,
thread 1794583788.831849): with --event-id, the live-thread check
first looks for our own metadata stamp (event_type
"yanayana_reply", event_payload.event_id = the originating Slack
event_id) via post_metadata.py, and the send itself goes through
post_metadata.py (direct chat.postMessage with the stamp) instead
of slackcli. Text matching stays as the second check, so a reply
sent before the stamp existed is still caught. When the metadata
read fails, the check falls back to the text check -- which stays
fail-closed on its own read failure -- rather than blocking a
reply the text check can verify. Without --event-id the send is
stamped with an auto-generated request_id instead (2026-11-23,
three-way "event_id必須＋request_id自動生成" agreement), and the
text check is unchanged -- a request_id stamp carries no event_id,
so it stays a plain text candidate (see stamped_texts).
Request-id stamping (added 2026-11-23, the fallback-key hole fix):
with --request-id instead of --event-id the same metadata stamp
carries the auto-generated nonce (event_payload.request_id), and
the metadata-first duplicate check matches on it. When neither id
is given, main() generates a request_id before sending, so no
reply through either CLI entry point goes out unstamped; only a
direct send() call with neither id keeps the legacy slackcli path.

Stamp guard (added 2026-11-15, from Neiro's correction in
#slack-へようこそ): the text check reads the thread with
include_all_metadata and excludes our own messages stamped with a
different event_id, so a same-text reply to another event no
longer counts as this event's duplicate. Yama had shared the same
guard as designed, then found it was never in their code; our
slackcli text read had the same gap, so the guard was adopted
here (stamped_texts) rather than only noted.

Granularity: with --target-ts (the ts of the message being answered),
idempotency is per target message -- a reply to a different message in
the same thread is a new reply, not a duplicate (the thread-only check
blocked legitimate second replies; fixed 2026-11-12). In target mode
the live-thread check only blocks when the exact same text is already
in the thread, which is what a crash-and-retry would resend. Without
--target-ts the original thread-level behavior is kept.

Windowed read: in target mode the live-thread checks read only from
60s before the target event onward (adopted 2026-11-14 from Yama's
share in #slack-へようこそ), instead of re-reading the whole thread
from the parent. A duplicate of our own reply and another author's
answer to the target can only exist after the target event, so the
check's core is unchanged while the read shrinks to a few messages --
in the 180-reply thread this share arrived in, a full re-read was
~250KB and nearly failing on connection drops, the main cause of
reply delay. The 60s margin covers clock skew and near-simultaneous
posts. Manual posts without an event have no anchor, so they keep
the full-thread read, as do Yama's. Fail-closed is unchanged: a
failed read still refuses to send blind. The windowed read is
further capped at one page (100 messages) from the window start,
adopted 2026-11-14 from Yama's proposal in #slack-へようこそ
(thread 1794583788.831849): if more than a page follows the target,
the window is too long to verify and the reply is not sent
(fail-closed) instead of being treated as unanswered -- a reply
hours later to a question buried under 100+ posts would be out of
place, and paging onward is the same unbounded-read weakness the
window was meant to remove. The local sent-record check runs first,
so a recorded send still blocks a duplicate without any thread read.
Boundary: the parroting guard (duplicate_of_other) reads the window
from target-60s onward, and -- adopted 2026-11-14 from Neiro's share
in #slack-へようこそ (thread 1794583788.831849) -- re-checks the
thread's latest page (one bare read: parent plus the newest ~99
replies) right before sending. The realistic vector -- the draft
picking up the newest post's text (incident 2026-11-14, 21s before
the target) -- is always inside the window, because the window runs
to now and the newest post is never older than the target; the
latest-page re-check additionally catches an identical post older
than target-60s (e.g. the parent text in an old thread, or the same
question asked again further up) that the window cannot see. The
remaining hole is the same as Neiro's: an identical post pushed
beyond the latest 100 is still missed.

Yama's objection (2026-11-14, thread 1794614134.265419) stands for
the *other* parroting vector: a bot-to-bot turn routinely takes
longer than 60s (inference, API calls, rate-limit waits), so the
draft can repeat Yanayana's own earlier summary when the other side
rephrases the point -- and the other side then feeds that echo back
in, chaining into a loop. That vector is now covered by a second
guard, duplicate_of_own_ledger: the outgoing text is compared
against Yanayana's own past replies recorded locally in
events/replied.jsonl (same thread; whitespace-normalized exact
match), so the check costs no API call and sees the whole thread
history, not just the 60s window. Role split, as Yama proposed:
double-send prevention reads Slack, parroting prevention reads the
local ledger.

Usage:
  reply_once.py --channel C0... --thread-ts 1794471381.083589 --message "..."
  reply_once.py --channel C0... --thread-ts 1794471381.083589 --check-only
    # prints "already-replied" or "not-replied", sends nothing
  reply_once.py --channel C0... --thread-ts T... --target-ts M... --message "..."
    # per-target-message idempotency
  reply_once.py --channel C0... --thread-ts T... --target-ts M...
      --event-id Ev... --message "..."
    # event-driven reply: metadata-stamped send (post_metadata.py)
    # and metadata-first duplicate check; without --event-id a
    # request_id is auto-generated and stamped instead, so no
    # reply ever goes out unstamped (2026-11-23)
  reply_once.py --channel C0... --thread-ts T... --target-ts M...
      --request-id <UUID> --message "..."
    # no originating event: same metadata stamp, carrying the
    # request_id nonce instead (send_reply_locked.py auto-generates
    # one, so no send through the locked path goes out unstamped)

Exit codes: 0 = sent, already replied (no-op), or check completed;
1 = usage error; 2 = send failed / thread could not be read and no
record exists to fall back on (refuses to send blind);
3 = ambiguous_post_outcome: the send response was lost and the
recovery confirmation could not prove delivery -- the outcome is
recorded and the caller must not repost blindly (a later attempt's
pre-send check decides from the live thread / record);
4 = duplicate-text: the outgoing message is identical to another
author's existing post in the thread (parroting guard, added
2026-11-14); nothing was sent.
5 = duplicate-own-ledger: the outgoing message repeats Yanayana's
own earlier reply in the same thread (local-ledger parroting guard,
added 2026-11-14 from Yama's proposal); nothing was sent.
"""
import fcntl
import html
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
# Sibling modules (post_metadata) must be importable when this file
# is loaded by path (importlib spec, as the tests do), not just when
# run as a script -- the script directory is not sys.path[0] then.
if BASE not in sys.path:
    sys.path.insert(0, BASE)
import post_metadata

SLACKCLI = os.path.join(BASE, "bin", "slackcli")
EVENTS = os.path.join(BASE, "events")
REPLIED = os.path.join(EVENTS, "replied.jsonl")
AMBIGUOUS = os.path.join(EVENTS, "ambiguous_posts.jsonl")

YANAYANA_USER = "U0EXAMPLEBOT"  # Yanayana bot user id in muse取締役会

# Seconds before the target event a windowed pre-send read starts.
# Adopted 2026-11-14 from Yama's share in #slack-へようこそ (their
# pre-send check reads only from 1 minute before the target event to
# after it): duplicates and answers only exist after the event, so
# the 60s margin only guards clock skew / near-simultaneous posts.
WINDOW_MARGIN_SECONDS = 60


def window_start(target_ts):
    """Oldest ts a target-mode pre-send read starts from.

    target_ts is a Slack ts string ("1794642224.439119"); the window
    opens 60s before it and runs to now (reads are unbounded above).
    """
    return f"{float(target_ts) - WINDOW_MARGIN_SECONDS:.6f}"


def load_record():
    """Read the reply ledger.

    One corrupt line must not discard the whole ledger: bad lines
    are skipped and good records stay. A file that cannot be read
    at all raises (missing is fine -> []); callers treat that as
    cannot-verify. Same lesson as Neiro's 中-1 (2026-11-21): an
    exception while reading a dedupe ledger must not read as
    "no duplicate" and let a send proceed.
    """
    try:
        with open(REPLIED, "r", encoding="utf-8") as f:
            out = []
            for lineno, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    # 2026-11-21, Yama's question in #slack-へようこそ:
                    # a skipped corrupt line used to vanish silently,
                    # so ledger loss went unnoticed. Log the skip
                    # (line number + truncated content) to stderr so
                    # it shows in the run logs; good lines still load.
                    print(f"load_record: skipping corrupt line {lineno} "
                          f"in {REPLIED}: {line[:120]!r}", file=sys.stderr)
                    # 2026-11-22, Yama's proposal (same thread): the
                    # point is noticing, not recording -- note the
                    # sighting for the daily summary (ledger_health),
                    # never a dedicated log file. Lazy import so a
                    # missing/broken helper can never break the load.
                    try:
                        if BASE not in sys.path:
                            sys.path.insert(0, BASE)
                        from ledger_health import note_corrupt_line
                        note_corrupt_line(REPLIED, lineno)
                    except Exception:
                        pass
                    continue
            return out
    except FileNotFoundError:
        return []


def in_record(thread_ts):
    return any(r.get("thread_ts") == thread_ts for r in load_record())


def in_record_for_target(target_ts):
    # Records written by reply_once carry target_ts; one manual record
    # used in_reply_to_event for the same meaning. Accept both.
    return any(
        r.get("target_ts") == target_ts or r.get("in_reply_to_event") == target_ts
        for r in load_record())


def thread_read_page(channel, thread_ts, oldest=None):
    """One page of a thread read (oldest first, parent included).

    Adopts Yama's 2026-11-12 fix #4 (shared in #slack-へようこそ): a
    bare --limit read returns only the oldest page, so in a long thread
    the recent messages -- exactly where a duplicate of a just-sent
    reply would be -- fall outside the checked range. Pass
    next_oldest back as --oldest to walk the whole thread.

    Live-API caveat found 2026-11-12 while answering Yama's question
    about later-page detection: without --oldest the API returns the
    parent plus the *latest* N replies, and next_oldest then points at
    the newest message, so a second page is empty and older replies
    beyond the latest 100 are never seen. Paging must therefore start
    with --oldest set to the parent ts, which makes the API return
    oldest-first pages (verified live against a real thread).
    """
    cmd = [SLACKCLI, "conversations", "read", channel,
           "--thread-ts", thread_ts, "--limit", "100", "--json"]
    if oldest:
        cmd += ["--oldest", oldest]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    out = r.stdout
    i = out.find("{")
    if i < 0:
        raise RuntimeError(f"slackcli thread read gave no JSON: {r.stderr.strip()[:200]}")
    return json.loads(out[i:])


def thread_read_page_retry(channel, thread_ts, oldest=None, attempts=5):
    """thread_read_page with retries.

    Adopted 2026-11-12 from Yama's share in #slack-へようこそ: a failed
    read is often transient (IncompleteRead was seen several times that
    morning), so retry a few times before concluding cannot-verify.
    Fail-closed is kept -- if all attempts fail, the caller still
    refuses to send blind; the retries only reduce replies stuck on a
    transient read failure.

    Retry budget raised 2026-11-12 (same day) from 3 to 5 attempts
    following Yama's progress report in #slack-へようこそ: connection
    errors get up to 5 tries with exponential backoff 1s, 2s, 4s, 8s.
    """
    last_exc = None
    for n in range(attempts):
        try:
            return thread_read_page(channel, thread_ts, oldest)
        except Exception as e:
            last_exc = e
            if n + 1 < attempts:
                # Exponential backoff (1s, 2s, 4s, 8s), adopted 2026-11-12
                # from Neiro's share in #slack-へようこそ (Groq
                # gpt-oss-120b's advice they passed on), raised to 5
                # attempts the same day from Yama's progress report:
                # a transient Slack failure chains when retried
                # immediately, so retry fast first, then wait longer
                # as failures continue. Was a fixed 2s sleep.
                time.sleep(2 ** n)
    raise last_exc


def thread_latest_page(channel, thread_ts):
    """The thread's latest page: the parent plus the newest ~99 replies.

    A bare read (no --oldest) returns the parent plus the *latest*
    replies -- verified live 2026-11-12 and again 2026-11-14 (a bare
    read of the 225-reply audit thread returned the newest messages,
    not the oldest). One page only, so the read stays bounded; used
    by the parroting guard's send-time re-check (see
    duplicate_of_other).
    """
    return thread_read_page_retry(channel, thread_ts)


def thread_all_messages(channel, thread_ts, oldest=None, max_pages=20,
                        fail_closed=False):
    """All messages in the live thread, all pages, oldest first.

    With oldest=None the scan starts at the parent ts (full thread);
    with oldest set it starts there instead -- the windowed read for
    target-mode checks (see window_start): paging still walks forward
    from the given oldest via next_oldest.

    max_pages caps how many pages are read. fail_closed (used by the
    target-mode windowed checks, adopted 2026-11-14 from Yama's
    proposal in #slack-へようこそ, thread 1794583788.831849):
    when the cap is hit while the thread still has more messages,
    raise instead of returning a partial scan -- a partial scan
    cannot prove the reply absent, and a late reply to a question
    buried under more than a page of later posts is out of place
    anyway, so the caller refuses to send rather than guessing.
    Without fail_closed the old behaviour is kept (warn and return
    the partial scan).
    """
    messages = []
    # Start paging at the parent ts: without --oldest the first page is
    # the parent plus the *latest* replies, and next_oldest then points
    # at the newest message, so later pages come back empty and older
    # replies are never seen. With --oldest=thread_ts the API pages
    # oldest-first and next_oldest walks forward correctly.
    oldest = oldest or thread_ts
    for page in range(max_pages):  # safety cap: max_pages x 100 messages
        data = thread_read_page_retry(channel, thread_ts, oldest)
        messages.extend(data.get("messages", []))
        if not data.get("has_more"):
            break
        nxt = data.get("next_oldest")
        if not nxt or nxt == oldest:
            break
        oldest = nxt
    else:
        # Ran out of pages while the thread still has more: the scan
        # stopped short, so a duplicate later in the thread could be
        # missed. Adopted 2026-11-12 from Yama's share in
        # #slack-へようこそ (their agent_tools.py warns at the same
        # 20-page cutoff).
        if fail_closed:
            raise RuntimeError(
                "thread scan stopped at page cap with more messages "
                "unchecked; refusing to decide from a partial window")
        print("warning: thread scan stopped at 20-page cap; "
              "thread may have more messages unchecked",
              file=sys.stderr)
    return messages


def thread_yanayana_replies(channel, thread_ts, since_ts=None):
    """(ts, text) of Yanayana's replies in the live thread, all pages.

    The parent message itself (ts == thread_ts) does not count, so this
    also works for threads Yanayana started. With since_ts set, only
    replies from that ts onward are read (windowed target-mode check),
    capped at one page (100 messages) and fail-closed: if more than a
    page follows the target, the window is too long to verify and the
    caller refuses to send (Yama's 2026-11-14 proposal). Without
    since_ts the full thread is read as before.
    """
    if since_ts is None:
        return [{"ts": m.get("ts"), "text": m.get("text") or ""}
                for m in thread_all_messages(channel, thread_ts)
                if m.get("user") == YANAYANA_USER and m.get("ts") != thread_ts]
    return [{"ts": m.get("ts"), "text": m.get("text") or ""}
            for m in thread_all_messages(channel, thread_ts, oldest=since_ts,
                                        max_pages=1, fail_closed=True)
            if m.get("user") == YANAYANA_USER and m.get("ts") != thread_ts]


def _identical_other_post(messages, wanted):
    """Ts of another author's message in the list identical to wanted."""
    for m in messages:
        if m.get("user") == YANAYANA_USER:
            continue
        if (m.get("text") or "").strip() == wanted:
            return m.get("ts")
    return None


def duplicate_of_other(channel, thread_ts, message, since_ts=None):
    """Ts of another author's thread message whose text is identical
    to the outgoing message, else None.

    Guards against parroting (incident 2026-11-14): a reply went out
    identical to Yama's post from 21 seconds earlier, because the
    draft path had picked up the newest post's text while a reply to
    an earlier message was being composed. The existing dedupe only
    watches for sending twice to the same target; it never compared
    the outgoing text against other authors' posts. The comparison is
    exact after stripping surrounding whitespace, and the parent
    message counts too (echoing the question back is not a reply) --
    in windowed mode only when the parent is inside the window (see
    the module docstring for the boundary).

    Target mode reads the window first (one page from 60s before the
    target, fail-closed), then re-checks the thread's latest page
    (adopted 2026-11-14 from Neiro's share in #slack-へようこそ,
    thread 1794583788.831849: their send-time check looks at the
    latest 100 messages, which catches an identical post older than
    the window that the windowed read cannot see). The remaining hole
    is the same as theirs: an identical post pushed beyond the latest
    100 is still missed. A failed latest-page read is fail-closed
    like the windowed read -- the caller refuses to send blind.
    """
    if not message or not message.strip():
        return None
    wanted = message.strip()
    if since_ts is None:
        messages = thread_all_messages(channel, thread_ts)
        return _identical_other_post(messages, wanted)
    # One page, fail-closed (Yama's 2026-11-14 proposal): more
    # than a page after the target means the window is too long
    # to verify, so the caller refuses to send.
    messages = thread_all_messages(channel, thread_ts, oldest=since_ts,
                                   max_pages=1, fail_closed=True)
    found = _identical_other_post(messages, wanted)
    if found:
        return found
    latest = thread_latest_page(channel, thread_ts)
    return _identical_other_post(latest.get("messages", []), wanted)


def normalize_text(text):
    """Whitespace- and escaping-normalized form for ledger comparison.

    All runs of whitespace (spaces, newlines, tabs) collapse to a
    single space, and the ends are stripped, so a draft that only
    re-wraps an earlier reply still matches it. Yama's proposal
    (2026-11-14): start from exact match after normalizing blank
    space and line breaks. HTML entities are unescaped first
    (Claude Code audit 2026-11-21): Slack returns `&amp;` etc. in
    message text, so without this a delivered reply containing `&`
    never matches the ledger draft.
    """
    return " ".join(html.unescape(text or "").split())


def duplicate_of_own_ledger(thread_ts, message):
    """Reply ts of Yanayana's own earlier reply in the same thread
    whose text matches the outgoing message, else None.

    Local-ledger parroting guard (adopted 2026-11-14 from Yama's
    proposal in #slack-へようこそ, thread 1794614134.265419):
    bot-to-bot turns routinely exceed the 60s window of the
    Slack-read guard, so a draft can repeat Yanayana's own earlier
    summary when the other side rephrases the point, and the echo
    then chains back as the other side's input. The ledger is the
    local record events/replied.jsonl (texts stored since 2026-11-14;
    older records without text are skipped), so the check costs no
    API call and sees the whole thread history. Scope is the same
    thread, as Yama proposed; channel top-level posts are covered by
    channel_send_once.py's own-post check over the live channel.
    """
    if not message or not message.strip():
        return None
    wanted = normalize_text(message)
    if not wanted:
        return None
    for r in load_record():
        if r.get("thread_ts") != thread_ts:
            continue
        text = r.get("text")
        if not text:
            continue
        if normalize_text(text) == wanted:
            return r.get("reply_ts")
    return None


def thread_yanayana_texts(channel, thread_ts, since_ts=None):
    """Texts of Yanayana's replies in the live thread, all pages.

    Kept as a thin wrapper over thread_yanayana_replies for callers
    that only need the texts (already_replied's duplicate check).
    With since_ts set, only replies from that ts onward are read.
    """
    return [r["text"] for r in thread_yanayana_replies(channel, thread_ts,
                                                       since_ts=since_ts)]


def thread_has_yanayana_reply(channel, thread_ts):
    return bool(thread_yanayana_texts(channel, thread_ts))


def recovery_check(channel, thread_ts, message, attempts=5, since_ts=None):
    """After a lost send response, re-read the thread to see whether
    the reply actually landed.

    Adopted 2026-11-12 from Yama's share in #slack-へようこそ: when
    the send response is lost, retry the recovery confirmation up to
    5 times (raised from 3 the same day, per Yama's progress report:
    connection errors get up to 5 tries, backoff 1s, 2s, 4s, 8s); if
    the reply is found, treat it as delivered; if it is still not
    found, record ambiguous_post_outcome and do not repost
    blindly. Returns the found reply's ts (str), False (reads
    succeeded and the text is absent -- confirmed not delivered), or
    None (reads themselves failed -- could not verify).

    The reply ts is returned (not just True) so the caller can record
    it in the replies ledger -- adopted 2026-11-12 from Yama's
    self-review proposal in #slack-へようこそ: a recovered reply
    recorded without its ts leaves the ledger unable to say which
    reply landed, which is the same gap as their missing replies-table
    row. Recording only the fact of delivery is not enough.
    """
    for n in range(attempts):
        try:
            for r in thread_yanayana_replies(channel, thread_ts,
                                             since_ts=since_ts):
                if r["text"] == message:
                    return r["ts"]
            return False
        except Exception as e:
            print(f"warning: recovery check attempt {n + 1} failed ({e})",
                  file=sys.stderr)
            if n + 1 < attempts:
                # Same exponential backoff (1s, 2s, 4s, 8s) as
                # thread_read_page_retry -- Neiro's 2026-11-12 share,
                # raised to 5 attempts per Yama's progress report.
                time.sleep(2 ** n)
    return None


def _append_jsonl(path, record):
    """Append one JSON line, serialized with flock like msgstate.py
    (adopted 2026-11-14 from Neiro's review of the published code:
    plain O_APPEND is nearly safe for short lines, but two processes
    appending at once can still interleave)."""
    os.makedirs(EVENTS, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            f.flush()
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def record_reply(channel, thread_ts, target_ts, reply_ts, recovered=False,
                 message=None, event_id=None, request_id=None):
    # The sent text is stored (added 2026-11-14, answering Yama's
    # question in #slack-へようこそ): until now the record kept only
    # ts values, so a later incident could only be reconstructed from
    # the live thread, not from local records. With the text in the
    # ledger, what was actually sent is provable offline.
    # The originating event_id is stored too when the caller knows it
    # (added 2026-11-15 from Neiro's correction in #slack-へようこそ:
    # their reconcile fix started from the record side because the
    # ambiguous record carried no mark; the same was true of this
    # ledger and of the correction entries -- a later reader could not
    # tell from the record alone which event a reply answers. The
    # field is the originating event id (audit); it does not claim the
    # reply itself carries that stamp -- manual and resend paths send
    # without one. Entries recorded without one keep the old shape.
    entry = {
        "thread_ts": thread_ts, "reply_ts": reply_ts,
        "channel": channel,
        "target_ts": target_ts, "in_reply_to_event": target_ts,
        "recovered": recovered,
        "at": datetime.now(timezone.utc).isoformat(),
    }
    if message is not None:
        entry["text"] = message
    if event_id is not None:
        entry["event_id"] = event_id
    if request_id is not None:
        # The auto-generated request_id stamp (2026-11-23, the
        # fallback-key hole fix): a reply sent without an originating
        # event is stamped with a request_id instead of going out
        # unstamped, and the ledger keeps the mark so a later reader
        # can tell which stamp proves this reply -- the same "keep
        # the mark" principle as event_id above.
        entry["request_id"] = request_id
    _append_jsonl(REPLIED, entry)


def load_ambiguous():
    try:
        with open(AMBIGUOUS, "r", encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
    except Exception:
        return []


def resolve_ambiguous(channel, thread_ts, target_ts, event_id=None):
    """Correct an ambiguous_post_outcome record once a later check
    proves the reply did land.

    Adopted 2026-11-12 from Yama's self-review proposal in
    #slack-へようこそ: when a response is lost and the recovery
    confirmation finds the reply, the ledger must be corrected --
    leaving "reply not posted" standing misleads later triage. The
    original ambiguous entry is kept as history; a
    resolved_by_thread_check entry is appended for the same target
    so any reader sees the correction. The correction entry carries
    the originating event_id when the caller knows it (added
    2026-11-15 from Neiro's correction in #slack-へようこそ: their
    fix keeps the mark on the correction entries too, so the
    correction is attributable to its event without re-reading the
    thread). No-op when there is no unresolved ambiguous entry for
    the target, or one was already resolved. Returns True when a
    resolution entry was appended.
    """
    entries = load_ambiguous()
    unresolved = [e for e in entries
                  if e.get("outcome") == "ambiguous_post_outcome"
                  and e.get("target_ts") == target_ts]
    already = [e for e in entries
               if e.get("outcome") == "resolved_by_thread_check"
               and e.get("target_ts") == target_ts]
    if not unresolved or already:
        return False
    correction = {
        "outcome": "resolved_by_thread_check",
        "thread_ts": thread_ts, "target_ts": target_ts,
        "channel": channel,
        "note": ("reply found in thread on a later pre-send check; "
                 "the earlier ambiguous_post_outcome record was wrong"),
        "at": datetime.now(timezone.utc).isoformat(),
    }
    if event_id is not None:
        correction["event_id"] = event_id
    _append_jsonl(AMBIGUOUS, correction)
    return True


def close_no_text(channel, thread_ts, target_ts):
    """Close a no-text ambiguous entry as unmatchable, not resolved.

    Agreed with Yama 2026-11-14 in #slack-へようこそ (Yanayana's
    closed_no_text proposal): an entry without message text can never
    be proven delivered by text matching, so keeping it "unresolved"
    forever only pollutes the unresolved count with entries that
    cannot be checked. Closing appends a closed_no_text record --
    distinct from resolved_by_thread_check -- and the entry leaves
    the reconcile sweep. The original entry stays as history, and a
    later pre-send check can still append resolved_by_thread_check
    for the same target if the reply is actually found in the thread.
    No-op when there is no unresolved ambiguous entry for the target,
    or one was already closed or resolved. Returns True when a
    closing entry was appended.
    """
    entries = load_ambiguous()
    unresolved = [e for e in entries
                  if e.get("outcome") == "ambiguous_post_outcome"
                  and e.get("target_ts") == target_ts]
    already = [e for e in entries
               if e.get("outcome") in ("closed_no_text",
                                      "resolved_by_thread_check")
               and e.get("target_ts") == target_ts]
    if not unresolved or already:
        return False
    _append_jsonl(AMBIGUOUS, {
        "outcome": "closed_no_text",
        "thread_ts": thread_ts, "target_ts": target_ts,
        "channel": channel,
        "note": ("no message text was recorded, so the entry can never "
                 "be matched against the thread; closed as unmatchable, "
                 "not resolved"),
        "at": datetime.now(timezone.utc).isoformat(),
    })
    return True


def record_ambiguous(channel, thread_ts, target_ts, reason, message=None,
                     event_id=None, request_id=None):
    # The message text is stored so a later periodic reconciliation
    # (reconcile_ambiguous.py, adopted 2026-11-12 from Yama's share)
    # can match the entry against the live thread by exact text.
    # Without the text an ambiguous entry can never be proven
    # delivered except by a later pre-send check for the same target.
    # The originating event_id is stored too (added 2026-11-15 from
    # Yama's confirmation in #slack-へようこそ: their reconcile
    # excludes replies stamped with a different event_id): the sweep
    # can then tell a same-text reply to *this* event from one to
    # another event. Entries without one keep the old text-only match.
    entry = {
        "outcome": "ambiguous_post_outcome",
        "thread_ts": thread_ts, "target_ts": target_ts,
        "channel": channel, "reason": reason,
        "at": datetime.now(timezone.utc).isoformat(),
    }
    if message is not None:
        entry["message"] = message
    if event_id is not None:
        entry["event_id"] = event_id
    if request_id is not None:
        entry["request_id"] = request_id
    _append_jsonl(AMBIGUOUS, entry)


def metadata_reply_found(channel, thread_ts, event_id=None, request_id=None):
    """Ts of our metadata-stamped reply for this event, else None.

    Reads the thread with include_all_metadata and matches our
    yanayana_reply stamp (post_metadata.find_reply). Exactly one of
    event_id / request_id: an event-driven reply is matched by its
    originating event_id, a request_id-stamped reply by its
    auto-generated nonce. Raises RuntimeError when the token cannot
    be resolved or the read fails -- the caller treats that as
    "metadata check unavailable", not as "not replied", and falls
    back to the text check.
    """
    token = post_metadata.resolve_token()
    if not token:
        raise RuntimeError("metadata check: token not found")
    found = post_metadata.find_reply(channel, thread_ts, token,
                                     event_id=event_id,
                                     request_id=request_id)
    return found.get("ts") if found else None


def stamped_texts(channel, thread_ts, event_id, since_ts=None):
    """Texts of Yanayana's replies, excluding other events' replies.

    Reads the thread with include_all_metadata and drops our own
    messages stamped with a *different* event_id: a same-text reply
    to another event is that event's answer, not this one's
    duplicate, so it must not block this send. Messages with no
    stamp (sent before stamping existed, or manual posts) stay as
    text candidates, as do messages stamped with this event_id
    (already caught by metadata_reply_found, kept here for safety).

    Added 2026-11-15 from Neiro's correction in #slack-へようこそ:
    Yama had shared that their text matching excluded replies
    stamped with a different event_id, then found the guard was
    never in their code -- their ledger and thread text checks
    read without metadata, so a same-text reply to another event
    counted as a duplicate. Our text fallback had the same gap
    (slackcli read, no metadata), so the guard belongs here too.
    Raises RuntimeError when the token cannot be resolved or the
    read fails; the caller falls back to the slackcli text read.
    """
    token = post_metadata.resolve_token()
    if not token:
        raise RuntimeError("stamped text check: token not found")
    messages = post_metadata.read_thread(channel, thread_ts, token,
                                         oldest=since_ts)
    texts = []
    for m in messages:
        if m.get("user") != YANAYANA_USER or m.get("ts") == thread_ts:
            continue
        stamped = post_metadata.stamp_event_id(m)
        if stamped is not None and stamped != str(event_id):
            continue
        texts.append(m.get("text") or "")
    return texts


def already_replied(channel, thread_ts, target_ts=None, message=None,
                    event_id=None, request_id=None):
    try:
        if target_ts:
            if in_record_for_target(target_ts):
                return "record"
        elif in_record(thread_ts):
            return "record"
    except Exception as e:
        # Ledger unreadable: cannot verify, refuse to send blind
        # rather than risk a duplicate reply.
        print(f"warning: record check failed ({e}); cannot verify",
              file=sys.stderr)
        return "unknown"
    if event_id or request_id:
        # Metadata first: a stamp proves this event was answered
        # even when the reply text differs from the draft (e.g. a
        # retry with edited wording). A failed metadata read falls
        # back to the text check below, which stays fail-closed on
        # its own read failure -- the metadata check is an extra
        # proof channel, not a new single point of failure.
        # A request_id stamp has no event_id, so stamped_texts
        # (event_id-only) cannot exclude it; it stays a plain text
        # candidate below, like any manual post's reply.
        try:
            if metadata_reply_found(channel, thread_ts,
                                    event_id=event_id,
                                    request_id=request_id):
                return "thread"
        except Exception as e:
            print(f"warning: metadata check failed ({e}); falling back "
                  f"to text check", file=sys.stderr)
    try:
        # Target mode reads only the window from 60s before the
        # target onward (see window_start): a duplicate of our reply
        # can only exist after the target event, and a same-text reply
        # to an earlier message is not this reply's duplicate. Manual
        # mode (no target) has no anchor and keeps the full-thread
        # read, as Yama's design does for event-less posts.
        since_ts = window_start(target_ts) if target_ts else None
        if event_id:
            # Metadata-aware text check: exclude our replies stamped
            # with a different event_id (see stamped_texts). When
            # the stamped read fails, fall back to the slackcli text
            # read -- degraded to the old gap for that run, but the
            # warning says so, and the read itself stays fail-closed.
            try:
                texts = stamped_texts(channel, thread_ts, event_id,
                                      since_ts=since_ts)
            except Exception as e:
                print(f"warning: stamped text check failed ({e}); "
                      f"falling back to slackcli text check",
                      file=sys.stderr)
                texts = thread_yanayana_texts(channel, thread_ts,
                                              since_ts=since_ts)
        else:
            texts = thread_yanayana_texts(channel, thread_ts, since_ts=since_ts)
        if target_ts:
            # Other replies in the thread answer other messages and do
            # not block this one; only the exact same text (what a
            # crash-and-retry would resend) counts as a duplicate.
            # Compared normalized (see normalize_text): Slack escapes
            # `&` etc. in returned text, so a raw exact match misses
            # a delivered reply (Claude Code audit 2026-11-21).
            if message and any(normalize_text(t) == normalize_text(message)
                               for t in texts):
                return "thread"
        elif texts:
            return "thread"
    except Exception as e:
        # Thread read failed: fall back to the record only, and say so.
        # If the record has no proof we cannot verify; refuse to send
        # blind rather than risk a duplicate.
        print(f"warning: thread check failed ({e}); record-only check", file=sys.stderr)
        return "unknown"
    return None


def send(channel, thread_ts, message, event_id=None, request_id=None):
    """Send one thread reply; returns the sent message's ts.

    With event_id the reply is stamped with metadata (event_type
    "yanayana_reply", event_payload.event_id = the originating Slack
    event_id) via post_metadata.py, so a later run can match it by
    stamp. With request_id (no originating event: a fallback-key or
    manual send) the same stamp carries the auto-generated nonce
    instead -- the 2026-11-23 fallback-key hole fix: no send through
    this path goes out unstamped any more. With neither id the
    legacy slackcli path is used (direct send() callers only; both
    CLI entry points auto-generate a request_id first, so they never
    reach this branch).
    Raises RuntimeError on failure so the caller runs its
    lost-response recovery check.
    """
    if event_id or request_id:
        ok, result = post_metadata.send_reply(channel, thread_ts,
                                              message, event_id=event_id,
                                              request_id=request_id)
        if not ok:
            raise RuntimeError(f"post_metadata send failed: {result}")
        return result.get("ts") or result.get("message", {}).get("ts")
    r = subprocess.run(
        [SLACKCLI, "messages", "send", "--recipient-id", channel,
         "--thread-ts", thread_ts, "--message", message, "--json"],
        capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"slackcli send failed: {r.stderr.strip()[:300]}")
    out = r.stdout
    i = out.find("{")
    data = json.loads(out[i:]) if i >= 0 else {}
    return data.get("ts") or data.get("message", {}).get("ts")


def main(argv):
    channel = thread_ts = message = target_ts = event_id = request_id = None
    check_only = False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--channel" and i + 1 < len(argv):
            channel = argv[i + 1]; i += 2
        elif a == "--thread-ts" and i + 1 < len(argv):
            thread_ts = argv[i + 1]; i += 2
        elif a == "--target-ts" and i + 1 < len(argv):
            target_ts = argv[i + 1]; i += 2
        elif a == "--event-id" and i + 1 < len(argv):
            event_id = argv[i + 1]; i += 2
        elif a == "--request-id" and i + 1 < len(argv):
            request_id = argv[i + 1]; i += 2
        elif a == "--message" and i + 1 < len(argv):
            message = argv[i + 1]; i += 2
        elif a == "--check-only":
            check_only = True; i += 1
        else:
            print(f"unknown argument: {a}", file=sys.stderr)
            return 1
    if not channel or not thread_ts or (not check_only and not message):
        print("usage: reply_once.py --channel C... --thread-ts T... "
              "[--target-ts M...] [--event-id Ev...] [--request-id UUID] "
              "[--message TEXT] [--check-only]", file=sys.stderr)
        return 1

    # Window for this run's target-mode live-thread checks (the
    # duplicate-text guard and the recovery checks); already_replied
    # computes the same window itself. None in manual mode: no target
    # event, so the full thread is read.
    since_ts = window_start(target_ts) if target_ts else None

    found = already_replied(channel, thread_ts, target_ts=target_ts,
                            message=message, event_id=event_id,
                            request_id=request_id)
    if found == "unknown":
        print("cannot-verify: thread read failed and no record entry; "
              "refusing to send", file=sys.stderr)
        return 2
    if found:
        # A reply is already proven for this target: if an earlier
        # send was recorded ambiguous, correct that record now.
        if target_ts:
            resolve_ambiguous(channel, thread_ts, target_ts,
                              event_id=event_id)
            try:
                already_recorded = in_record_for_target(target_ts)
            except Exception as e:
                # Reply already proven by the thread; a ledger that
                # cannot be read only skips the repair record, it
                # must not crash the run after the proof.
                print(f"warning: could not check ledger for thread-found "
                      f"reply ({e}); skipping repair record", file=sys.stderr)
                already_recorded = True
            if found == "thread" and message and not already_recorded:
                # Adopted 2026-11-12 from Yama's self-review proposal in
                # #slack-へようこそ (their record-reply path): a reply
                # proven by the thread but missing from the ledger is
                # recorded now, with its reply ts, so the ledger stays
                # the single source of truth for dedupe instead of
                # relying on the thread scan every time.
                try:
                    for r in thread_yanayana_replies(channel, thread_ts,
                                                     since_ts=since_ts):
                        if r["text"] == message:
                            record_reply(channel, thread_ts, target_ts,
                                         r["ts"], recovered=True,
                                         message=r["text"],
                                         event_id=event_id,
                                         request_id=request_id)
                            break
                except Exception as e:
                    print(f"warning: could not record thread-found reply ({e})",
                          file=sys.stderr)
        print(f"already-replied ({found})")
        return 0
    if check_only:
        print("not-replied")
        return 0

    # Local-ledger parroting guard first: it costs no API call, so a
    # ledger hit skips the history read below. Order adopted
    # 2026-11-14 from Yama's confirmation (thread
    # 1794614134.265419): their ledger check is the mainline step
    # before the Slack history scan, not a fallback.
    try:
        own_ts = duplicate_of_own_ledger(thread_ts, message)
    except Exception as e:
        # Same fail-closed stance as the already-replied check: when
        # the ledger cannot be read, do not send blind.
        print(f"cannot-verify: own-ledger check failed ({e}); "
              "refusing to send", file=sys.stderr)
        return 2
    if own_ts:
        print(f"duplicate-own-ledger: outgoing message repeats "
              f"Yanayana's own earlier reply in this thread "
              f"({own_ts}); refusing to send", file=sys.stderr)
        return 5

    try:
        dup_ts = duplicate_of_other(channel, thread_ts, message,
                                    since_ts=since_ts)
    except Exception as e:
        # Same fail-closed stance as the already-replied check: when
        # the thread cannot be read, do not send blind.
        print(f"cannot-verify: duplicate-text check failed ({e}); "
              "refusing to send", file=sys.stderr)
        return 2
    if dup_ts:
        print(f"duplicate-text: outgoing message is identical to "
              f"another author's post ({dup_ts}); refusing to send",
              file=sys.stderr)
        return 4

    # No originating event and no caller-supplied nonce: the stamp
    # is a request_id, so generate one here -- every send through
    # this entry point carries a stamp (2026-11-23 three-way
    # agreement; event_id is provenance and is never fabricated).
    # Generated lazily, after the guards, so a refused send mints
    # no id and the pre-send checks never see a fresh nonce (a
    # metadata check on it could never match a previous attempt).
    # The id is recorded with the reply, so reconcile can match the
    # landed stamp to this send. send_reply_locked.py generates one
    # earlier for the same reason; whichever layer mints it, exactly
    # one stamp reaches the send.
    if not event_id and not request_id:
        request_id = str(uuid.uuid4())
        print(f"note: no event_id; stamping with generated request_id "
              f"{request_id}", file=sys.stderr)

    reply_ts = None
    try:
        reply_ts = send(channel, thread_ts, message, event_id=event_id,
                        request_id=request_id)
    except Exception as e:
        # The send response was lost: the reply may still have landed.
        # Confirm from the live thread before deciding anything.
        print(f"warning: send failed ({e}); running recovery check",
              file=sys.stderr)
        outcome = recovery_check(channel, thread_ts, message,
                                 since_ts=since_ts)
        if outcome:
            record_reply(channel, thread_ts, target_ts, outcome, recovered=True,
                         message=message, event_id=event_id,
                         request_id=request_id)
            # The reply_ts stays in the success line (same shape as
            # the plain send below): send_reply_locked.py parses
            # "sent reply_ts=" to finalize, and the recovered shape
            # without it was misread as a send failure there, which
            # left the replylog record and the state finalization
            # undone for a reply that had in fact landed.
            print(f"sent reply_ts={outcome} (recovered: reply found in thread after lost send response)")
            return 0
        reason = ("send-response-lost; recovery check could not verify delivery"
                  if outcome is None else
                  "send-response-lost; recovery check confirmed reply absent")
        record_ambiguous(channel, thread_ts, target_ts, reason, message,
                         event_id=event_id, request_id=request_id)
        print(f"ambiguous_post_outcome: {reason}; not reposting blindly",
              file=sys.stderr)
        return 3
    if not reply_ts:
        # Send reported success but gave no ts: same recovery flow.
        print("warning: send returned no ts; running recovery check",
              file=sys.stderr)
        outcome = recovery_check(channel, thread_ts, message,
                                 since_ts=since_ts)
        if outcome:
            record_reply(channel, thread_ts, target_ts, outcome, recovered=True,
                         message=message, event_id=event_id,
                         request_id=request_id)
            print(f"sent reply_ts={outcome} (recovered: reply found in thread, send gave no ts)")
            return 0
        reason = ("send gave no ts; recovery check could not verify delivery"
                  if outcome is None else
                  "send gave no ts; recovery check confirmed reply absent")
        record_ambiguous(channel, thread_ts, target_ts, reason, message,
                         event_id=event_id, request_id=request_id)
        print(f"ambiguous_post_outcome: {reason}; not reposting blindly",
              file=sys.stderr)
        return 3
    record_reply(channel, thread_ts, target_ts, reply_ts, message=message,
                 event_id=event_id, request_id=request_id)
    print(f"sent reply_ts={reply_ts}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
