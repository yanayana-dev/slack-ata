#!/usr/bin/env bash
# slack-listener-sync-check.sh — 稼働中のリスナーと公開用コピーの同期チェック。
#
# 背景（2026-11-18、Neiroさんの共有 e79a783 を取り込んだ Yanayana 版）:
# Neiroさんの指摘で分かった罠 — リスナーの .py は実体コピーのままで自動の
# 照合がなく、リポジトリにだけあるガードが稼働中の auto-join.py へ反映
# されていないずれが、手作業の diff でしか見つからなかった。同じ罠を繰り返
# さないため、稼働中のコピーと公開用コピーを照合する。
#
# Yanayana 版の一点だけの違い: 公開用コピーは scrub 済み（実ID→プレース
# ホルダ）なので、素の diff では全部ずれに見えてしまう。稼働中のファイルに
# scrub.sed をかけた一時コピーと公開用コピーを比べる。
#
# 検証するもの（対象は公開用コピーにあるトップレベルの *.py, *.sh,
# *.service, *.timer。稼働中にだけある config.json / oauth_helper_server.py /
# proxy.env / bin/ / events/ は対象外）:
#   1. 公開用コピーにあるファイルに対応する稼働中のファイルが存在すること
#   2. scrub 後の内容が一致すること
#   3. .py は両方とも py_compile を通り、.sh は両方とも bash -n を通ること
#   4. 稼働中にだけある公開対象ファイルが無いこと（同期忘れの検出。
#      config.json 等の既知の稼働中専用ファイルは除く）
# ファイル名の対応: 公開用の slack-listener.service は稼働中の
# slack-listener.service に対応する（scrub で名前が変わるため）。
# このスクリプト自身も公開用コピーにあるので照合対象に入る。Yamaさんの
# 「watchdog.sh 自体は誰が照合するのか」という問いへの答えもこれ:
# watchdog.sh は公開用コピーにあるので、このチェックが照合する。
# 残る隙間は「チェック自体が壊れて走らない場合」で、それは watchdog の
# ログと systemd の失敗状態でしか見えない（3者共通の課題として持ち帰り）。
#
# 実行場所: watchdog.sh から5分間隔で呼ぶ。check_hooks.sh と同じ形で、
# 異常時は Slack へ警告する（重複抑止は署名付き状態ファイル）。正常時は
# 状態ファイルを消して "listener sync: ok" を出して exit 0。
# watchdog は root で動くため、既定パスは $HOME ではなく絶対パスで書く。
#
# 環境変数（テスト用）: LIVE_DIR, PUBLIC_DIR, SCRUB_SED, SYNC_CHECK_STATE,
# SYNC_CHECK_CHANNEL, SLACKCLI, --dry-run。
set -u
LIVE_DIR=${LIVE_DIR:-/path/to/slack-watcher}
PUBLIC_DIR=${PUBLIC_DIR:-/path/to/workspace/github-publish/slack-ata}
SCRUB_SED=${SCRUB_SED:-/path/to/workspace/github-publish/scrub.sed}
BASE=${SYNC_CHECK_BASE:-$LIVE_DIR}
STATE_FILE=${SYNC_CHECK_STATE:-$BASE/events/listener_sync_health.json}
CHANNEL=${SYNC_CHECK_CHANNEL:-C0EXAMPLE001}
SLACKCLI=${SLACKCLI:-$BASE/bin/slackcli}
DRY_RUN=0
if [ "${1:-}" = "--dry-run" ]; then DRY_RUN=1; fi

# 稼働中専用で公開しないファイル（名前で指定）。これ以外に稼働中にだけ
# ある公開対象ファイルがあれば UNPUBLISHED として警告する。
LIVE_ONLY="config.json oauth_helper_server.py proxy.env"

PROBLEMS=0
failures=()

WORKDIR=$(mktemp -d)
trap 'rm -rf "$WORKDIR"' EXIT

# live 名 ← public 名の対応。公開用の slack-listener.service は稼働中の
# slack-listener.service に対応する。
live_name() {
  case "$1" in
    slack-listener.service) echo "slack-listener.service" ;;
    *) echo "$1" ;;
  esac
}

# public 名 ← live 名の対応（UNPUBLISHED 判定用）。
public_name() {
  case "$1" in
    slack-listener.service) echo "slack-listener.service" ;;
    *) echo "$1" ;;
  esac
}

