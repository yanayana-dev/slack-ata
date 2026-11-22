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
# *.service, *.timer と tests/ 以下の *.py（再帰）。稼働中にだけある
# config.json / oauth_helper_server.py / proxy.env / bin/ / events/
# は対象外）:
#   1. 公開用コピーにあるファイルに対応する稼働中のファイルが存在すること
#   2. scrub 後の内容が一致すること
#   3. .py は両方とも py_compile を通り、.sh は両方とも bash -n を通ること
#      （py_compile は scrub 後のテキストではなく元のファイルにかける。
#      置換で構文が壊れる心配を避けるため。2026-11-21、Neiroさんの指摘）
#   4. 稼働中にだけある公開対象ファイルが無いこと（同期忘れの検出。
#      config.json 等の既知の稼働中専用ファイルは除く）
# tests/ は 2026-11-21 に範囲へ追加した。以前はトップレベルのみで、
# tests/ のずれ（例：2026-11-20 の curl rc=6 再現テスト）は見逃していた。
# tests/ の .bak は対象外（拡張子が .py ではないため拾われない）。
# tests/ はサブディレクトリも含めて再帰で照合する（2026-11-21、
# Neiroさんの確認への答え：maxdepth 1 だと将来のサブディレクトリの
# テストやテスト用データで同じ見逃しが起きる）。
# tests/.local-only に列挙したテスト（tests/ からの相対パス、1行1件。
# 例：test_local_debug.py、sub/test_x.py）はローカル専用として
# UNPUBLISHED 対象外にする（同・確認への答え：環境固有の事情で公開
# しないテストの逃がし方。ファイル自体は *.py ではないので照合対象に
# は入らない）。書きかけのテストの構文エラーは本番ファイルと同じ重さ
# で止める（同・確認への答え：テストも公開物のため、壊れたまま同期は
# しない。1件の失敗で他のずれが見えなくなることはない：全ファイル分
# の問題を集めてから警告する）。
# ファイル名の対応: 公開用の slack-listener.service は稼働中の
# slack-listener.service に対応する（scrub で名前が変わるため）。
# このスクリプト自身も公開用コピーにあるので照合対象に入る。Yamaさんの
# 「watchdog.sh 自体は誰が照合するのか」という問いへの答えもこれ:
# watchdog.sh は公開用コピーにあるので、このチェックが照合する。
# 残る隙間は「チェック自体が壊れて走らない場合」だったが、2026-11-19
# に runner（slack-sync-check-runner.sh）が drift 警告後も exit 0 に
# 正規化し、watchdog.sh が is-failed で硬い失敗だけを Slack へ警告する
# 形にしたので、ユニットの failed 状態がその隙間を埋める。
#
# 実行場所: 当初は watchdog.sh から5分間隔で呼んでいたが、2026-11-18
# 夜に専用 systemd タイマー（slack-sync-check.timer、5分間隔）へ移行した
# （スレッドで宣言した方針。watchdog 自身が呼び出し元だと、watchdog の
# ずれを watchdog が見る自己参照が残るため）。異常時は Slack へ警告する
# （重複抑止は署名付き状態ファイル）。正常時は状態ファイルを消して
# "listener sync: ok" を出して exit 0。
#
# 警告の猶予期間（2026-11-22、あたの指摘で追加）: ずれを見つけても
# 即座には警告しない。同じ署名のずれが SYNC_CHECK_GRACE_SECONDS
# （既定900秒＝5分間隔で3回分）続いたときだけ警告する。活発に作業
# している最中は同期前のずれが数分で解消されることが多く、即時警告
# だと #slack-へようこそ が警告と解消報告で埋まってしまうため。
# 初回検出時は状態ファイルに署名と first_seen を記録して黙り、署名が
# 変われば時計はリセットされ、ずれが解消されれば状態ファイルごと
# 消える。警告済みのずれが変化した場合も、同じ猶予ルールを通す
# （一貫した扱いにして、作業中の署名の揺れで警告が増えないように）。
# 警告送信の失敗やチェック自体の異常終了は猶予の対象外で、従来どおり
# 即座に表面化する（送信失敗は first_seen を残したまま exit 1 になり、
# runner / watchdog の is-failed 警告が拾う）。
# systemd は root で動くため、既定パスは $HOME ではなく絶対パスで書く。
#
# 環境変数（テスト用）: LIVE_DIR, PUBLIC_DIR, SCRUB_SED, SYNC_CHECK_STATE,
# SYNC_CHECK_CHANNEL, SLACKCLI, SYNC_CHECK_GRACE_SECONDS, --dry-run。
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
# ずれの警告までの猶予（秒）。同じ署名のずれがこの秒数続いたら警告する。
GRACE_SECONDS=${SYNC_CHECK_GRACE_SECONDS:-900}
case "$GRACE_SECONDS" in *[!0-9]*|"") GRACE_SECONDS=900 ;; esac

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
    failures+=("DRIFT: $live （公開用コピーと内容が違う。稼働側が正本なので、稼働中の内容で公開用コピーを作り直すこと）")
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

