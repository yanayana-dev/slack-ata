#!/usr/bin/env bash
# Watchdog for slack-listener (modeled on Neiro's setup):
# - reinstalls the systemd unit if it went missing or is stale
#   (hash comparison, adopted 2026-11-12 from the Yama/Neiro thread:
#   "missing or hash differs" also catches a stale file left behind,
#   not just a deleted one)
# - restarts the service if it is not active
# - restarts it if the heartbeat is stale (>120s) or not connected
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
MAX_ATTEMPTS=3
BACKOFF_BASE=30

# Hook health check (adopted 2026-11-15 from Neiro's check-script share):
# syntax of ~/hooks/scripts/*.sh and JSON of ~/hooks/definitions/*.json;
# warns to Slack on new/changed failures. Never blocks the watchdog.
"$BASE/check_hooks.sh" >/dev/null 2>&1 || true

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

# A reinstalled unit only takes effect after a restart (daemon-reload
# alone does not affect the running service), so restart right away and
# let the next run verify health instead of restarting twice.
if [ "$listener_reinstalled" = 1 ]; then
  systemctl restart "$UNIT" >/dev/null 2>&1
  exit 0
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

if ! systemctl is-active --quiet "$UNIT"; then
  systemctl start "$UNIT" >/dev/null 2>&1
  exit 0
fi

now=$(date +%s)
hb_mtime=$(stat -c %Y "$HB" 2>/dev/null || echo 0)
age=$(( now - hb_mtime ))
if [ "$age" -gt 120 ] || ! grep -q '"connected": true' "$HB" 2>/dev/null; then
  systemctl restart "$UNIT" >/dev/null 2>&1
fi
exit 0