check_one() {
  local rel="$1" live="$LIVE_DIR/$(live_name "$rel")" pub="$PUBLIC_DIR/$rel"
  if [ ! -e "$live" ]; then
    failures+=("MISSING: $live （稼働中のファイルが無い）")
    PROBLEMS=$((PROBLEMS + 1))
    return
  fi
  local scrubbed="$WORKDIR/scrubbed"
  if ! sed -f "$SCRUB_SED" "$live" > "$scrubbed" 2>/dev/null; then
    failures+=("SCRUB-ERROR: $live （scrub.sed がかけられない）")
    PROBLEMS=$((PROBLEMS + 1))
    return
  fi
  if ! cmp -s "$scrubbed" "$pub"; then
    failures+=("DRIFT: $live （公開用コピーと内容が違う。稼働中の変更を公開用へ戻すか、公開用の変更を稼働中へ反映すること）")
    PROBLEMS=$((PROBLEMS + 1))
  fi
  case "$rel" in
    *.sh)
      bash -n "$live" 2>/dev/null || { failures+=("SYNTAX-ERROR: $live （bash -n が通らない）"); PROBLEMS=$((PROBLEMS + 1)); }
      bash -n "$pub" 2>/dev/null || { failures+=("SYNTAX-ERROR: $pub （bash -n が通らない）"); PROBLEMS=$((PROBLEMS + 1)); }
      ;;
    *.py)
      python3 -m py_compile "$live" 2>/dev/null || { failures+=("SYNTAX-ERROR: $live （py_compile が通らない）"); PROBLEMS=$((PROBLEMS + 1)); }
      python3 -m py_compile "$pub" 2>/dev/null || { failures+=("SYNTAX-ERROR: $pub （py_compile が通らない）"); PROBLEMS=$((PROBLEMS + 1)); }
      ;;
  esac
}

if [ ! -f "$SCRUB_SED" ]; then
  failures+=("SCRUB-MISSING: $SCRUB_SED （scrub.sed が無いので比較できない）")
  PROBLEMS=$((PROBLEMS + 1))
else
  while IFS= read -r -d '' f; do
    rel="${f#"$PUBLIC_DIR"/}"
    check_one "$rel"
  done < <(find "$PUBLIC_DIR" -maxdepth 1 -type f \( -name '*.py' -o -name '*.sh' -o -name '*.service' -o -name '*.timer' \) -print0 2>/dev/null)
  # 稼働中にだけある公開対象ファイル（同期忘れ）を検出する。
  while IFS= read -r -d '' f; do
    name="${f#"$LIVE_DIR"/}"
    case " $LIVE_ONLY " in *" $name "*) continue ;; esac
    if [ ! -e "$PUBLIC_DIR/$(public_name "$name")" ]; then
      failures+=("UNPUBLISHED: $f （稼働中にだけある。公開用コピーへ同期すること）")
      PROBLEMS=$((PROBLEMS + 1))
    fi
  done < <(find "$LIVE_DIR" -maxdepth 1 -type f \( -name '*.py' -o -name '*.sh' -o -name '*.service' -o -name '*.timer' \) -print0 2>/dev/null)
fi

if [ "$PROBLEMS" -eq 0 ]; then
  rm -f "$STATE_FILE"
  echo "listener sync: ok"
  exit 0
fi

text=$(printf '%s\n' "${failures[@]}")
sig=$(printf '%s' "$text" | sha256sum | cut -d' ' -f1)
prev=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("signature",""))' "$STATE_FILE" 2>/dev/null || echo "")
if [ "$sig" = "$prev" ]; then
  echo "listener sync: still failing (already warned)"
  printf '%s\n' "$text"
  echo "NG: ${PROBLEMS}件の問題あり"
  exit 1
fi

msg="【リスナー同期チェック】稼働中の ~/workspace/slack-listener と公開用コピーの内容がずれています（watchdogから5分間隔で確認中）:
$text
稼働中の変更を公開用へ戻すか、公開用の変更を稼働中へ反映してください。"
if [ "$DRY_RUN" = "1" ]; then
  echo "would warn to $CHANNEL:"
  printf '%s\n' "$msg"
  printf '%s\n' "$text"
  echo "NG: ${PROBLEMS}件の問題あり"
  exit 1
fi
if ! "$SLACKCLI" messages send --recipient-id "$CHANNEL" --message "$msg" --json >/dev/null 2>&1; then
  echo "listener sync: Slack warn send failed" >&2
  printf '%s\n' "$text"
  echo "NG: ${PROBLEMS}件の問題あり"
  exit 1
fi
tmp="$STATE_FILE.tmp.$$"
mkdir -p "$(dirname "$STATE_FILE")"
printf '{"signature": "%s", "failures": %s}' "$sig" "$(printf '%s' "$text" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')" > "$tmp" && mv "$tmp" "$STATE_FILE"
printf '%s\n' "$text"
echo "NG: ${PROBLEMS}件の問題あり"
exit 1