# tests/.local-only に列挙されたテスト（tests/ からの相対パス）は
# ローカル専用として UNPUBLISHED 対象外にする。
is_local_only_test() {
  local rel="$1" list="$LIVE_DIR/tests/.local-only"
  [ -f "$list" ] && grep -Fxq -- "$rel" "$list"
}

if [ ! -f "$SCRUB_SED" ]; then
  failures+=("SCRUB-MISSING: $SCRUB_SED （scrub.sed が無いので比較できない）")
  PROBLEMS=$((PROBLEMS + 1))
else
  while IFS= read -r -d '' f; do
    rel="${f#"$PUBLIC_DIR"/}"
    check_one "$rel"
  done < <(find "$PUBLIC_DIR" -maxdepth 1 -type f \( -name '*.py' -o -name '*.sh' -o -name '*.service' -o -name '*.timer' \) -print0 2>/dev/null)
  # tests/ も同じ形で照合する（scrub 比較＋py_compile）。サブディレクトリ
  # も含めて再帰で見る（maxdepth 1 だと将来のサブディレクトリのテストで
  # 同じ見逃しが起きる）。
  while IFS= read -r -d '' f; do
    rel="${f#"$PUBLIC_DIR"/}"
    check_one "$rel"
  done < <(find "$PUBLIC_DIR/tests" -type f -name '*.py' -print0 2>/dev/null)
  # 稼働中にだけある公開対象ファイル（同期忘れ）を検出する。
  while IFS= read -r -d '' f; do
    name="${f#"$LIVE_DIR"/}"
    case " $LIVE_ONLY " in *" $name "*) continue ;; esac
    if [ ! -e "$PUBLIC_DIR/$(public_name "$name")" ]; then
      failures+=("UNPUBLISHED: $f （稼働中にだけある。公開用コピーへ同期すること）")
      PROBLEMS=$((PROBLEMS + 1))
    fi
  done < <(find "$LIVE_DIR" -maxdepth 1 -type f \( -name '*.py' -o -name '*.sh' -o -name '*.service' -o -name '*.timer' \) -print0 2>/dev/null)
  # tests/ にだけあるテストも UNPUBLISHED で拾う（.bak は対象外）。
  # tests/.local-only に列挙されたローカル専用テストは除く。
  while IFS= read -r -d '' f; do
    name="${f#"$LIVE_DIR"/}"
    if is_local_only_test "${name#tests/}"; then continue; fi
    if [ ! -e "$PUBLIC_DIR/$(public_name "$name")" ]; then
      failures+=("UNPUBLISHED: $f （稼働中にだけある。公開用コピーへ同期すること）")
      PROBLEMS=$((PROBLEMS + 1))
    fi
  done < <(find "$LIVE_DIR/tests" -type f -name '*.py' -print0 2>/dev/null)
fi

if [ "$PROBLEMS" -eq 0 ]; then
  rm -f "$STATE_FILE"
  echo "listener sync: ok"
  exit 0
