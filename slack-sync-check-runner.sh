#!/usr/bin/env bash
# slack-sync-check-runner.sh — slack-listener-sync-check.sh を systemd
# の oneshot（slack-sync-check.service）として呼ぶためのランナー。
#
# 背景（2026-11-19、Neiroさんの設計を採用）:
# 同期チェックは drift があれば Slack へ警告して exit 1 で終わる。
# そのまま service の ExecStart にすると、drift があるたびにユニットが
# failed 状態になり、watchdog の is-failed チェックが「警告は既に送った
# drift」と「チェック自体が走らなかった／警告を送れなかった硬い失敗」を
# 区別できない。そこでこのランナーが exit code を正規化する:
#   - チェックが ok（exit 0）→ 0
#   - drift 警告を Slack へ送れた → 0（警告は出ているので systemd の
#     failed にはしない）
#   - 同じ drift で既に警告済み（重複抑止）→ 0（警告は既に出ている）
#   - 警告の送信に失敗した → 1（硬い失敗。watchdog が is-failed で拾う）
#   - チェック自体が異常終了した（exit 0/1 以外、または NG 行なし）→ 1
# --dry-run は何も送らないので、チェックの exit code をそのまま返す。
# チェックの出力はそのまま通す（文言はチェック側が持つ）。
#
# なぜ出力で判定するのか: チェック側の exit code は drift でも警告送信
# 失敗でも同じ 1 なので、出力で区別するしかない。判定が網羅的なのは
# チェックの構造による -- 警告の送信は `if ! slackcli ...` の1か所で、
# 失敗すれば必ず "Slack warn send failed" が stderr に出る（タイムアウト
# や 429 など exit 非0 になる失敗はすべてここを通る）。"NG:" 行は
# チェック自身の末尾出力だけで、失敗内容にユーザー由来のテキストや
# diff は含まれない（警告はファイル名のみ）ため、誤って拾う行はない。
# したがって exit 1 かつ NG 行あり かつ送信失敗なし ＝ 警告は送れた、
# という読みはこのチェックに限って正しい。
set -u
CHECK=${SYNC_CHECK_SCRIPT:-/path/to/slack-watcher/slack-listener-sync-check.sh}
DRY_RUN=0
for a in "$@"; do
  if [ "$a" = "--dry-run" ]; then DRY_RUN=1; fi
done
OUT=$(mktemp)
ERR=$(mktemp)
trap 'rm -f "$OUT" "$ERR"' EXIT
bash "$CHECK" "$@" >"$OUT" 2>"$ERR"
rc=$?
cat "$OUT"
cat "$ERR" >&2
if [ "$DRY_RUN" = 1 ]; then
  exit "$rc"
fi
if [ "$rc" -eq 0 ]; then
  exit 0
fi
if grep -q "Slack warn send failed" "$ERR"; then
  exit 1
fi
if grep -q "still failing (already warned)" "$OUT"; then
  exit 0
fi
if grep -q "^NG:" "$OUT"; then
  exit 0
fi
exit 1
