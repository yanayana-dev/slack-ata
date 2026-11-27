#!/usr/bin/env bash
# Watchdog for slack-listener (modeled on Neiro's setup):
# - reinstalls the systemd unit if it went missing or is stale
#   (hash comparison, adopted 2026-11-12 from the Yama/Neiro thread:
#   "missing or hash differs" also catches a stale file left behind,
#   not just a deleted one)
# - restarts the service if it is not active
# - restarts it if the heartbeat is stale (>120s) or not connected
#   (revised 2026-11-19 from Yama's share in thread
#   1795006461.907139: re-read the heartbeat 5s later before
#   restarting, and do not restart the same heartbeat again within
#   60s of a successful restart)
#
# Reinstall hardening, adopted 2026-11-12 from Yama's self-review
# implementation share in #slack-へようこそ (their watchdog.sh):
# - the copy source's existence is checked before attempting a
#   reinstall (a missing source is logged and skipped, not retried);
# - after a reinstall, `systemctl cat` must show the unit, otherwise
#   the run stops without restarting or looping (alert via the log);
# - reinstall attempts are capped (3) with exponential backoff
#   (30s base) in a state file, so a broken copy is not reinstalled
#   forever; the counter resets once the unit is healthy again.
# One deliberate difference from Yama's: we check the file (cmp -s)
# first and only reinstall on missing/stale, so no failed `restart`
# is spent to discover "Unit not found" (Neiro's point), which also
# means failure-type branching on restart is not needed here.
# The watchdog runs as a system unit (root), so no sudo is needed
# and no sudoers snippet is required on this side.
set -u
BASE=${WATCHDOG_BASE:-/path/to/slack-watcher}
DST_DIR=${WATCHDOG_DST_DIR:-/etc/systemd/system}
UNIT=slack-listener.service
SRC=$BASE/slack-listener.service
DST=$DST_DIR/slack-listener.service
HB=$BASE/events/heartbeat.json
STATE_FILE=${WATCHDOG_STATE_FILE:-$BASE/events/watchdog_reinstall.state}
# Shared restart-cooldown stamp, used by both the post-reinstall
# restart below and the heartbeat-stall restart further down: any
# watchdog run (timer, hook self-heal, recovery cron) that restarts
# the listener stamps it, so an overlapping run does not restart
# the just-started listener again while its first heartbeat (the
# listener writes one every 15s) is still pending.
COOLDOWN_FILE=${WATCHDOG_RESTART_COOLDOWN_FILE:-$BASE/events/watchdog_restart.cooldown}
MAX_ATTEMPTS=3
BACKOFF_BASE=30

# Hang + overlap guard (adopted 2026-11-27 from Neiro's share,
# commit 11d9215: timeout 240 + flock -n): the timer path and the
# hook/cron direct calls can overlap, and a hung run must not block
# later cycles forever. Re-exec once under `timeout 240` (the timer
# path additionally has TimeoutStartSec=300 on the oneshot service),
# then take a non-blocking exclusive lock; a second run exits quietly.
if [ -z "${WATCHDOG_TIMEOUT_WRAPPED:-}" ]; then
  # Resolve $0: `timeout` does a PATH lookup, so a relative $0
  # (e.g. `bash watchdog.sh`) would fail with 127 on the re-exec.
  # Run through `bash`: the script has no +x bit (callers invoke it
  # as `bash watchdog.sh`, same as the timer and the hooks do).
  SCRIPT_PATH="$(readlink -f -- "$0" 2>/dev/null || printf '%s' "$0")"
  WATCHDOG_TIMEOUT_WRAPPED=1 exec timeout 240 bash "$SCRIPT_PATH" "$@"
fi
LOCK_FILE=${WATCHDOG_LOCK_FILE:-$BASE/events/watchdog.lock}
mkdir -p "$BASE/events" 2>/dev/null
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "watchdog: another run is active; exiting" >&2
  exit 0
fi

# Reader-side guard (adopted 2026-11-19 from Yama's share in thread
# 1795064369.469599): before restarting the listener, check that
# listener.py still compiles. Python files are edited directly (not
# atomically), so a half-written file must not kill the running
# process -- the restart is skipped for this cycle and the next
# cycle retries once the edit is complete.
listener_py_ok() {
  [ -f "$BASE/listener.py" ] && python3 -m py_compile "$BASE/listener.py" 2>/dev/null
}

