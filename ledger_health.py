#!/usr/bin/env python3
"""Broken-ledger-line sightings, for the daily summary.

Adopted 2026-11-22 from Yama's proposal in #slack-へようこそ
(thread 1795265680.893129): when a ledger loader skips a corrupt
line, the core problem is not "is it recorded" but "is it noticed".
stderr warnings land in the journal, which nobody watches, and a
dedicated log file would only add one more place nobody looks.
So instead of a dedicated file, sightings are collected here in
one small state file (events/broken_ledger.jsonl, one record per
JST date + ledger + line number) and surfaced where they are
noticed: the 21:00 daily summary reads them.

Usage (called by the three ledger loaders, never directly by hand):
  note_corrupt_line(ledger_path, lineno)   # record a first sighting
  today_summary(log_path=None)             # [{ledger, count, first_lineno}]

CLI (for the daily summary job):
  python3 ledger_health.py
  Prints one line per ledger with sightings today (JST), e.g.
    台帳に壊れた行あり（replies.jsonl・1件・最初の行番号 2）
  Prints nothing and exits 0 when there are none.

Safety: this module must never break the send path. All errors
inside note_corrupt_line are swallowed; callers keep the stderr
warning as the immediate trace, and a missing/broken state file
simply means "no sightings recorded". Duplicates are harmless:
today_summary counts distinct line numbers per ledger.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

BASE = os.environ.get("SLACK_LISTENER_BASE") or os.path.dirname(os.path.abspath(__file__))
DEFAULT_LOG = os.path.join(BASE, "events", "broken_ledger.jsonl")

JST = timezone(timedelta(hours=9))


def _today_jst():
    return datetime.now(JST).date().isoformat()


def _log_path_for(ledger_path):
    """State file next to the ledger (so tests with a tmp ledger
    never touch the real events/ directory)."""
    directory = os.path.dirname(os.path.abspath(ledger_path))
    return os.path.join(directory, "broken_ledger.jsonl")


def note_corrupt_line(ledger_path, lineno):
    """Record the first sighting of a corrupt line, once per
    (JST date, ledger, line number). Safe to call on every skip:
    repeat sightings of the same line are ignored. Never raises.
    """
    try:
        log_path = _log_path_for(ledger_path)
        date = _today_jst()
        ledger = os.path.basename(ledger_path)
        key = (date, ledger, int(lineno))
        seen = set()
        if os.path.exists(log_path):
            with open(log_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                        seen.add((r.get("date"), r.get("ledger"), r.get("lineno")))
                    except Exception:
                        continue
        if key in seen:
            return False
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        rec = {
            "date": date,
            "ledger": ledger,
            "lineno": int(lineno),
            "seen_at": datetime.now(timezone.utc).isoformat(),
        }
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return True
    except Exception:
        return False


def today_summary(log_path=None, date=None):
    """Sightings for one JST date (default today).

    Returns [{ledger, count, first_lineno}] sorted by ledger name,
    where count is the number of distinct corrupt line numbers seen
    in that ledger that day. Empty list when there are none.
    """
    path = log_path or DEFAULT_LOG
    date = date or _today_jst()
    by_ledger = {}
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("date") != date:
                continue
            ledger = r.get("ledger")
            lineno = r.get("lineno")
            if not ledger or not isinstance(lineno, int):
                continue
            by_ledger.setdefault(ledger, set()).add(lineno)
    return [
        {"ledger": ledger, "count": len(nos), "first_lineno": min(nos)}
        for ledger, nos in sorted(by_ledger.items())
    ]


def main():
    for s in today_summary():
        print(f"台帳に壊れた行あり（{s['ledger']}・{s['count']}件・"
              f"最初の行番号 {s['first_lineno']}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
