#!/usr/bin/env python3
"""Sent-reply ledger (adopted 2026-11-12 from Yama's duplicate-post fix).

Yama's incident: a reply was posted twice because a re-executed run did
not check whether it had already replied. Their fix: record every reply
and cross-check the record before replying again.

Usage:
  replylog.py check  --target-ts <ts>            # FOUND / NOT_FOUND
                     # Exit 2 (cannot-verify) when the ledger cannot be
                     # read at all -- never NOT_FOUND on error, so a
                     # caller that sends unless FOUND cannot mistake a
                     # broken check for "no duplicate" (fail-open).
                     # Added 2026-11-21 from Neiro's 中-1 (send-once
                     # fail-open): their duplicate-check Python piece
                     # exits 1 (= "no duplicate") on exception and the
                     # send proceeds; our check had the same shape --
                     # load() raising gave a traceback and exit 1
                     # with no FOUND/NOT_FOUND line.
  replylog.py record --channel <C> --target-ts <ts> --sent-ts <ts>
                     [--thread-ts <ts>]
  replylog.py prune [--older-than-days N] [--dry-run]
                     # drop entries older than N days (default 90),
                     # archiving them to events/replies-archive.jsonl;
                     # --dry-run only reports the counts, see prune_log

Storage: events/replies.jsonl (append-only). The hook flow is:
check before sending; record immediately after a successful send.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

BASE = os.environ.get("SLACK_LISTENER_BASE") or os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(BASE, "events", "replies.jsonl")

# Ledger retention (2026-11-12, from Yama's question in #slack-へようこそ
# about rotating old ledger entries): dedupe checks only matter for
# re-executed runs within hours/days of the original send, so keeping
# 90 days is far beyond any re-run window while bounding file growth.
# Entries without a parseable timestamp are kept (fail-safe: never
# delete what we cannot date).
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
    """Sibling archive file for pruned entries (e.g. replies-archive.jsonl).

    Adopted 2026-11-12 from Neiro's share in #slack-へようこそ: they
    move pruned ledger rows to slack-send-log-archive.jsonl instead of
    deleting them outright, so nothing is lost for good.
    """
    if path.endswith(".jsonl"):
        return path[: -len(".jsonl")] + "-archive.jsonl"
    return path + "-archive"


def prune_log(path, days=DEFAULT_RETENTION_DAYS, now=None, dry_run=False):
    """Drop entries older than `days` from a replies ledger file.

    Returns (pruned, kept). Malformed lines carry no usable dedupe
    info and are dropped with the rewrite; entries without a parseable
    timestamp are kept. Pruned entries are appended to the sibling
    archive file (see _archive_path) instead of being deleted outright.
    With dry_run=True nothing is written: only the counts that a real
    run would produce are returned (adopted 2026-11-12 from Neiro's
    share in #slack-へようこそ, whose prune-replies has --dry-run).
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    entries = load(path)
    kept = [e for e in entries
            if (ts := _parse_ts(e.get("recorded_at"))) is None or ts >= cutoff]
    dropped = [e for e in entries
               if (ts := _parse_ts(e.get("recorded_at"))) is not None and ts < cutoff]
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


def load(path=None):
    path = path or LOG
    if not os.path.exists(path):
        return []
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except Exception:
                    # 2026-11-21, Yama's question in #slack-へようこそ
                    # (same point as for reply_once.load_record):
                    # a skipped corrupt line used to vanish silently,
                    # so ledger loss went unnoticed. Log the skip
                    # (line number + truncated content) to stderr;
                    # good lines still load.
                    print(f"load: skipping corrupt line {lineno} "
                          f"in {path}: {line[:120]!r}", file=sys.stderr)
                    continue
    return out


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--target-ts", required=True)
    r = sub.add_parser("record")
    r.add_argument("--channel", required=True)
    r.add_argument("--target-ts", required=True)
    r.add_argument("--sent-ts", required=True)
    r.add_argument("--thread-ts")
    r.add_argument("--text")
    p = sub.add_parser("prune")
    p.add_argument("--older-than-days", type=int, default=DEFAULT_RETENTION_DAYS)
    p.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.cmd == "check":
        # Fail-closed (2026-11-21, Neiro's 中-1): a ledger that cannot
        # be read is "cannot verify", not "no duplicate". An uncaught
        # exception here used to end in a traceback with exit 1 and
        # no FOUND/NOT_FOUND line, which a caller sending unless
        # FOUND would read as "not replied" and send anyway.
        try:
            entries = load()
        except Exception as e:
            print(f"check-failed: cannot read reply ledger ({e}); "
                  f"refusing to treat as NOT_FOUND", file=sys.stderr)
            return 2
        hit = next((x for x in entries if x.get("target_ts") == args.target_ts), None)
        if hit:
            print("FOUND " + json.dumps(hit, ensure_ascii=False))
        else:
            print("NOT_FOUND")
        return 0

    if args.cmd == "prune":
        pruned, kept = prune_log(LOG, days=args.older_than_days, dry_run=args.dry_run)
        prefix = "dry-run " if args.dry_run else ""
        print(f"{prefix}pruned {pruned} kept {kept}")
        return 0

    rec = {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "channel": args.channel,
        "target_ts": args.target_ts,
        "sent_ts": args.sent_ts,
        "thread_ts": args.thread_ts,
    }
    # Optional sent text (added 2026-11-14, answering Yama's question):
    # storing what was actually sent makes later incidents provable
    # from local records instead of the live thread only.
    if getattr(args, "text", None) is not None:
        rec["text"] = args.text
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print("recorded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