# Detection-time identity record (adopted 2026-11-25 from Neiro's
# proposal in thread 1795611852.507439, observation item (5)):
# whether the hosts/VMs are the same cannot be checked after a
# stop, so when a stale heartbeat is confirmed, record hostname
# and uptime on the spot. uptime's boot time is the useful half
# here -- our hostname is the generic runtime name -- since a
# reset uptime proves a reboot happened between detections.
# Refined 2026-11-25 from Yama's follow-up in the same thread:
# hostname can collide across VMs and uptime drifts with record
# time, so also record the boot_id hash -- first 8 chars of
# sha256sum of /proc/sys/kernel/random/boot_id (file bytes as-is,
# trailing newline included) -- and the boot time (uptime -s).
# Same kernel (same VM) => same hash; different VM => different.
# One JSON line per detection in events/detection_identity.log;
# best-effort only, never blocks the restart.
DETECTION_LOG=${WATCHDOG_DETECTION_LOG:-$BASE/events/detection_identity.log}
record_detection_identity() { # $1 = heartbeat age in seconds
  local boot_id_hash boot_time
  boot_id_hash=$(sha256sum /proc/sys/kernel/random/boot_id 2>/dev/null | cut -c1-8)
  [ -n "$boot_id_hash" ] || boot_id_hash=unknown
  boot_time=$(uptime -s 2>/dev/null || echo unknown)
  {
    printf '{"at": "%s", "event": "heartbeat_stale", "heartbeat_age_s": %s, "hostname": "%s", "uptime": "%s", "boot_id_hash": "%s", "boot_time": "%s"}\n' \
      "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" \
      "$(hostname 2>/dev/null || echo unknown)" \
      "$(uptime 2>/dev/null | sed 's/"/'"'"'/g' || echo unknown)" \
      "$boot_id_hash" "$boot_time"
  } >> "$DETECTION_LOG" 2>/dev/null || true
}

# Hook health check (adopted 2026-11-15 from Neiro's check-script share):
# syntax of ~/hooks/scripts/*.sh and JSON of ~/hooks/definitions/*.json;
# warns to Slack on new/changed failures. Never blocks the watchdog.
"$BASE/check_hooks.sh" >/dev/null 2>&1 || true

# Listener sync check moved out of this watchdog on 2026-11-18
# night: it compares live listener files with the (scrubbed) public
# copy, and the watchdog itself is one of the compared files, so the
# watchdog calling it left a self-reference (the drifted file would
# be running its own check). It now runs on its own
# slack-sync-check.timer (5 min); this watchdog only restores and
# starts those units below, like the auto-join units.

# Auto-join of new public channels moved out of this watchdog on
# 2026-11-18 (あた's choice: keep the heavy checks at 5 minutes, run
# only the light auto-join every minute): it now runs on its own
# slack-auto-join.timer; this watchdog only restores and starts
# those units below, like the DM poll units.

# --- reinstall attempt state: lines "name count next_allowed_epoch" ---
state_get() { # name -> "count next_allowed" (defaults 0 0)
  if [ -f "$STATE_FILE" ]; then
    awk -v n="$1" '$1==n {print $2, $3; found=1} END {if (!found) print "0 0"}' "$STATE_FILE"
  else
    echo "0 0"
  fi
}

state_set() { # name count next_allowed
  local tmp="$STATE_FILE.tmp.$$"
  mkdir -p "$(dirname "$STATE_FILE")"
  { if [ -f "$STATE_FILE" ]; then grep -v "^$1 " "$STATE_FILE" || true; fi
    echo "$1 $2 $3"; } > "$tmp" && mv "$tmp" "$STATE_FILE"
}

state_reset() { # name
  if [ -f "$STATE_FILE" ]; then
    local tmp="$STATE_FILE.tmp.$$"
    grep -v "^$1 " "$STATE_FILE" > "$tmp" || true
    mv "$tmp" "$STATE_FILE"
  fi
}

