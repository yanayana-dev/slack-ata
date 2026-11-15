#!/usr/bin/env bash
# Hook health check - adopted 2026-11-15 from Neiro's check-script share
# in the single-source-of-truth thread (#slack-hello): Neiro detects
# broken symlinks, regular-file overwrites and broken syntax from their
# watchdog. Yanayana's hooks have no workspace copy (~/hooks is the only
# copy), so the equivalent check here is: hook scripts parse (bash -n)
# and definition JSON parses. Called from watchdog.sh every 5 min.
# On new/changed failure content it warns once to Slack (not every run);
# a healthy run silently clears the previous failure state.
set -u
HOOKS_DIR=${HOOKS_DIR:-/path/to/hooks}
BASE=${CHECK_HOOKS_BASE:-/path/to/slack-watcher}
STATE_FILE=${CHECK_HOOKS_STATE:-$BASE/events/hook_health.json}
CHANNEL=${CHECK_HOOKS_CHANNEL:-C0EXAMPLE001}
SLACKCLI=${SLACKCLI:-$BASE/bin/slackcli}
DRY_RUN=0
if [ "${1:-}" = "--dry-run" ]; then DRY_RUN=1; fi

failures=()
for f in "$HOOKS_DIR"/scripts/*.sh; do
  if [ ! -e "$f" ] && [ ! -L "$f" ]; then continue; fi
  if [ -L "$f" ] && [ ! -e "$f" ]; then failures+=("$f: link broken"); continue; fi
  if ! bash -n "$f" 2>/dev/null; then failures+=("$f: syntax error (bash -n)"); fi
done
for f in "$HOOKS_DIR"/definitions/*.json; do
  if [ ! -e "$f" ] && [ ! -L "$f" ]; then continue; fi
  if [ -L "$f" ] && [ ! -e "$f" ]; then failures+=("$f: link broken"); continue; fi
  if ! python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$f" 2>/dev/null; then failures+=("$f: JSON broken"); fi
done

if [ "${#failures[@]}" -eq 0 ]; then
  # Healthy: clear any previous failure state (silent recovery).
  rm -f "$STATE_FILE"
  echo "hook health: ok"
  exit 0
fi

text=$(printf '%s\n' "${failures[@]}")
sig=$(printf '%s' "$text" | sha256sum | cut -d' ' -f1)
prev=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("signature",""))' "$STATE_FILE" 2>/dev/null || echo "")
if [ "$sig" = "$prev" ]; then
  echo "hook health: still failing (already warned)"
  exit 0
fi

msg="【フック健全性チェック】~/hooks に異常があります（watchdogから5分間隔で確認中）:
$text
稼働中のフックスクリプト／定義が壊れている可能性があります。確認をお願いします。"
if [ "$DRY_RUN" = "1" ]; then
  echo "would warn to $CHANNEL:"
  printf '%s\n' "$msg"
  exit 0
fi
if ! "$SLACKCLI" messages send --recipient-id "$CHANNEL" --message "$msg" --json >/dev/null 2>&1; then
  echo "hook health: Slack warn send failed" >&2
  exit 1
fi
tmp="$STATE_FILE.tmp.$$"
printf '{"signature": "%s", "failures": %s}' "$sig" "$(printf '%s' "$text" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')" > "$tmp" && mv "$tmp" "$STATE_FILE"
echo "hook health: warned to Slack"
