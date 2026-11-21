#!/usr/bin/env python3
"""Tests for seen_state.py: the seen-event-ids file with .bak fallback.

Yama's 2026-11-18 question (thread 1795006461.907139) pointed out the
gap: seen_event_ids.json was a single file with no backup, while
auto_join_state.json already had the .bak pattern. These checks cover
the load fallback, the write-back, and the guards that keep a corrupt
or degenerated main file from clobbering a good backup.

Plain stdlib script, no pytest (same style as the other suites).
Run:
  python3 tests/test_seen_state.py
Exit 0 when every check passes; each check prints PASS/FAIL.
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import backfill
import seen_state

CHECKS = [0, 0]  # [passed, failed]


def check(name, cond):
    if cond:
        CHECKS[0] += 1
        print(f"PASS {name}")
    else:
        CHECKS[1] += 1
        print(f"FAIL {name}")


def write(path, content):
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


MAIN = ["EvAAA", "ts:C1:1795000000.1", "EvBBB"]
OTHER = ["EvXXX", "EvYYY"]


def fresh(tmp):
    return os.path.join(tmp, "seen_event_ids.json")


with tempfile.TemporaryDirectory() as tmp:
    # 1. Good main file loads as-is; no .bak is created by a load.
    p = fresh(tmp)
    write(p, json.dumps(MAIN))
    check("load good main", seen_state.load_seen(p) == MAIN)
    check("load does not create .bak", not os.path.exists(p + ".bak"))

    # 2. Corrupt main falls back to .bak and writes it back to main.
    p = os.path.join(tmp, "seen2.json")
    write(p, "{not json")
    write(p + ".bak", json.dumps(OTHER))
    check("load falls back to .bak on corrupt main",
          seen_state.load_seen(p) == OTHER)
    check("fallback writes back to main", read_json(p) == OTHER)

    # 3. Missing main falls back to .bak as well.
    p = os.path.join(tmp, "seen3.json")
    write(p + ".bak", json.dumps(OTHER))
    check("load falls back to .bak on missing main",
          seen_state.load_seen(p) == OTHER)
    check("missing-main fallback writes back", read_json(p) == OTHER)

    # 4. Neither file exists: empty list, no crash.
    p = os.path.join(tmp, "seen4.json")
    check("load with no files returns []", seen_state.load_seen(p) == [])

    # 5. Both files unusable: empty list, no crash.
    p = os.path.join(tmp, "seen5.json")
    write(p, "{broken")
    write(p + ".bak", "[also broken")
    check("load with both unusable returns []",
          seen_state.load_seen(p) == [])

    # 6. A JSON value that is not a list is unusable, not a seen set.
    p = os.path.join(tmp, "seen6.json")
    write(p, json.dumps({"not": "a list"}))
    write(p + ".bak", json.dumps(OTHER))
    check("non-list main falls back to .bak",
          seen_state.load_seen(p) == OTHER)

    # 7. Save keeps the previous good main as .bak and writes the new one.
    p = os.path.join(tmp, "seen7.json")
    write(p, json.dumps(MAIN))
    seen_state.save_seen(OTHER, p)
    check("save writes new main", read_json(p) == OTHER)
    check("save keeps previous good main as .bak",
          read_json(p + ".bak") == MAIN)
    check("save leaves no .tmp behind", not os.path.exists(p + ".tmp"))

    # 8. A corrupt main must never overwrite a good .bak on save.
    p = os.path.join(tmp, "seen8.json")
    write(p, "{broken")
    write(p + ".bak", json.dumps(OTHER))
    seen_state.save_seen(MAIN, p)
    check("corrupt main does not clobber good .bak",
          read_json(p + ".bak") == OTHER)
    check("save still writes new main after corrupt old one",
          read_json(p) == MAIN)

    # 9. A valid-but-empty main must not clobber a non-empty .bak.
    p = os.path.join(tmp, "seen9.json")
    write(p, "[]")
    write(p + ".bak", json.dumps(OTHER))
    seen_state.save_seen([], p)
    check("empty main does not clobber non-empty .bak",
          read_json(p + ".bak") == OTHER)

    # 10. First save (no main yet) creates the main file, no .bak needed.
    p = os.path.join(tmp, "seen10.json")
    seen_state.save_seen(MAIN, p)
    check("first save writes main", read_json(p) == MAIN)
    check("first save creates no .bak", not os.path.exists(p + ".bak"))

    # 11. backfill.py wrappers keep the SEEN_MAX trim on save.
    old_max = backfill.SEEN_MAX
    backfill.SEEN_MAX = 3
    old_path = backfill.SEEN_PATH
    p = os.path.join(tmp, "seen11.json")
    backfill.SEEN_PATH = p
    try:
        backfill.save_seen(["a", "b", "c", "d", "e"])
        check("backfill save_seen trims to SEEN_MAX",
              read_json(p) == ["c", "d", "e"])
        check("backfill load_seen reads back the trimmed list",
              backfill.load_seen() == ["c", "d", "e"])
    finally:
        backfill.SEEN_MAX = old_max
        backfill.SEEN_PATH = old_path

    # 12. union_seen (2026-11-21 burst fix): disk order first, then
    # own-only keys; nothing on either side is ever dropped.
    check("union_seen keeps disk order and appends own-only keys",
          seen_state.union_seen(["a", "b"], ["b", "c"]) == ["a", "b", "c"])
    check("union_seen with empty disk is own list",
          seen_state.union_seen([], ["x", "y"]) == ["x", "y"])
    check("union_seen with empty own is disk list",
          seen_state.union_seen(["x"], []) == ["x"])
    check("union_seen dedupes within own",
          seen_state.union_seen(["a"], ["b", "b", "a"]) == ["a", "b"])

print(f"{CHECKS[0]} passed, {CHECKS[1]} failed")
sys.exit(1 if CHECKS[1] else 0)