# reinstall_unit <src> <dst> <unit-name> [extra-enable-target]
# Returns 0 when the unit was reinstalled and verified visible.
reinstall_unit() {
  local src="$1" dst="$2" unit="$3" enable_target="${4:-$3}"
  if [ ! -f "$src" ]; then
    echo "watchdog: copy source $src missing; skipping reinstall of $unit" >&2
    return 1
  fi
  local count next_allowed now delay
  read -r count next_allowed <<EOF
$(state_get "$unit")
EOF
  now=$(date +%s)
  if [ "$count" -ge "$MAX_ATTEMPTS" ]; then
    if [ "$now" -lt "$next_allowed" ]; then
      echo "watchdog: reinstall attempt cap ($MAX_ATTEMPTS) reached for $unit; backing off until $next_allowed" >&2
      return 1
    fi
    # Backoff has expired: a transient failure that exhausted the cap
    # must not disable self-heal forever (2026-11-13: the watchdog
    # service unit went missing, its reinstall hit the cap, and the
    # timer then sat inactive with no further retry). Reset and try again.
    echo "watchdog: backoff expired for $unit; resetting attempt count and retrying reinstall" >&2
    count=0
    state_reset "$unit"
  fi
  if [ "$now" -lt "$next_allowed" ]; then
    echo "watchdog: backing off reinstall of $unit until $next_allowed (attempt $count)" >&2
    return 1
  fi
  if cp "$src" "$dst" && systemctl daemon-reload && systemctl enable "$enable_target" >/dev/null 2>&1; then
    if systemctl cat "$unit" >/dev/null 2>&1; then
      state_reset "$unit"
      return 0
    fi
    echo "watchdog: $unit not visible after reinstall (systemctl cat failed); not restarting" >&2
  else
    echo "watchdog: reinstall of $unit failed (copy/reload/enable)" >&2
  fi
  count=$((count + 1))
  delay=$((BACKOFF_BASE * (2 ** (count - 1))))
  state_set "$unit" "$count" $((now + delay))
  return 1
}

listener_reinstalled=0
if ! cmp -s "$SRC" "$DST"; then
  if reinstall_unit "$SRC" "$DST" "$UNIT"; then
    listener_reinstalled=1
  fi
else
  state_reset "$UNIT"
fi

# Also restore the watchdog's own units if they went missing or are stale.
watchdog_reinstalled=0
for u in slack-listener-watchdog.service slack-listener-watchdog.timer; do
  if ! cmp -s "$BASE/$u" "$DST_DIR/$u"; then
    if reinstall_unit "$BASE/$u" "$DST_DIR/$u" "$u" slack-listener-watchdog.timer; then
      watchdog_reinstalled=1
    fi
  else
    state_reset "$u"
  fi
done

# Also restore the hourly reconcile units (DLQ pending resends +
# ambiguous sends, final form agreed with Yama/Neiro 2026-11-13):
# without a timer the sweep never runs, so a held resend would sit
# forever. Found 2026-11-13: the code and tests existed but nothing
# scheduled reconcile_ambiguous.py, and no state file had ever been
# written.
reconcile_reinstalled=0
for u in slack-reconcile.service slack-reconcile.timer; do
  if ! cmp -s "$BASE/$u" "$DST_DIR/$u"; then
    if reinstall_unit "$BASE/$u" "$DST_DIR/$u" "$u" slack-reconcile.timer; then
      reconcile_reinstalled=1
    fi
  else
    state_reset "$u"
  fi
done

# Also restore the DM poll units (added 2026-11-14 with dm_poll.py):
# without a timer, Ata's own DMs (read through the user-token
# connector) are never polled, so new DMs would sit unseen.
dm_poll_reinstalled=0
for u in slack-dm-poll.service slack-dm-poll.timer; do
  if ! cmp -s "$BASE/$u" "$DST_DIR/$u"; then
    if reinstall_unit "$BASE/$u" "$DST_DIR/$u" "$u" slack-dm-poll.timer; then
      dm_poll_reinstalled=1
    fi
  else
    state_reset "$u"
  fi
done

# Also restore the auto-join units (added 2026-11-18): without the
# 1-minute timer, new public channels are no longer joined
# automatically and the bot would silently miss new channels.
auto_join_reinstalled=0
for u in slack-auto-join.service slack-auto-join.timer; do
  if ! cmp -s "$BASE/$u" "$DST_DIR/$u"; then
    if reinstall_unit "$BASE/$u" "$DST_DIR/$u" "$u" slack-auto-join.timer; then
      auto_join_reinstalled=1
    fi
  else
    state_reset "$u"
  fi
done

