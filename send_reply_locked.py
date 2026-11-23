#!/usr/bin/env python3
"""Send a Slack thread reply with check -> send -> record serialized.

Problem (raised by Yama 2026-11-14 in #slack-へようこそ, thread
1794583788.831849): the pre-send check and the send are two separate
steps, so two concurrent hook workers can both check ("not replied"),
both send, and both record -- a duplicate post. reply_once.py already
does check -> send -> record in one process, but its flock only
serializes the ledger append, not the whole sequence, and the hook's
manual flow (replylog.py check -> slackcli send -> replylog.py record)
has no serialization at all.

Fix: hold one exclusive flock on events/send.lock across the whole
sequence -- replylog check (replies.jsonl), reply_once.py (its own
replied.jsonl record plus the live-thread same-text check, the send
itself, and its recovery path), and the replylog record. A second
worker blocks on the lock until the first has sent AND recorded, so
its check then sees the reply and sends nothing. flock is released
automatically when the holder process exits, even on crash, so no
stale lock can strand later sends.

Both ledgers are kept in sync on the success path: reply_once.py
records into events/replied.jsonl and this wrapper additionally
records the same send into events/replies.jsonl (with the sent text),
so either ledger's check blocks a later duplicate.

State finalization (2026-11-23, the bookkeeping seam): with
--state-key KEY the wrapper also marks the message done in
msgstate.py, inside the same lock, on every path where the reply is
known to exist (sent now, or already-replied per either check). The
hook flow used to do send -> replylog record -> mark done as three
separate agent steps; a run that ended after the send lost the two
writes and the answered message stayed pending (observed 2026-11-18,
2026-11-22, 2026-11-23). One process now performs the whole
finalization, and msgstate.py's heal-replied sweep (from the reply
ledgers) is the backstop if this process itself dies mid-sequence.

Usage:
  send_reply_locked.py --channel C... --thread-ts T... --target-ts M...
                       --message "..." [--event-id Ev...]
                       [--state-key KEY] [--dry-run]
  --dry-run acquires the lock and runs the checks, then reports
  what would be sent without sending.
  --event-id is passed through to reply_once.py: the reply is then
  sent metadata-stamped via post_metadata.py and the pre-send
  check looks for our metadata stamp first (see reply_once.py).
  Without it the slackcli send and text-only check are used.

Exit codes: 0 = sent, already-replied (no-op), dry-run ok, or a
duplicate-text/own-ledger guard refused the send (nothing was sent);
1 = usage error; 2 = cannot-verify / send failed / ledger record
failed (fail-closed, nothing was reposted blindly); 3 = ambiguous
post outcome, propagated from reply_once.py (the send response was
lost; see events/ambiguous_posts.jsonl -- do not repost blindly).
"""
import fcntl
import json
import os
import subprocess
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
EVENTS = os.path.join(BASE, "events")
LOCK_PATH = os.path.join(EVENTS, "send.lock")
REPLYLOG = os.path.join(BASE, "replylog.py")
REPLY_ONCE = os.path.join(BASE, "reply_once.py")
MSGSTATE = os.path.join(BASE, "msgstate.py")


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120)


def main(argv):
    channel = thread_ts = target_ts = message = event_id = None
    state_key = None
    dry_run = False
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
        elif a == "--state-key" and i + 1 < len(argv):
            state_key = argv[i + 1]; i += 2
        elif a == "--message" and i + 1 < len(argv):
            message = argv[i + 1]; i += 2
        elif a == "--dry-run":
            dry_run = True; i += 1
        else:
            print(f"unknown argument: {a}", file=sys.stderr)
            return 1
    if not channel or not thread_ts or not target_ts or (not dry_run and not message):
        print("usage: send_reply_locked.py --channel C... --thread-ts T... "
              "--target-ts M... --message TEXT [--event-id Ev...] "
              "[--state-key KEY] [--dry-run]", file=sys.stderr)
        return 1
    if not event_id and state_key and state_key.startswith("Ev"):
        # The hook flow keys msgstate entries by event_id, so a
        # --state-key that looks like an event id IS the originating
        # event. Default the stamp from it instead of sending
        # unstamped: Yama's 2026-11-23 metadata finding -- an
        # unstamped reply is invisible to reconcile detection and to
        # re-entry blocking, and the hole is exactly "the caller
        # forgot --event-id". Fallback channel:ts keys do not start
        # with "Ev", so they still send unstamped as before.
        event_id = state_key
        print(f"note: --event-id not given; stamping from --state-key {event_id}",
              file=sys.stderr)

    os.makedirs(EVENTS, exist_ok=True)
    # One exclusive lock for the whole check -> send -> record
    # sequence. Blocking acquire; released automatically on exit.
    with open(LOCK_PATH, "a+", encoding="utf-8") as lockf:
        fcntl.flock(lockf, fcntl.LOCK_EX)
        try:
            return _send_locked(channel, thread_ts, target_ts, message,
                                dry_run, event_id, state_key)
        finally:
            fcntl.flock(lockf, fcntl.LOCK_UN)