fi

text=$(printf '%s\n' "${failures[@]}")
sig=$(printf '%s' "$text" | sha256sum | cut -d' ' -f1)
msg="【リスナー同期チェック】稼働中の ~/workspace/slack-listener と公開用コピーの内容がずれています（専用タイマー slack-sync-check.timer で5分間隔で確認中）:
$text
稼働側が正本です。稼働中の内容で公開用コピーを作り直してください。"

# 状態ファイルを読む。旧形式（signature と failures だけ）は警告が
# 送れたときにだけ書かれていたので、warned キーが無ければ警告済み
# として扱う。
prev_sig=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("signature",""))' "$STATE_FILE" 2>/dev/null || echo "")
prev_first_seen=$(python3 -c 'import json,sys; print(int(json.load(open(sys.argv[1])).get("first_seen",0)))' "$STATE_FILE" 2>/dev/null || echo 0)
prev_warned=$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(1 if d.get("warned", True) else 0)' "$STATE_FILE" 2>/dev/null || echo 0)

if [ "$sig" = "$prev_sig" ] && [ "$prev_warned" = "1" ]; then
  echo "listener sync: still failing (already warned)"
  printf '%s\n' "$text"
  echo "NG: ${PROBLEMS}件の問題あり"
  exit 1
fi

NOW=$(date +%s)
if [ "$sig" = "$prev_sig" ]; then FIRST_SEEN=$prev_first_seen; else FIRST_SEEN=$NOW; fi
ELAPSED=$((NOW - FIRST_SEEN))

write_state() {
  # $1 = warned (0/1)。tmp + rename の原子書き（AGENTS.md の作法）。
  mkdir -p "$(dirname "$STATE_FILE")"
  local tmp="$STATE_FILE.tmp.$$"
  SYNC_SIG="$sig" SYNC_FIRST_SEEN="$FIRST_SEEN" SYNC_WARNED="$1" SYNC_FAILURES="$text" \
    python3 -c 'import json,os; print(json.dumps({"signature": os.environ["SYNC_SIG"], "first_seen": int(os.environ["SYNC_FIRST_SEEN"]), "warned": os.environ["SYNC_WARNED"] == "1", "failures": os.environ["SYNC_FAILURES"]}))' > "$tmp" && mv "$tmp" "$STATE_FILE"
}

# 猶予期間中は警告せず、署名と first_seen を記録して黙る。ずれの内容は
# journal には残る（stdout）。runner は NG 行を見て exit 0 に正規化する
# ので、ユニットが failed になることもない。
if [ "$ELAPSED" -lt "$GRACE_SECONDS" ]; then
  if [ "$DRY_RUN" = "1" ]; then
    echo "would warn to $CHANNEL after grace period (elapsed ${ELAPSED}s of ${GRACE_SECONDS}s):"
    printf '%s\n' "$msg"
  else
    write_state 0
    echo "listener sync: drift detected, warning deferred (elapsed ${ELAPSED}s of grace ${GRACE_SECONDS}s)"
  fi
  printf '%s\n' "$text"
  echo "NG: ${PROBLEMS}件の問題あり"
  exit 1
fi

if [ "$DRY_RUN" = "1" ]; then
  echo "would warn to $CHANNEL:"
  printf '%s\n' "$msg"
  printf '%s\n' "$text"
  echo "NG: ${PROBLEMS}件の問題あり"
  exit 1
fi
# 送信前に pending 状態（warned=0）を残す。送信が失敗しても first_seen
# が残るので、次回は猶予を待たずに再送を試み、失敗は即座に表面化する。
write_state 0
if ! "$SLACKCLI" messages send --recipient-id "$CHANNEL" --message "$msg" --json >/dev/null 2>&1; then
  echo "listener sync: Slack warn send failed" >&2
  printf '%s\n' "$text"
  echo "NG: ${PROBLEMS}件の問題あり"
  exit 1
fi
write_state 1
printf '%s\n' "$text"
echo "NG: ${PROBLEMS}件の問題あり"
exit 1