# Also restore the sync-check units (added 2026-11-18 night with the
# move out of this watchdog): without the 5-minute timer, drift
# between the live listener and the public copy goes unnoticed.
sync_check_reinstalled=0
for u in slack-sync-check.service slack-sync-check.timer; do
  if ! cmp -s "$BASE/$u" "$DST_DIR/$u"; then
    if reinstall_unit "$BASE/$u" "$DST_DIR/$u" "$u" slack-sync-check.timer; then
      sync_check_reinstalled=1
    fi
  else
    state_reset "$u"
  fi
done

# The timer itself can sit inactive even when every unit file is intact:
# after a VM replacement timers.target never becomes active (all system
# timers were dead on 2026-11-13), so `enable` alone does not start it,
# and the reinstall branch above only restarts the timer when a file
# differed. Ensure it is running on every pass, before the early-exit
# paths below (2026-11-13 21:18 JST: timer found inactive for the third
# time today despite identical unit files; listener was fine).
# timers.target itself can sit inactive after a VM restart even though the
# boot reached multi-user.target (2026-11-13: found inactive 12 min after a
# reboot, with no journal entries for it and no failed units; it is `static`,
# not masked/disabled). Starting our timer unit alone does not wake the
# other system timers (apt-daily, dpkg-db-backup, ... had no NEXT), so start
# the target itself too, before the timer check below (Yama's point in
# #slack-へようこそ, 2026-11-13: start the target, not just the timer).
if ! systemctl is-active --quiet timers.target; then
  systemctl start timers.target >/dev/null 2>&1
fi

if ! systemctl is-active --quiet slack-listener-watchdog.timer; then
  systemctl start slack-listener-watchdog.timer >/dev/null 2>&1
fi

if ! systemctl is-active --quiet slack-reconcile.timer; then
  systemctl start slack-reconcile.timer >/dev/null 2>&1
fi

if ! systemctl is-active --quiet slack-dm-poll.timer; then
  systemctl start slack-dm-poll.timer >/dev/null 2>&1
fi

if ! systemctl is-active --quiet slack-auto-join.timer; then
  systemctl start slack-auto-join.timer >/dev/null 2>&1
fi

if ! systemctl is-active --quiet slack-sync-check.timer; then
  systemctl start slack-sync-check.timer >/dev/null 2>&1
fi

# A reinstalled unit only takes effect after a restart (daemon-reload
# alone does not affect the running service), so restart right away and
# let the next run verify health instead of restarting twice.
if [ "$listener_reinstalled" = 1 ]; then
  if listener_py_ok; then
    if systemctl restart "$UNIT" >/dev/null 2>&1; then
      # Stamp the restart cooldown (same file the heartbeat path
      # uses): a second watchdog run overlapping this one -- the
      # timer, the hook self-heal, or the recovery cron -- must not
      # restart the just-started listener again while its first
      # heartbeat (15s cadence) is still pending. Found 2026-11-22:
      # after a VM replacement the listener was restarted twice
      # within 5s (22:38:44 and 22:38:49 UTC) because the reinstall
      # path never stamped the cooldown.
      echo "$(date +%s)" > "$COOLDOWN_FILE"
    fi
    exit 0
  else
    echo "watchdog: listener.py does not compile; skipping restart after reinstall" >&2
  fi
fi
if [ "$watchdog_reinstalled" = 1 ]; then
  systemctl restart slack-listener-watchdog.timer >/dev/null 2>&1
fi
if [ "$reconcile_reinstalled" = 1 ]; then
  systemctl restart slack-reconcile.timer >/dev/null 2>&1
fi
if [ "$dm_poll_reinstalled" = 1 ]; then
  systemctl restart slack-dm-poll.timer >/dev/null 2>&1
fi
if [ "$auto_join_reinstalled" = 1 ]; then
  systemctl restart slack-auto-join.timer >/dev/null 2>&1
fi
if [ "$sync_check_reinstalled" = 1 ]; then
  systemctl restart slack-sync-check.timer >/dev/null 2>&1
fi

if ! systemctl is-active --quiet "$UNIT"; then
  systemctl start "$UNIT" >/dev/null 2>&1
  exit 0
fi

