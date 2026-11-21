#!/usr/bin/env python3
"""E2E tests for msgstate.py (per-message processing state).

Modeled on Neiro's ~/hooks/tests/test_slack_poll.py (shared in
#slack-へようこそ 2026-11-12): same checklist, adapted to our
msgstate semantics -- pending/processing/done/failed/dead with
flock-serialized claims and a 600s stale-processing sweep, plus
Neiro's skipped-row counting (2026-11-12).

Plain stdlib script, no pytest (Yama's style). Run:
  python3 tests/test_msgstate.py
Exit 0 when every check passes; each check prints PASS/FAIL.

All state lives in a temp dir via SLACK_LISTENER_BASE, so the real
events/ directory is never touched.
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MSGSTATE = os.path.join(ROOT, "msgstate.py")

CHECKS = [0, 0]  # [passed, failed]


def check(name, cond, detail=""):
    CHECKS[0 if cond else 1] += 1
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not cond else ""))
    return cond


def run(args, base):
    env = dict(os.environ, SLACK_LISTENER_BASE=base)
    return subprocess.run(
        [sys.executable, MSGSTATE] + args,
        capture_output=True, text=True, env=env, timeout=30)


def pending(base):
    r = run(["pending"], base)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def oldest_age(base):
    r = run(["oldest-pending-age"], base)
    assert r.returncode == 0, r.stderr
    out = r.stdout.strip()
    return int(out) if out else None


def mark(base, key, status, error=None):
    args = ["mark", key, status]
    if error is not None:
        args += ["--error", error]
    r = run(args, base)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def state(base):
    with open(os.path.join(base, "events", "message_state.json"), encoding="utf-8") as f:
        return json.load(f)


def dead_letters(base):
    path = os.path.join(base, "events", "dead_letter.jsonl")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def reconcile_done(base, dry_run=False):
    args = ["reconcile-done"]
    if dry_run:
        args.append("--dry-run")
    r = run(args, base)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def inbox_stats(base):
    path = os.path.join(base, "events", "inbox_stats.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def make_base(messages):
    base = tempfile.mkdtemp(prefix="msgstate-test-")
    os.makedirs(os.path.join(base, "events"), exist_ok=True)
    with open(os.path.join(base, "events", "inbox.jsonl"), "w", encoding="utf-8") as f:
        for m in messages:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")
    return base


def msg(event_id="EvTest1", ts="1794478306.000001", text="hello"):
    return {"event_id": event_id, "received_at": "2026-11-12T10:00:00+00:00",
            "channel": "C0EXAMPLE001", "channel_type": "channel",
            "user": "U0EXAMPLEUSER", "text": text, "ts": ts, "thread_ts": None}


def set_stale(base, key, attempts):
    """Force an entry into stale processing (updated_at older than 600s)."""
    st = state(base)
    old = (datetime.now(timezone.utc) - timedelta(seconds=1200)).isoformat()
    st[key] = {"status": "processing", "attempts": attempts, "updated_at": old}
    with open(os.path.join(base, "events", "message_state.json"), "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


def main():
    # 1. 新規イベントのwake
    base = make_base([msg()])
    items = pending(base)
    check("新規イベントがpendingでwakeする", len(items) == 1 and items[0]["key"] == "EvTest1"
          and items[0]["status"] == "pending" and items[0]["attempts"] == 0,
          f"got {items}")
    check("pendingはメッセージ本文を保持する", items[0]["message"]["text"] == "hello")

    # 2. 二重wakeなし（claim後はpendingに出ない）
    check("claimはprocessingを返す", mark(base, "EvTest1", "processing") == "processing")
    check("claim後はpendingが空", pending(base) == [])
    check("attemptsはclaimでちょうど1", state(base)["EvTest1"]["attempts"] == 1)

    # 3. stale再実行でattemptsが1→2ちょうど1回だけ増える
    set_stale(base, "EvTest1", 1)
    items = pending(base)  # sweep: processing -> pending
    check("staleはpendingへ戻る", len(items) == 1 and items[0]["status"] == "pending")
    check("sweepでattemptsは増えない", state(base)["EvTest1"]["attempts"] == 1)
    check("再claimはprocessingを返す", mark(base, "EvTest1", "processing") == "processing")
    check("stale再実行でattemptsが1→2ちょうど1回だけ増える",
          state(base)["EvTest1"]["attempts"] == 2)

    # 4. 上限到達でdead＋デッドレター1行
    base2 = make_base([msg()])
    mark(base2, "EvTest1", "processing")   # attempts 1
    mark(base2, "EvTest1", "failed", "boom1")
    mark(base2, "EvTest1", "processing")   # attempts 2
    mark(base2, "EvTest1", "failed", "boom2")
    mark(base2, "EvTest1", "processing")   # attempts 3
    check("3回目のclaimまではprocessing", state(base2)["EvTest1"]["attempts"] == 3)
    out = mark(base2, "EvTest1", "processing")  # attempts 4 > MAX -> dead
    check("上限到達でdeadを返す", out == "dead", f"got {out!r}")
    check("上限到達でstatusがdead", state(base2)["EvTest1"]["status"] == "dead")
    dl = dead_letters(base2)
    check("上限到達でデッドレターがちょうど1行", len(dl) == 1, f"got {len(dl)}")
    check("デッドレターに元メッセージが残る", dl[0]["message"]["event_id"] == "EvTest1")
    check("deadはpendingに出ない", pending(base2) == [])

    # 5. failedのリトライとdead（failedはpendingに再掲される）
    base3 = make_base([msg()])
    mark(base3, "EvTest1", "processing")
    mark(base3, "EvTest1", "failed", "boom")
    items = pending(base3)
    check("failedはpendingに再掲される", len(items) == 1 and items[0]["status"] == "failed")
    check("failedのまま再claimできる", mark(base3, "EvTest1", "processing") == "processing")
    # deadは終端：その後のmarkは蘇生しない
    mark(base3, "EvTest1", "failed", "x")
    mark(base3, "EvTest1", "processing")  # attempts 3
    mark(base3, "EvTest1", "processing")  # -> dead
    check("dead後のmarkはdeadを返し蘇生しない",
          mark(base3, "EvTest1", "done") == "dead")
    check("dead後のstatusはdeadのまま", state(base3)["EvTest1"]["status"] == "dead")

    # 6. doneの再wakeなし
    base4 = make_base([msg()])
    mark(base4, "EvTest1", "processing")
    mark(base4, "EvTest1", "done")
    check("doneはpendingに出ない（再wakeなし）", pending(base4) == [])
    check("doneは2回目のpendingでも出ない", pending(base4) == [])

    # 7. 不正行のスキップ（Neiro提案のスキップ件数カウント付き）
    base5 = make_base([msg()])
    with open(os.path.join(base5, "events", "inbox.jsonl"), "a", encoding="utf-8") as f:
        f.write("this is not json\n\n{\"event_id\": 123, broken\n")
    items = pending(base5)
    check("不正行をスキップして正常行だけwakeする",
          len(items) == 1 and items[0]["key"] == "EvTest1", f"got {items}")
    st = inbox_stats(base5)
    check("スキップ件数がカウントされる（空行1・不正行2）",
          st["skipped_blank"] == 1 and st["skipped_malformed"] == 2, f"got {st}")
    with open(os.path.join(base5, "events", "inbox_stats.json"), encoding="utf-8") as f:
        raw_stats = f.read()
    check("統計ファイルに壊れた行の内容は残らない",
          "this is not json" not in raw_stats and "broken" not in raw_stats)
    pending(base5)  # 再読してもスナップショットなので件数は増えない
    st2 = inbox_stats(base5)
    check("再読してもスキップ件数は増えない（累積ではない）",
          st2["skipped_blank"] == 1 and st2["skipped_malformed"] == 2, f"got {st2}")

    # 8. flock付き10並行ポーリングでwakeがちょうど1件＋claimの更新ロストなし
    base6 = make_base([msg()])
    results = []
    errors = []

    def poll_once():
        try:
            results.append(pending(base6))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=poll_once) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("10並行ポーリングが全て成功", not errors and len(results) == 10, f"{errors}")
    check("10並行ポーリングでwakeがちょうど1件ずつ",
          all(len(r) == 1 and r[0]["key"] == "EvTest1" for r in results))
    # 10並行claim：flock直列化でattemptsがちょうど10まで増え、更新ロストなし
    outs = []
    lock = threading.Lock()

    def claim_once():
        o = mark(base6, "EvTest1", "processing")
        with lock:
            outs.append(o)

    threads = [threading.Thread(target=claim_once) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("10並行claimでattemptsがちょうど4で止まる（更新ロストなし）",
          state(base6)["EvTest1"]["attempts"] == 4,
          f"got {state(base6)['EvTest1']['attempts']}")
    check("10並行claimの内訳はprocessing 3回・dead 7回",
          outs.count("processing") == 3 and outs.count("dead") == 7, f"got {outs}")

    # 9. done確定でlast_errorが消える＋reconcile-done（Neiro共有の採用）
    base7 = make_base([msg()])
    mark(base7, "EvTest1", "processing")
    mark(base7, "EvTest1", "failed", "max attempts exceeded")
    check("failedでlast_errorが残る",
          state(base7)["EvTest1"].get("last_error") == "max attempts exceeded")
    mark(base7, "EvTest1", "done")
    check("done確定でlast_errorが消える",
          "last_error" not in state(base7)["EvTest1"])
    # 明示的に --error 付きで done にした場合は残る
    mark(base7, "EvTest1", "failed", "boom again")
    mark(base7, "EvTest1", "done", "explicit note")
    check("明示 --error 付きのdoneはlast_errorを残す",
          state(base7)["EvTest1"].get("last_error") == "explicit note")
    # reconcile-done: 修正より前の残りを網羅的に拾う（中身を問わない）
    st = state(base7)
    st["EvTest1"]["last_error"] = "stale from before the fix"
    with open(os.path.join(base7, "events", "message_state.json"), "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
    out = reconcile_done(base7, dry_run=True)
    check("reconcile-done --dry-runは件数を数えるだけ",
          "cleared: 1 (dry_run=True)" in out, f"got {out!r}")
    check("dry-runではlast_errorが残る",
          state(base7)["EvTest1"].get("last_error") == "stale from before the fix")
    out = reconcile_done(base7)
    check("reconcile-doneは古いlast_errorを消す",
          "cleared: 1 (dry_run=False)" in out, f"got {out!r}")
    check("reconcile-done後last_errorが消える",
          "last_error" not in state(base7)["EvTest1"])
    check("reconcile-doneでレコード自体は残る",
          state(base7)["EvTest1"]["status"] == "done")

    # 10. oldest-pending-age（watchdogの滞留警告用、agyレビュー 2026-11-18）
    def aged_msg(event_id, seconds_ago, ts):
        m = msg(event_id=event_id, ts=ts)
        m["received_at"] = (datetime.now(timezone.utc)
                            - timedelta(seconds=seconds_ago)).isoformat()
        return m

    base8 = make_base([])
    check("未処理が無ければoldest-pending-ageは空",
          oldest_age(base8) is None)
    base9 = make_base([aged_msg("EvOld", 200, "1794478306.000001")])
    age = oldest_age(base9)
    check("最古の未処理からの経過秒を返す",
          age is not None and 190 <= age <= 220, f"got {age}")
    base10 = make_base([aged_msg("EvOld", 200, "1794478306.000001"),
                        aged_msg("EvNew", 30, "1794478306.000002")])
    age = oldest_age(base10)
    check("複数あれば最古のものを測る",
          age is not None and 190 <= age <= 220, f"got {age}")
    base11 = make_base([aged_msg("EvProc", 200, "1794478306.000001")])
    mark(base11, "EvProc", "processing")
    check("処理中のものは滞留に数えない",
          oldest_age(base11) is None)
    base12 = make_base([aged_msg("EvStale", 200, "1794478306.000001")])
    mark(base12, "EvStale", "processing")
    set_stale(base12, "EvStale", 1)
    age = oldest_age(base12)
    check("staleな処理中はpendingへ戻してから測る",
          age is not None and 190 <= age <= 220, f"got {age}")

    # 13. バースト対策 (2026-11-21): pending の既定上限と touch。
    # 1回の wake がバースト全件を抱えると、その run が死んだときに
    # claim がまとめて stale 化し attempt を浪費する。上限で run を
    # 小さく保ち、残りは次の poll で再 wake させる。
    def pending_raw(base, args):
        r = run(["pending"] + args, base)
        assert r.returncode == 0, r.stderr
        return json.loads(r.stdout)

    def count_pending(base):
        r = run(["count-pending"], base)
        assert r.returncode == 0, r.stderr
        return int(r.stdout.strip())

    def touch(base, key):
        r = run(["touch", key], base)
        assert r.returncode == 0, r.stderr
        return r.stdout.strip()

    base13 = make_base([msg(f"EvB{i}", f"1794478306.00001{i}")
                        for i in range(1, 6)])
    items = pending(base13)
    check("バースト時もpendingは既定で3件まで", len(items) == 3,
          f"got {len(items)}")
    check("上限内は古い順に返る",
          [it["key"] for it in items] == ["EvB1", "EvB2", "EvB3"])
    check("--allで全件返る", len(pending_raw(base13, ["--all"])) == 5)
    check("--limit 2で2件返る",
          len(pending_raw(base13, ["--limit", "2"])) == 2)
    check("count-pendingは上限の影響を受けない",
          count_pending(base13) == 5)
    mark(base13, "EvB1", "done")
    items = pending(base13)
    check("先頭がdoneになると上限枠が次へずれる",
          [it["key"] for it in items] == ["EvB2", "EvB3", "EvB4"])

    base14 = make_base([msg("EvT1", "1794478306.000021")])
    mark(base14, "EvT1", "processing")
    set_stale(base14, "EvT1", 1)  # processing, attempts=1, updated 1200s ago
    check("touchはtouchedを返す", touch(base14, "EvT1") == "touched")
    st = state(base14)["EvT1"]
    check("touchでattemptsは増えない", st["attempts"] == 1)
    updated = datetime.fromisoformat(st["updated_at"])
    check("touchでupdated_atが新しくなる",
          (datetime.now(timezone.utc) - updated).total_seconds() < 60)
    check("touch後はsweepで差し戻されない", pending(base14) == [])
    mark(base14, "EvT1", "done")
    check("doneへのtouchはdoneを返し状態を変えない",
          touch(base14, "EvT1") == "done"
          and state(base14)["EvT1"]["status"] == "done")
    check("不在キーへのtouchはabsentを返す",
          touch(base14, "EvNope") == "absent")
    check("不在キーへのtouchは状態を作らない",
          "EvNope" not in state(base14))

    # 14. 優先順位 (2026-11-21, Neiroさんのバースト対策の取り込み)。
    # DM > Yanayana/あた宛て > その他、各ティア内は古い順。バースト時
    # に宛てられた投稿が議論の後ろで待たされ続けないための順序で、
    # 上限の枠はこの順序の先頭から埋まる。
    def msg2(event_id, ts, text, channel_type="channel"):
        m = msg(event_id, ts, text)
        m["channel_type"] = channel_type
        return m

    base15 = make_base([
        msg2("EvP1", "1794478306.000031", "雑談です"),
        msg2("EvP2", "1794478306.000032", "やなやな、教えて"),
        msg2("EvP3", "1794478306.000033", "Neiroさんの共有です"),
        msg2("EvP4", "1794478306.000034", "DMです", "im"),
        msg2("EvP5", "1794478306.000035", "あた宛て <@U0EXAMPLEOWNER> どう思う？"),
        msg2("EvP6", "1794478306.000036", "Hey Yanayana, one question"),
    ])
    items = pending(base15)
    check("バースト時はDM・宛てられた投稿が上限枠に先に入る",
          [it["key"] for it in items] == ["EvP4", "EvP2", "EvP5"],
          f"got {[it['key'] for it in items]}")
    check("--allはティア順・ティア内は古い順",
          [it["key"] for it in pending_raw(base15, ["--all"])]
          == ["EvP4", "EvP2", "EvP5", "EvP6", "EvP1", "EvP3"])
    check("count-pendingは順序の影響を受けない",
          count_pending(base15) == 6)
    mark(base15, "EvP4", "done")
    items = pending(base15)
    check("DMがdoneになると次のティアが枠に入る",
          [it["key"] for it in items] == ["EvP2", "EvP5", "EvP6"])

    base16 = make_base([
        msg2("EvQ1", "1794478306.000041", "雑談です"),
        msg2("EvQ2", "1794478306.000042", "確認です <@U0EXAMPLEBOT>"),
    ])
    items = pending(base16)
    check("メンション <@Yanayana> も宛てられた扱い",
          [it["key"] for it in items] == ["EvQ2", "EvQ1"],
          f"got {[it['key'] for it in items]}")

    print(f"\n{CHECKS[0]} passed, {CHECKS[1]} failed, "
          f"{CHECKS[0] + CHECKS[1]} checks total")
    return 0 if CHECKS[1] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