def _mark_done(state_key):
    """Finalize the message in msgstate.py (best effort, warned).

    Runs while send.lock is held; msgstate takes its own state lock
    inside, and no path holds the state lock while waiting on
    send.lock, so the nesting cannot deadlock. A failure here does
    not change the send outcome: both ledgers already prove the
    reply, and msgstate's heal-replied sweep marks the entry done
    from them on the next poll. Returns True when the entry is done.
    """
    if not state_key:
        return False
    r = run([sys.executable, MSGSTATE, "mark", state_key, "done"])
    if r.returncode == 0 and r.stdout.strip() == "done":
        return True
    print(f"warning: msgstate mark done failed for {state_key}: "
          f"{(r.stdout or '').strip()} {(r.stderr or '').strip()[:200]} "
          f"-- heal-replied will reconcile from the ledgers",
          file=sys.stderr)
    return False


def _send_locked(channel, thread_ts, target_ts, message, dry_run,
                 event_id=None, state_key=None):
    # 1. replylog ledger (events/replies.jsonl, the hook flow's ledger).
    r = run([sys.executable, REPLYLOG, "check", "--target-ts", target_ts])
    if r.returncode != 0:
        print(f"replylog check failed: {r.stderr.strip()[:200]}", file=sys.stderr)
        return 2
    if r.stdout.strip().startswith("FOUND"):
        # The reply exists, so the message is handled: finalize the
        # state here too (this is the re-processing path that used to
        # depend on the worker remembering a separate mark step).
        _mark_done(state_key)
        print(f"already-replied (replylog): {target_ts}")
        return 0
    if dry_run:
        print(f"would-send to {channel} thread {thread_ts} target {target_ts}")
        return 0

    # 2. reply_once.py: its replied.jsonl record + live-thread
    # same-text check, the parroting guards, the send, and the
    # lost-response recovery -- all inside this lock. With
    # --event-id the send is metadata-stamped (post_metadata.py)
    # and the pre-send check looks for the stamp first.
    cmd = [sys.executable, REPLY_ONCE, "--channel", channel,
           "--thread-ts", thread_ts, "--target-ts", target_ts,
           "--message", message]
    if event_id:
        cmd += ["--event-id", event_id]
    r = run(cmd)
    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    if r.returncode == 0 and "already-replied" in out:
        _mark_done(state_key)
        print(f"already-replied (reply_once): {out}")
        return 0
    if r.returncode in (4, 5):
        # Duplicate-text / own-ledger guard refused; nothing sent.
        # No state finalization: exit 4 means another author posted
        # the same text (this message may still need a different
        # reply), and exit 5 only proves an identical earlier reply
        # exists somewhere in the thread, not that it answered this
        # target. The worker decides the state for these.
        print(f"not-sent ({out or err})")
        return 0
    if r.returncode != 0 or "sent reply_ts=" not in out:
        print(f"send failed (reply_once exit {r.returncode}): {out} {err}"[:500],
              file=sys.stderr)
        return r.returncode if r.returncode in (2, 3) else 2
    reply_ts = out.split("sent reply_ts=", 1)[1].split()[0].strip()

    # 3. Finalize the message state BEFORE the replylog record: the
    # reply has landed and replied.jsonl already proves it, so the
    # message is handled even if the record below fails. A record
    # failure still exits 2 (the caller must know a ledger is
    # incomplete), but it can no longer strand the state at pending.
    _mark_done(state_key)

    # 4. Record the same send in the replylog ledger, still locked.
    r = run([sys.executable, REPLYLOG, "record", "--channel", channel,
             "--target-ts", target_ts, "--sent-ts", reply_ts,
             "--thread-ts", thread_ts, "--text", message])
    if r.returncode != 0:
        print(f"sent reply_ts={reply_ts} BUT replylog record failed: "
              f"{r.stderr.strip()[:200]} -- do not repost blindly; "
              f"record this ts manually", file=sys.stderr)
        return 2
    print(f"sent reply_ts={reply_ts}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