now=$(date +%s)
hb_mtime=$(stat -c %Y "$HB" 2>/dev/null || echo 0)
age=$(( now - hb_mtime ))
if [ "$age" -gt 120 ] || ! grep -q '"connected": true' "$HB" 2>/dev/null; then
  # Re-check after 5s (Yama's share, thread 1795006461.907139,
  # adopted 2026-11-19): a single stale read -- e.g. the heartbeat
  # file being written at the moment of stat, or a transient
  # disconnect -- must not restart the listener. Only a heartbeat
  # that is still stale/disconnected after the wait triggers a
  # restart.
  sleep 5
  now=$(date +%s)
  hb_mtime=$(stat -c %Y "$HB" 2>/dev/null || echo 0)
  age=$(( now - hb_mtime ))
  if [ "$age" -gt 120 ] || ! grep -q '"connected": true' "$HB" 2>/dev/null; then
    # Record host identity at detection time (Neiro's proposal,
    # thread 1795611852.507439): before any restart decision, so
    # the record exists even when the cooldown suppresses it.
    record_detection_identity "$age"
    # Cooldown after a successful restart (same share): the next
    # check can land while the listener is still starting up, and
    # must not fire a second restart at the same heartbeat. 60s
    # matches Yama's cooldown; our timer runs every 5 minutes, so
    # this mainly guards back-to-back or manual runs and the hook's
    # self-heal call. The stamp is written only when the restart
    # itself succeeded; a failed restart stays retryable.
    # (COOLDOWN_FILE is defined near the top of the script and is
    # also stamped by the post-reinstall restart above.)
    last_restart=$(cat "$COOLDOWN_FILE" 2>/dev/null || echo 0)
    case "$last_restart" in ''|*[!0-9]*) last_restart=0 ;; esac
    if [ $(( now - last_restart )) -ge 60 ]; then
      if listener_py_ok; then
        if systemctl restart "$UNIT" >/dev/null 2>&1; then
          echo "$now" > "$COOLDOWN_FILE"
        fi
      else
        echo "watchdog: listener.py does not compile; skipping restart" >&2
      fi
    fi
  fi
fi

