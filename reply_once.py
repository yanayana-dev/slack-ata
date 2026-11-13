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

Granularity: with --target-ts (the ts of the message being answered),
idempotency is per target message -- a reply to a different message in
the same thread is a new reply, not a duplicate (the thread-only check
blocked legitimate second replies; fixed 2026-11-12). In target mode
the live-thread check only blocks when the exact same text is already
in the thread, which is what a crash-and-retry would resend. Without
--target-ts the original thread-level behavior is kept.

Usage:
  reply_once.py --channel C0... --thread-ts 1794471381.083589 --message "..."
  reply_once.py --channel C0... --thread-ts 1794471381.083589 --check-only
    # prints "already-replied" or "not-replied", sends nothing
  reply_once.py --channel C0... --thread-ts T... --target-ts M... --message "..."
    # per-target-message idempotency

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
"""
import fcntl
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
SLACKCLI = os.path.join(BASE, "bin", "slackcli")
EVENTS = os.path.join(BASE, "events")
REPLIED = os.path.join(EVENTS, "replied.jsonl")
AMBIGUOUS = os.path.join(EVENTS, "ambiguous_posts.jsonl")

YANAYANA_USER = "U0EXAMPLEBOT"  # Yanayana bot user id in muse取締役会


def load_record():
    try:
        with open(REPLIED, "r", encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
    except Exception:
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


def thread_all_messages(channel, thread_ts):
    """All messages in the live thread, all pages, oldest first."""
    messages = []
    # Start paging at the parent ts: without --oldest the first page is
    # the parent plus the *latest* replies, and next_oldest then points
    # at the newest message, so later pages come back empty and older
    # replies are never seen. With --oldest=thread_ts the API pages
    # oldest-first and next_oldest walks forward correctly.
    oldest = thread_ts
    for page in range(20):  # safety cap: 20 pages x 100 messages
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
        print("warning: thread scan stopped at 20-page cap; "
              "thread may have more messages unchecked",
              file=sys.stderr)
    return messages


def thread_yanayana_replies(channel, thread_ts):
    """(ts, text) of Yanayana's replies in the live thread, all pages.

    The parent message itself (ts == thread_ts) does not count, so this
    also works for threads Yanayana started.
    """
    return [{"ts": m.get("ts"), "text": m.get("text") or ""}
            for m in thread_all_messages(channel, thread_ts)
            if m.get("user") == YANAYANA_USER and m.get("ts") != thread_ts]


def duplicate_of_other(channel, thread_ts, message):
    """Ts of another author's thread message whose text is identical
    to the outgoing message, else None.

    Guards against parroting (incident 2026-11-14): a reply went out
    identical to Yama's post from 21 seconds earlier, because the
    draft path had picked up the newest post's text while a reply to
    an earlier message was being composed. The existing dedupe only
    watches for sending twice to the same target; it never compared
    the outgoing text against other authors' posts. The comparison is
    exact after stripping surrounding whitespace, and the parent
    message counts too (echoing the question back is not a reply).
    """
    if not message or not message.strip():
        return None
    wanted = message.strip()
    for m in thread_all_messages(channel, thread_ts):
        if m.get("user") == YANAYANA_USER:
            continue
        if (m.get("text") or "").strip() == wanted:
            return m.get("ts")
    return None


def thread_yanayana_texts(channel, thread_ts):
    """Texts of Yanayana's replies in the live thread, all pages.

    Kept as a thin wrapper over thread_yanayana_replies for callers
    that only need the texts (already_replied's duplicate check).
    """
    return [r["text"] for r in thread_yanayana_replies(channel, thread_ts)]


def thread_has_yanayana_reply(channel, thread_ts):
    return bool(thread_yanayana_texts(channel, thread_ts))


def recovery_check(channel, thread_ts, message, attempts=5):
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
            for r in thread_yanayana_replies(channel, thread_ts):
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
                 message=None):
    # The sent text is stored (added 2026-11-14, answering Yama's
    # question in #slack-へようこそ): until now the record kept only
    # ts values, so a later incident could only be reconstructed from
    # the live thread, not from local records. With the text in the
    # ledger, what was actually sent is provable offline.
    entry = {
        "thread_ts": thread_ts, "reply_ts": reply_ts,
        "channel": channel,
        "target_ts": target_ts, "in_reply_to_event": target_ts,
        "recovered": recovered,
        "at": datetime.now(timezone.utc).isoformat(),
    }
    if message is not None:
        entry["text"] = message
    _append_jsonl(REPLIED, entry)


def load_ambiguous():
    try:
        with open(AMBIGUOUS, "r", encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
    except Exception:
        return []


def resolve_ambiguous(channel, thread_ts, target_ts):
    """Correct an ambiguous_post_outcome record once a later check
    proves the reply did land.

    Adopted 2026-11-12 from Yama's self-review proposal in
    #slack-へようこそ: when a response is lost and the recovery
    confirmation finds the reply, the ledger must be corrected --
    leaving "reply not posted" standing misleads later triage. The
    original ambiguous entry is kept as history; a
    resolved_by_thread_check entry is appended for the same target
    so any reader sees the correction. No-op when there is no
    unresolved ambiguous entry for the target, or one was already
    resolved. Returns True when a resolution entry was appended.
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
    _append_jsonl(AMBIGUOUS, {
        "outcome": "resolved_by_thread_check",
        "thread_ts": thread_ts, "target_ts": target_ts,
        "channel": channel,
        "note": ("reply found in thread on a later pre-send check; "
                 "the earlier ambiguous_post_outcome record was wrong"),
        "at": datetime.now(timezone.utc).isoformat(),
    })
    return True


def record_ambiguous(channel, thread_ts, target_ts, reason, message=None):
    # The message text is stored so a later periodic reconciliation
    # (reconcile_ambiguous.py, adopted 2026-11-12 from Yama's share)
    # can match the entry against the live thread by exact text.
    # Without the text an ambiguous entry can never be proven
    # delivered except by a later pre-send check for the same target.
    entry = {
        "outcome": "ambiguous_post_outcome",
        "thread_ts": thread_ts, "target_ts": target_ts,
        "channel": channel, "reason": reason,
        "at": datetime.now(timezone.utc).isoformat(),
    }
    if message is not None:
        entry["message"] = message
    _append_jsonl(AMBIGUOUS, entry)


def already_replied(channel, thread_ts, target_ts=None, message=None):
    if target_ts:
        if in_record_for_target(target_ts):
            return "record"
    elif in_record(thread_ts):
        return "record"
    try:
        texts = thread_yanayana_texts(channel, thread_ts)
        if target_ts:
            # Other replies in the thread answer other messages and do
            # not block this one; only the exact same text (what a
            # crash-and-retry would resend) counts as a duplicate.
            if message and message in texts:
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


def send(channel, thread_ts, message):
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
    channel = thread_ts = message = target_ts = None
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
        elif a == "--message" and i + 1 < len(argv):
            message = argv[i + 1]; i += 2
        elif a == "--check-only":
            check_only = True; i += 1
        else:
            print(f"unknown argument: {a}", file=sys.stderr)
            return 1
    if not channel or not thread_ts or (not check_only and not message):
        print("usage: reply_once.py --channel C... --thread-ts T... "
              "[--target-ts M...] [--message TEXT] [--check-only]", file=sys.stderr)
        return 1

    found = already_replied(channel, thread_ts, target_ts=target_ts, message=message)
    if found == "unknown":
        print("cannot-verify: thread read failed and no record entry; "
              "refusing to send", file=sys.stderr)
        return 2
    if found:
        # A reply is already proven for this target: if an earlier
        # send was recorded ambiguous, correct that record now.
        if target_ts:
            resolve_ambiguous(channel, thread_ts, target_ts)
            if found == "thread" and message and not in_record_for_target(target_ts):
                # Adopted 2026-11-12 from Yama's self-review proposal in
                # #slack-へようこそ (their record-reply path): a reply
                # proven by the thread but missing from the ledger is
                # recorded now, with its reply ts, so the ledger stays
                # the single source of truth for dedupe instead of
                # relying on the thread scan every time.
                try:
                    for r in thread_yanayana_replies(channel, thread_ts):
                        if r["text"] == message:
                            record_reply(channel, thread_ts, target_ts,
                                         r["ts"], recovered=True,
                                         message=r["text"])
                            break
                except Exception as e:
                    print(f"warning: could not record thread-found reply ({e})",
                          file=sys.stderr)
        print(f"already-replied ({found})")
        return 0
    if check_only:
        print("not-replied")
        return 0

    try:
        dup_ts = duplicate_of_other(channel, thread_ts, message)
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

    reply_ts = None
    try:
        reply_ts = send(channel, thread_ts, message)
    except Exception as e:
        # The send response was lost: the reply may still have landed.
        # Confirm from the live thread before deciding anything.
        print(f"warning: send failed ({e}); running recovery check",
              file=sys.stderr)
        outcome = recovery_check(channel, thread_ts, message)
        if outcome:
            record_reply(channel, thread_ts, target_ts, outcome, recovered=True,
                         message=message)
            print("sent (recovered: reply found in thread after lost send response)")
            return 0
        reason = ("send-response-lost; recovery check could not verify delivery"
                  if outcome is None else
                  "send-response-lost; recovery check confirmed reply absent")
        record_ambiguous(channel, thread_ts, target_ts, reason, message)
        print(f"ambiguous_post_outcome: {reason}; not reposting blindly",
              file=sys.stderr)
        return 3
    if not reply_ts:
        # Send reported success but gave no ts: same recovery flow.
        print("warning: send returned no ts; running recovery check",
              file=sys.stderr)
        outcome = recovery_check(channel, thread_ts, message)
        if outcome:
            record_reply(channel, thread_ts, target_ts, outcome, recovered=True,
                         message=message)
            print("sent (recovered: reply found in thread, send gave no ts)")
            return 0
        reason = ("send gave no ts; recovery check could not verify delivery"
                  if outcome is None else
                  "send gave no ts; recovery check confirmed reply absent")
        record_ambiguous(channel, thread_ts, target_ts, reason, message)
        print(f"ambiguous_post_outcome: {reason}; not reposting blindly",
              file=sys.stderr)
        return 3
    record_reply(channel, thread_ts, target_ts, reply_ts, message=message)
    print(f"sent reply_ts={reply_ts}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
