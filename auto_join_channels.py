#!/usr/bin/env python3
"""Auto-join new public channels as the Yanayana bot.

Requested by あた on 2026-11-18: when a new channel is created in the
muse取締役会 workspace, the bot should join it without あた having to
ask each time (the manual join of #yamaguchi勉強応援 that day was the
trigger case).

Shape:
  * Polls `slackcli conversations list` (the same read the manual
    check used) and joins every conversation that is a public,
    non-archived channel the bot is not a member of yet.
  * Private channels are never self-joined: Slack does not list
    private channels the bot is not in, and joining one needs an
    invitation by design -- that path stays manual.
  * Archived channels are skipped (e.g. #ソーシャル), as are DMs /
    group DMs (ids that do not start with "C", or is_channel false).
  * After a successful join, one greeting parent post is sent via
    channel_send_once.py, so a re-run can never duplicate it.
  * After a successful join, backfill.py --channel <id>
    --window-minutes 15 is run (best effort) so messages posted
    between the channel's creation and the join -- up to one 5-minute
    watchdog interval -- are not missed: the Socket Mode listener
    only sees messages posted after the bot joins. The 15-minute
    window (adopted 2026-11-18 from Neiro's fix
    neiro-slack-system 96f8597) replaces the global newest-seen floor
    for this run: that floor is set by whatever channel was busiest,
    so it could sit above the new channel's gap posts and skip them.
    Unlike Neiro's v1 seed, our backfill also follows thread replies.
    A backfill failure never fails the join.
  * A channel the bot was removed from is never auto-rejoined:
    membership is remembered in events/auto_join_state.json, and a
    channel that was a member before but is not one now (while still
    listed and not archived) is recorded as removed and skipped from
    then on. A manual re-invite clears the record, so joining again
    by hand re-enables auto-join. Adopted 2026-11-18 from Yama's
    auto-join share (slack-yama 32c07f2a): without this, the 1-minute
    timer would rejoin a channel within a minute of being removed
    from it.
  * Baseline self-heal (adopted 2026-11-18 from Neiro's auto-join.py,
    neiro-slack-system): a channel the bot is already a member of but
    which has no inbox baseline yet -- joined by hand, or a join
    whose backfill failed -- gets the same 15-minute backfill, so the
    gap is closed retroactively instead of left open. Channels with
    a baseline are untouched, so this costs one backfill run at most
    until the first message lands.
  * Every join is appended to events/auto_join.jsonl. Failures are
    reported on stderr and in the exit code, but one failing channel
    does not stop the others.
  * Runs from slack-auto-join.timer every 1 minute.

slackcli stores its profile under $HOME/.config/slackcli. The
watchdog runs as root (HOME=/root), where no profile exists, so the
subprocess environment pins HOME to the directory that actually
holds the profile when the current HOME does not.
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
SLACKCLI = os.path.join(BASE, "bin", "slackcli")
EVENTS = os.path.join(BASE, "events")
LOG_PATH = os.path.join(EVENTS, "auto_join.jsonl")
STATE_PATH = os.path.join(EVENTS, "auto_join_state.json")
INBOX_PATH = os.path.join(EVENTS, "inbox.jsonl")
WORKSPACE = "muse-board"
GREETING = ("Yanayanaです。新しいチャンネルに参加しました！"
            "よろしくお願いします。投稿があれば返信やお手伝いをしますね。")


def slackcli_home():
    """HOME that holds the slackcli profile, for subprocess env."""
    current = os.environ.get("HOME") or ""
    if os.path.exists(os.path.join(current, ".config", "slackcli")):
        return current
    fallback = "/path/to"
    if os.path.exists(os.path.join(fallback, ".config", "slackcli")):
        return fallback
    return current


def subprocess_env():
    env = dict(os.environ)
    env["HOME"] = slackcli_home()
    return env


def load_state(path=STATE_PATH):
    """Membership memory: {"member": [...], "removed": [...]}.

    Missing or corrupt state means "no memory yet", never a crash:
    the first run after this lands simply starts recording.
    """
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {"member": [], "removed": []}
    if not isinstance(data, dict):
        return {"member": [], "removed": []}
    member = data.get("member") or []
    removed = data.get("removed") or []
    return {"member": sorted({str(c) for c in member}),
            "removed": sorted({str(c) for c in removed})}


def save_state(state, path=STATE_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    os.replace(tmp, path)


def update_removed(conversations, state):
    """Refresh the removed-channel record from the current listing.

    A channel the bot was a member of before but is not now -- while
    still listed and not archived -- was left or removed, so it joins
    the removed set and is never auto-rejoined. A channel that
    vanished from the listing entirely (e.g. turned private) is not
    recorded: it cannot be auto-joined anyway. A channel the bot is
    a member of again leaves the removed set (manual re-invite).
    Returns the updated state dict.
    """
    listed = {}
    current_member = set()
    for conv in conversations or []:
        cid = conv.get("id") or ""
        if not cid.startswith("C"):
            continue
        listed[cid] = conv
        if conv.get("is_member"):
            current_member.add(cid)
    previous_member = set(state.get("member") or [])
    removed = set(state.get("removed") or [])
    for cid in previous_member - current_member:
        conv = listed.get(cid)
        if conv is not None and not conv.get("is_archived"):
            removed.add(cid)
    removed -= current_member
    return {"member": sorted(current_member),
            "removed": sorted(removed)}


def joinable(conversations, removed=frozenset()):
    """Conversations the bot should self-join, from a list payload.

    Pure filter, kept separate so tests can pin the exact rules:
    channel id (C...), public, not archived, not already a member,
    and never a channel recorded as removed.
    """
    removed = set(removed or [])
    out = []
    for conv in conversations or []:
        cid = conv.get("id") or ""
        if not cid.startswith("C"):
            continue
        if conv.get("is_private"):
            continue
        if conv.get("is_archived"):
            continue
        if conv.get("is_member"):
            continue
        if cid in removed:
            continue
        out.append(conv)
    return out


def channels_with_baseline(inbox_path=INBOX_PATH):
    """Channel ids that already have at least one inbox line.

    A missing or unreadable inbox means "no baseline anywhere";
    malformed lines are skipped, never a crash.
    """
    baselines = set()
    try:
        with open(inbox_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                cid = rec.get("channel") or ""
                if cid:
                    baselines.add(cid)
    except OSError:
        return set()
    return baselines


def needs_baseline_seed(conversations, baselines):
    """Member channels with no inbox baseline yet (pure filter).

    These are channels the bot already belongs to -- joined by hand,
    or a join whose backfill failed -- where the listener has never
    recorded a message, so the join gap was never closed.
    """
    baselines = set(baselines or [])
    out = []
    for conv in conversations or []:
        cid = conv.get("id") or ""
        if not cid.startswith("C"):
            continue
        if conv.get("is_private"):
            continue
        if conv.get("is_archived"):
            continue
        if not conv.get("is_member"):
            continue
        if cid in baselines:
            continue
        out.append(conv)
    return out


def list_conversations(runner=subprocess.run):
    proc = runner(
        [SLACKCLI, "conversations", "list", "--workspace", WORKSPACE,
         "--json", "--limit", "200"],
        capture_output=True, text=True, timeout=60, env=subprocess_env())
    if proc.returncode != 0:
        raise RuntimeError(
            f"conversations list failed rc={proc.returncode}: "
            f"{(proc.stderr or '')[:300]}")
    data = json.loads(proc.stdout)
    return data.get("conversations") or []


def join_channel(channel_id, runner=subprocess.run):
    proc = runner(
        [SLACKCLI, "conversations", "join", channel_id,
         "--workspace", WORKSPACE, "--json"],
        capture_output=True, text=True, timeout=60, env=subprocess_env())
    if proc.returncode != 0:
        raise RuntimeError(
            f"join {channel_id} failed rc={proc.returncode}: "
            f"{(proc.stderr or '')[:300]}")
    data = json.loads(proc.stdout)
    channel = data.get("channel") or {}
    if not channel.get("is_member", True):
        raise RuntimeError(f"join {channel_id}: is_member still false")
    return channel


def greet(channel_id, runner=subprocess.run):
    """One greeting parent post via channel_send_once.py (deduped)."""
    proc = runner(
        [sys.executable, os.path.join(BASE, "channel_send_once.py"),
         "--channel", channel_id, "--message", GREETING],
        capture_output=True, text=True, timeout=120, env=subprocess_env(),
        cwd=BASE)
    return proc.returncode == 0


def backfill_channel(channel_id, runner=subprocess.run):
    """Recover gap messages for a just-joined channel (best effort).

    Returns the parsed backfill summary dict, or None when the run
    failed -- callers must not treat a backfill failure as a join
    failure. backfill.py admits messages newer than the 15-minute
    window floor (--window-minutes 15, see backfill.py: the global
    newest-seen floor could sit above the new channel's gap posts),
    and skips already-seen, own, and join/leave messages, so
    re-running is safe.
    """
    try:
        proc = runner(
            [sys.executable, os.path.join(BASE, "backfill.py"),
             "--window-minutes", "15", "--channel", channel_id],
            capture_output=True, text=True, timeout=120,
            env=subprocess_env(), cwd=BASE)
    except Exception as e:
        print(f"auto_join: backfill {channel_id} failed: {e}",
              file=sys.stderr)
        return None
    if proc.returncode != 0:
        print(f"auto_join: backfill {channel_id} rc={proc.returncode}: "
              f"{(proc.stderr or '')[:200]}", file=sys.stderr)
        return None
    try:
        return json.loads(proc.stdout[proc.stdout.find("{"):])
    except Exception:
        return {"recovered": None, "checked": None, "errors": ["unparsable"]}


def record(entry):
    os.makedirs(EVENTS, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    dry_run = "--dry-run" in argv
    no_greet = "--no-greet" in argv
    try:
        conversations = list_conversations()
    except Exception as e:
        print(f"auto_join: {e}", file=sys.stderr)
        return 2
    state = load_state()
    new_state = update_removed(conversations, state)
    newly_removed = (set(new_state["removed"])
                     - set(state.get("removed") or []))
    for cid in sorted(newly_removed):
        print(f"auto_join: removed from {cid}, will not auto-rejoin",
              file=sys.stderr)
    if not dry_run:
        try:
            save_state(new_state)
        except Exception as e:
            print(f"auto_join: state save failed: {e}", file=sys.stderr)
    targets = joinable(conversations, removed=new_state["removed"])
    # Baseline snapshot is taken before any join, so a channel joined
    # in this run is never also "healed": the listing says it is not
    # a member yet, so needs_baseline_seed() excludes it anyway.
    heal_targets = needs_baseline_seed(conversations,
                                       channels_with_baseline())
    joined = 0
    failed = 0
    for conv in targets:
        cid = conv["id"]
        name = conv.get("name") or ""
        if dry_run:
            print(f"auto_join: would join {cid} ({name})")
            joined += 1
            continue
        try:
            join_channel(cid)
        except Exception as e:
            print(f"auto_join: {e}", file=sys.stderr)
            failed += 1
            continue
        greeted = False
        if not no_greet:
            greeted = greet(cid)
        backfill = backfill_channel(cid)
        record({"at": datetime.now(timezone.utc).isoformat(),
                "channel": cid, "name": name, "greeted": greeted,
                "backfill": backfill})
        print(f"auto_join: joined {cid} ({name}) greeted={greeted} "
              f"backfill={backfill}")
        joined += 1
    # Baseline self-heal: member channels with no inbox baseline get
    # the same 15-minute backfill (best effort, never fails the run).
    # No greeting here: the bot is already a member.
    for conv in heal_targets:
        cid = conv["id"]
        name = conv.get("name") or ""
        if dry_run:
            print(f"auto_join: would seed baseline for {cid} ({name})")
            continue
        backfill = backfill_channel(cid)
        record({"at": datetime.now(timezone.utc).isoformat(),
                "channel": cid, "name": name, "baseline_seed": True,
                "backfill": backfill})
        print(f"auto_join: seeded baseline for {cid} ({name}) "
              f"backfill={backfill}")
    if failed:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