# Stale-pending warning (agy review 2026-11-18, thread 1795006461.907139):
# the hook itself cannot notice when it has stopped -- an event-driven
# hook cannot run to report its own death -- so this timer-driven
# watchdog checks the oldest unprocessed inbox message. The threshold
# is 900s (raised from 180s on 2026-11-23, Yama's share in thread
# 1795400905.365719): measured post-to-confirm latency is median 177s,
# p90 577s, so 180s cried wolf on roughly half of slow batches. 900s
# stays well above normal slow processing while still catching a
# genuinely dead hook within ~15-20 min. It is deliberately later than
# the 120s heartbeat restart above, so the warning is an escalation
# for "the restart did not fix it", not a pre-warning (a transient
# flap must not cry wolf). Dedup is one
# flag file: created when a warning is sent, removed when the queue
# drains, so an unresolved stall warns once and a self-recovery resets
# the latch (no hash state file needed). That is also why Neiro's
# episode cooldown (3600s, shared 2026-11-24, commit ccc2890) needs
# no separate implementation here -- the flag already warns once per
# stall episode however often the oldest item turns over. Never
# blocks the watchdog: every step is best-effort and the script
# still exits 0 below.
#
# Two-tier thresholds (adopted 2026-11-25 from Neiro's share in
# #slack-へようこそ, commit 865c873, their "Yama方式の取り入れ第2件"):
# the oldest pending age is split by author kind -- human posts
# warn at the main threshold (900s, unchanged), bot-to-bot posts
# only at 10x that (9000s, Neiro's pending ratio: their 180s ->
# 1800s). When both tiers are over, only the main warning goes out,
# on the oldest human message; the sub warning fires only for a
# bot-only backlog. Each tier has its own flag file, so a sub
# warning never consumes the main tier's one-warning-per-episode
# latch (the 3600s episode cooldown stays unnecessary here for the
# same reason as before: each flag warns once per stall episode
# and resets on drain). One deliberate difference from Neiro's:
# no author_kind is recorded at claim time -- our msgstate pending
# list already returns the full inbox record, which carries bot_id,
# so the kind is derived at read time (bot_id present = bot, else
# human; unmarked old records read as human, never the lenient
# side). The processing-side thresholds need no split either: our
# age metric already excludes processing items, and the worker-alive
# gate below suppresses both tiers while a claim is live (adopted
# 2026-11-24 from Neiro's share, commit ccc2890 -- that was the
# shape of all 9 of our recent warnings, and of all of Neiro's 27).
# A stuck claim cannot mask the warning for long: the stale sweep
# reclaims it within 600s without touch, or 1800s total claim
# lifetime (MAX_CLAIM_SECS, touch loophole closed 2026-11-22),
# after which processing drops to 0 and the warning fires.
STALL_FLAG=${WATCHDOG_STALL_FLAG:-$BASE/events/pending_stall.flag}
STALL_BOT_FLAG=${WATCHDOG_STALL_BOT_FLAG:-$BASE/events/pending_stall_bot.flag}
STALL_THRESHOLD=${WATCHDOG_STALL_THRESHOLD:-900}
# Sub tier defaults to 10x the main threshold (Neiro's pending
# ratio) and tracks a test override of the main value, so a test
# that sets WATCHDOG_STALL_THRESHOLD=10 gets a 100s sub tier
# without a second override. Set WATCHDOG_STALL_BOT_THRESHOLD
# explicitly to break the ratio.
STALL_BOT_THRESHOLD=${WATCHDOG_STALL_BOT_THRESHOLD:-$((STALL_THRESHOLD * 10))}
STALL_CHANNEL=${WATCHDOG_STALL_CHANNEL:-C0EXAMPLE001}
# Per-kind oldest ages, one snapshot (msgstate.py
# oldest-pending-age --json): {"human": secs|null, "bot": secs|null,
# "oldest_author_kind": ...}. Unreadable output leaves both empty
# and both tiers inactive, so the flags drain as before.
stall_ages=$(python3 "$BASE/msgstate.py" oldest-pending-age --json 2>/dev/null || echo "")
stall_human_age=$(printf '%s' "$stall_ages" | python3 -c "import json,sys; v=json.load(sys.stdin).get('human'); print(v if v is not None else '')" 2>/dev/null || echo "")
stall_bot_age=$(printf '%s' "$stall_ages" | python3 -c "import json,sys; v=json.load(sys.stdin).get('bot'); print(v if v is not None else '')" 2>/dev/null || echo "")
case "$stall_human_age" in ''|*[!0-9]*) stall_human_age="" ;; esac
case "$stall_bot_age" in ''|*[!0-9]*) stall_bot_age="" ;; esac
# Counts from Yanayana's own ledger (msgstate.py count-pending
# --json, one snapshot): pending + in-flight. The oldest age above
# already excludes processing items, so without these numbers a
# warning cannot tell "worker stopped" from "busy processing a long
# batch" -- the false-alarm confusion Neiro hit on 2026-11-24,
# fixed on their side the same day by adding their processing
# count ("うち処理中 N件"). Best-effort: if the counts cannot be
# read, the warning still goes out without them rather than failing.
stall_counts=$(python3 "$BASE/msgstate.py" count-pending --json 2>/dev/null || echo "")
stall_pending=$(printf '%s' "$stall_counts" | python3 -c "import json,sys; print(json.load(sys.stdin).get('pending',''))" 2>/dev/null || echo "")
stall_processing=$(printf '%s' "$stall_counts" | python3 -c "import json,sys; print(json.load(sys.stdin).get('processing',''))" 2>/dev/null || echo "")
stall_bot_pending=$(printf '%s' "$stall_counts" | python3 -c "import json,sys; print(json.load(sys.stdin).get('bot_pending',''))" 2>/dev/null || echo "")
case "$stall_pending" in ''|*[!0-9]*) stall_pending="" ;; esac
case "$stall_processing" in ''|*[!0-9]*) stall_processing="" ;; esac
case "$stall_bot_pending" in ''|*[!0-9]*) stall_bot_pending="" ;; esac
if [ -n "$stall_pending" ] && [ -n "$stall_processing" ]; then
  stall_counts_text="Yanayana側の台帳では未処理 ${stall_pending}件・処理中 ${stall_processing}件です。"
  if [ -n "$stall_bot_pending" ]; then
    stall_bot_counts_text="Yanayana側の台帳では未処理 ${stall_pending}件（うちボット ${stall_bot_pending}件）・処理中 ${stall_processing}件です。"
  else
    stall_bot_counts_text="$stall_counts_text"
  fi
else
  stall_counts_text=""
  stall_bot_counts_text=""
fi
# Tier activity: main = oldest human pending over the main
# threshold; sub = oldest bot pending over the sub threshold AND
# the main tier quiet (both over -> main only, on the oldest human
# message, as in Neiro's design).
stall_main_active=0
if [ -n "$stall_human_age" ] && [ "$stall_human_age" -ge "$STALL_THRESHOLD" ]; then
  stall_main_active=1
