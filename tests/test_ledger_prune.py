#!/usr/bin/env python3
"""Tests for ledger pruning (replylog.prune_log, channel_send_once.prune_record).

Added 2026-11-12 from Yama's question in #slack-へようこそ about
rotating old ledger entries: neither Yanayana ledger pruned old
entries, so both now drop entries older than 90 days (default).
Dedupe checks only matter for re-executed runs within hours/days,
so 90 days is far beyond any re-run window.

Plain stdlib script, no pytest (same style as the other tests here).
Run: python3 tests/test_ledger_prune.py
Exit 0 when every check passes; each check prints PASS/FAIL.

All state lives in a temp dir, so the real events/ directory is
never touched.
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
REPLYLOG = os.path.join(ROOT, "replylog.py")
SPEC = importlib.util.spec_from_file_location(
    "channel_send_once", os.path.join(ROOT, "channel_send_once.py"))
cso = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cso)

CHECKS = [0, 0]  # [passed, failed]


def check(name, cond, detail=""):
    CHECKS[0 if cond else 1] += 1
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not cond else ""))
    return cond


NOW = datetime.now(timezone.utc)


def iso(days_ago):
    return (NOW - timedelta(days=days_ago)).isoformat()


def write_jsonl(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def read_jsonl(path):
    try:
        with open(path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        return []


def run_replylog(args, base):
    env = dict(os.environ, SLACK_LISTENER_BASE=base)
    return subprocess.run(
        [sys.executable, REPLYLOG] + args,
        capture_output=True, text=True, env=env, timeout=30)


def test_replylog_prune(tmp):
    """replies.jsonl: old entries dropped, recent + undated kept."""
    base = os.path.join(tmp, "replylog")
    log = os.path.join(base, "events", "replies.jsonl")
    write_jsonl(log, [
        {"recorded_at": iso(100), "target_ts": "old.1"},
        {"recorded_at": iso(10), "target_ts": "new.1"},
        {"target_ts": "nodate.1"},
    ])
    r = run_replylog(["prune"], base)
    check("replylog prune exit 0", r.returncode == 0, r.stderr)
    check("replylog prune reports pruned 1 kept 2",
          r.stdout.strip() == "pruned 1 kept 2", r.stdout.strip())
    kept = read_jsonl(log)
    check("replylog prune drops only the old entry",
          [e.get("target_ts") for e in kept] == ["new.1", "nodate.1"],
          str(kept))
    # Pruned entries go to the sibling archive file, not deleted outright
    # (adopted 2026-11-12 from Neiro's share in #slack-へようこそ).
    archived = read_jsonl(os.path.join(base, "events", "replies-archive.jsonl"))
    check("replylog prune archives the old entry",
          [e.get("target_ts") for e in archived] == ["old.1"], str(archived))
    # Pruning must not break the dedupe check for kept entries.
    r = run_replylog(["check", "--target-ts", "new.1"], base)
    check("replylog check still FOUND for kept entry", r.stdout.startswith("FOUND"), r.stdout)
    r = run_replylog(["check", "--target-ts", "old.1"], base)
    check("replylog check NOT_FOUND for pruned entry", r.stdout.strip() == "NOT_FOUND", r.stdout)


def test_replylog_prune_dry_run(tmp):
    """--dry-run reports counts but writes nothing (Neiro's share)."""
    base = os.path.join(tmp, "replylog-dry")
    log = os.path.join(base, "events", "replies.jsonl")
    write_jsonl(log, [
        {"recorded_at": iso(100), "target_ts": "old.1"},
        {"recorded_at": iso(10), "target_ts": "new.1"},
    ])
    r = run_replylog(["prune", "--dry-run"], base)
    check("replylog prune --dry-run exit 0", r.returncode == 0, r.stderr)
    check("replylog prune --dry-run reports dry-run pruned 1 kept 1",
          r.stdout.strip() == "dry-run pruned 1 kept 1", r.stdout.strip())
    check("replylog prune --dry-run leaves the ledger untouched",
          [e.get("target_ts") for e in read_jsonl(log)] == ["old.1", "new.1"],
          str(read_jsonl(log)))
    check("replylog prune --dry-run creates no archive",
          not os.path.exists(os.path.join(base, "events", "replies-archive.jsonl")))
    # A real prune after the dry run still drops the old entry.
    r = run_replylog(["prune"], base)
    check("replylog prune after dry-run still works",
          r.stdout.strip() == "pruned 1 kept 1", r.stdout.strip())


def test_replylog_prune_missing_file(tmp):
    base = os.path.join(tmp, "replylog-missing")
    r = run_replylog(["prune"], base)
    check("replylog prune missing file exit 0", r.returncode == 0, r.stderr)
    check("replylog prune missing file reports 0/0",
          r.stdout.strip() == "pruned 0 kept 0", r.stdout.strip())


def test_channel_prune(tmp):
    """channel_posts.jsonl: old entries dropped, recent + undated kept."""
    sub = os.path.join(tmp, "channel")
    os.makedirs(sub, exist_ok=True)
    path = os.path.join(sub, "channel_posts.jsonl")
    write_jsonl(path, [
        {"at": iso(200), "text": "old post"},
        {"at": iso(5), "text": "new post"},
        {"text": "nodate post"},
    ])
    pruned, kept = cso.prune_record(path=path)
    check("channel prune returns (1, 2)", (pruned, kept) == (1, 2), f"({pruned}, {kept})")
    rows = read_jsonl(path)
    check("channel prune drops only the old entry",
          [e.get("text") for e in rows] == ["new post", "nodate post"], str(rows))
    archived = read_jsonl(os.path.join(sub, "channel_posts-archive.jsonl"))
    check("channel prune archives the old entry",
          [e.get("text") for e in archived] == ["old post"], str(archived))
    # in_record must still see the kept entry and miss the pruned one.
    cso.POSTS = path
    check("channel in_record still hits kept entry", cso.in_record("new post"))
    check("channel in_record misses pruned entry", not cso.in_record("old post"))


def test_channel_prune_dry_run(tmp):
    """--dry-run reports counts but writes nothing (Neiro's share)."""
    sub = os.path.join(tmp, "channel-dry")
    os.makedirs(sub, exist_ok=True)
    path = os.path.join(sub, "channel_posts.jsonl")
    write_jsonl(path, [
        {"at": iso(200), "text": "old post"},
        {"at": iso(5), "text": "new post"},
    ])
    pruned, kept = cso.prune_record(path=path, dry_run=True)
    check("channel prune dry_run returns (1, 1)", (pruned, kept) == (1, 1), f"({pruned}, {kept})")
    check("channel prune dry_run leaves the ledger untouched",
          [e.get("text") for e in read_jsonl(path)] == ["old post", "new post"],
          str(read_jsonl(path)))
    check("channel prune dry_run creates no archive",
          not os.path.exists(os.path.join(sub, "channel_posts-archive.jsonl")))
    # CLI form: --prune --dry-run must not write either.
    cso.POSTS = path
    rc = cso.main(["--prune", "--dry-run"])
    check("channel --prune --dry-run CLI exit 0", rc == 0, f"rc={rc}")
    check("channel --prune --dry-run CLI leaves ledger untouched",
          len(read_jsonl(path)) == 2)


def test_channel_prune_cli_and_missing(tmp):
    sub = os.path.join(tmp, "channel-cli")
    os.makedirs(sub, exist_ok=True)
    cso.POSTS = os.path.join(sub, "channel_posts.jsonl")
    rc = cso.main(["--prune"])
    check("channel --prune missing file exit 0", rc == 0, f"rc={rc}")
    write_jsonl(cso.POSTS, [{"at": iso(100), "text": "old"}])
    rc = cso.main(["--prune", "--older-than-days", "90"])
    check("channel --prune CLI exit 0", rc == 0, f"rc={rc}")
    check("channel --prune CLI drops old entry", read_jsonl(cso.POSTS) == [])
    check("channel --prune CLI archives old entry",
          read_jsonl(os.path.join(sub, "channel_posts-archive.jsonl")) == [{"at": iso(100), "text": "old"}],
          str(read_jsonl(os.path.join(sub, "channel_posts-archive.jsonl"))))
    # Pruning again with nothing old must not create/duplicate the archive.
    rc = cso.main(["--prune", "--older-than-days", "90"])
    check("channel --prune no-op exit 0", rc == 0, f"rc={rc}")
    check("channel --prune no-op leaves archive alone",
          len(read_jsonl(os.path.join(sub, "channel_posts-archive.jsonl"))) == 1)


def main():
    with tempfile.TemporaryDirectory(prefix="ledger-prune-") as tmp:
        test_replylog_prune(tmp)
        test_replylog_prune_dry_run(tmp)
        test_replylog_prune_missing_file(tmp)
        test_channel_prune(tmp)
        test_channel_prune_dry_run(tmp)
        test_channel_prune_cli_and_missing(tmp)
    print(f"\n{CHECKS[0]} passed, {CHECKS[1]} failed")
    return 1 if CHECKS[1] else 0


if __name__ == "__main__":
    sys.exit(main())