fi
stall_bot_active=0
if [ -n "$stall_bot_age" ] && [ "$stall_bot_age" -ge "$STALL_BOT_THRESHOLD" ] && [ "$stall_main_active" -eq 0 ]; then
  stall_bot_active=1
fi
if [ -n "$stall_processing" ] && [ "$stall_processing" -gt 0 ]; then
  # A worker is alive and working through the backlog: not a
  # stall, do not warn on either tier. The flags are left as-is --
  # if an episode was already warned about, it stays one warning
  # until the queue drains; if the worker then dies, processing
  # drops to 0 and the next run warns.
  :
else
  if [ "$stall_main_active" -eq 1 ]; then
    if [ ! -f "$STALL_FLAG" ]; then
      stall_msg="【未処理の滞留】最古の未処理メッセージが ${stall_human_age} 秒以上処理されていません（しきい値 ${STALL_THRESHOLD} 秒）。${stall_counts_text}フックまたは処理役が止まっている可能性があります。リスナーの再起動では直らなかった滞留です。確認してください。"
      if timeout 30 "$BASE/bin/slackcli" messages send --recipient-id "$STALL_CHANNEL" --message "$stall_msg" --json >/dev/null 2>&1; then
        mkdir -p "$(dirname "$STALL_FLAG")"
        touch "$STALL_FLAG"
      fi
    fi
  else
    rm -f "$STALL_FLAG"
  fi
  if [ "$stall_bot_active" -eq 1 ]; then
    if [ ! -f "$STALL_BOT_FLAG" ]; then
      stall_msg="【未処理の滞留】ボット同士の未処理投稿です（副の長めのしきい値での警告）。最古の未処理メッセージが ${stall_bot_age} 秒以上処理されていません（しきい値 ${STALL_BOT_THRESHOLD} 秒）。${stall_bot_counts_text}フックまたは処理役が止まっている可能性があります。リスナーの再起動では直らなかった滞留です。確認してください。"
      if timeout 30 "$BASE/bin/slackcli" messages send --recipient-id "$STALL_CHANNEL" --message "$stall_msg" --json >/dev/null 2>&1; then
        mkdir -p "$(dirname "$STALL_BOT_FLAG")"
        touch "$STALL_BOT_FLAG"
      fi
    fi
  else
    rm -f "$STALL_BOT_FLAG"
  fi
fi

# Unit-failure warnings (Neiro's design, adopted 2026-11-19, thread
# 1795006461.907139): a oneshot that exits non-zero sits in "failed"
# state until it succeeds again, so `systemctl is-failed` on the
# sync-check and auto-join units catches hard failures the checks
# themselves cannot report -- a long failure would otherwise sit
# unnoticed as only a unit state / jsonl record. The sync-check
# runner exits 0 after a drift warning is posted, so "failed" here
# means the check could not run or could not warn; drift itself
# never trips this. One flag file per unit dedups the warning; it is
# removed when the unit recovers. Best-effort, never blocks the
# watchdog: a failed send simply retries on the next run.
UNIT_FAIL_CHANNEL=${WATCHDOG_UNIT_FAIL_CHANNEL:-C0EXAMPLE001}
for unit in slack-sync-check.service slack-auto-join.service; do
  case "$unit" in
    slack-sync-check.service) role="同期チェック" ;;
    slack-auto-join.service) role="新チャンネルへの自動参加" ;;
  esac
  unit_flag="$BASE/events/unit_failed_${unit%.service}.flag"
  if systemctl is-failed --quiet "$unit" 2>/dev/null; then
    if [ ! -f "$unit_flag" ]; then
      unit_msg="【ユニットの失敗】${unit} が failed 状態です。${role}が動いていません。確認してください。"
      if timeout 30 "$BASE/bin/slackcli" messages send --recipient-id "$UNIT_FAIL_CHANNEL" --message "$unit_msg" --json >/dev/null 2>&1; then
        mkdir -p "$(dirname "$unit_flag")"
        touch "$unit_flag"
      fi
    fi
  elif [ "$(systemctl show -p Result --value "$unit" 2>/dev/null)" = "success" ]; then
    # 回復は成功した実行でだけ認める: "failed でない" だけだと、タイマー
    # 発火でユニットが走り直している最中に watchdog が通ってフラグを
    # 消し、直後に再失敗したときに重複警告してしまう（agy の指摘）。
    # まだ一度も成功していない／実行中の場合はフラグを残す。
    rm -f "$unit_flag"
  fi
done
exit 0
