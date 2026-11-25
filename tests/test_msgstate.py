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
    # Fresh by default (aging, 2026-11-23): an ancient received_at
    # would promote every fixture to the top tier and hide the base
    # tier order. aged_msg() overrides this for age tests.
    return {"event_id": event_id,
            "received_at": datetime.now(timezone.utc).isoformat(),
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

    def count_pending(base, args=None):
        r = run(["count-pending"] + (args or []), base)
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

    # 15. naive な updated_at は UTC として扱う (2026-11-21,
    # Neiro さんの agy レビュー指摘と同型の穴)。_parse_ts が
    # naive を返していた頃は、aware な now との引き算で
    # TypeError になり、sweep ごと pending が死んでいた。
    base17 = make_base([msg("EvNaive1", "1795270000.000001", "hello")])
    mark(base17, "EvNaive1", "processing")
    st = state(base17)
    naive_old = (datetime.now() - timedelta(seconds=1200)).isoformat()
    st["EvNaive1"]["updated_at"] = naive_old  # タイムゾーンなし
    with open(os.path.join(base17, "events", "message_state.json"),
              "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    items = pending(base17)  # sweep が TypeError で死なないこと
    check("naive な updated_at でも sweep が死なない",
          len(items) == 1 and items[0]["key"] == "EvNaive1",
          f"got {items}")
    check("naive な updated_at の stale は pending へ戻る",
          items[0]["status"] == "pending" if items else False)

    # 16. claim総時間の上限 (2026-11-22, touchの穴の修正)。
    # touchだけでは回数を消費しないので、延命し続けるclaimは
    # 600sのsweepにかからず止まらなかった。最初のmark processing
    # から最長30分でstaleとして差し戻し、差し戻しのたびに再claim
    # でattemptsを消費して3回でデッドレター化する。
    def set_claim_old(base, key, total_secs, heartbeat_secs=0):
        """claimed_atをtotal_secs前に、updated_atをheartbeat_secs前に置く。"""
        st = state(base)
        now = datetime.now(timezone.utc)
        st[key]["claimed_at"] = (now - timedelta(seconds=total_secs)).isoformat()
        st[key]["updated_at"] = (now - timedelta(seconds=heartbeat_secs)).isoformat()
        with open(os.path.join(base, "events", "message_state.json"), "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)

    base18 = make_base([msg("EvC1", "1794478306.000051")])
    mark(base18, "EvC1", "processing")
    st = state(base18)["EvC1"]
    check("claim時にclaimed_atが記録される", "claimed_at" in st, f"got {st}")
    claimed = datetime.fromisoformat(st["claimed_at"])
    check("claimed_atはclaim時刻に近い",
          abs((datetime.now(timezone.utc) - claimed).total_seconds()) < 60)
    check("30分以内のtouchはtouchedを返す", touch(base18, "EvC1") == "touched")
    # updated_atは新しくても、claimから30分超ならsweepで差し戻される
    set_claim_old(base18, "EvC1", total_secs=2000, heartbeat_secs=0)
    items = pending(base18)
    check("touch延命でもclaim総時間30分超はpendingへ戻る",
          len(items) == 1 and items[0]["key"] == "EvC1", f"got {items}")
    check("総時間での差し戻しもattemptsは増えない",
          state(base18)["EvC1"]["attempts"] == 1)
    # 30分超のclaimへのtouchは延命しない
    mark(base18, "EvC1", "processing")  # attempts=2, claimed_at更新
    set_claim_old(base18, "EvC1", total_secs=2000, heartbeat_secs=1200)
    old_updated = state(base18)["EvC1"]["updated_at"]
    check("30分超のclaimへのtouchはexpiredを返す",
          touch(base18, "EvC1") == "expired")
    check("expiredのtouchはupdated_atを進めない",
          state(base18)["EvC1"]["updated_at"] == old_updated)

    # claimed_atのない古いエントリ: 初回touchがclaimed_atを刻む
    base19 = make_base([msg("EvC2", "1794478306.000052")])
    mark(base19, "EvC2", "processing")
    st = state(base19)
    st["EvC2"].pop("claimed_at", None)
    with open(os.path.join(base19, "events", "message_state.json"), "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    check("claimed_atなしのtouchはtouchedを返す", touch(base19, "EvC2") == "touched")
    check("touchがclaimed_atを刻む", "claimed_at" in state(base19)["EvC2"])

    # 差し戻し→再claimを繰り返すと3回でデッドレター化する（ループが閉じる）
    base20 = make_base([msg("EvC3", "1794478306.000053")])
    mark(base20, "EvC3", "processing")  # attempts=1
    for _ in range(2):
        set_claim_old(base20, "EvC3", total_secs=2000, heartbeat_secs=0)
        items = pending(base20)  # sweep -> pending
        assert len(items) == 1, items
        mark(base20, "EvC3", "processing")  # attempts 2, then 3
        claimed = datetime.fromisoformat(state(base20)["EvC3"]["claimed_at"])
        assert abs((datetime.now(timezone.utc) - claimed).total_seconds()) < 60
    set_claim_old(base20, "EvC3", total_secs=2000, heartbeat_secs=0)
    items = pending(base20)  # sweep: attempts(3) >= MAX -> dead
    check("claim総時間の差し戻し3回でデッドレター化する",
          items == [] and state(base20)["EvC3"]["status"] == "dead"
          and len(dead_letters(base20)) == 1,
          f"got {items}, {state(base20)['EvC3']}")
    check("デッドレターに総時間超過の理由が残る",
          "claim lifetime exceeded" in (dead_letters(base20)[0].get("last_error") or ""))

    # 17. 終端ガードでの却下は events/mark_rejected.jsonl に警告を残す
    # (2026-11-23, #slack-へようこそ thread 1795420153.646849 での約束)。
    # done/dead への遅れた mark は従来サイレントに返るだけで、遅れた
    # 書き込みが終端状態と競合した痕跡がどこにも残らなかった。
    def mark_rejected(base):
        path = os.path.join(base, "events", "mark_rejected.jsonl")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    base21 = make_base([msg("EvR1", "1794478306.000061")])
    check("通常の遷移では却下ログは作られない",
          mark_rejected(base21) == []
          and mark(base21, "EvR1", "processing") == "processing"
          and mark_rejected(base21) == []
          and mark(base21, "EvR1", "failed", "boom") == "failed"
          and mark_rejected(base21) == []
          and mark(base21, "EvR1", "done") == "done"
          and mark_rejected(base21) == [])
    check("doneへの遅れたmarkはdoneを返す",
          mark(base21, "EvR1", "processing") == "done")
    rej = mark_rejected(base21)
    check("doneへの遅れたmarkは却下ログがちょうど1行",
          len(rej) == 1, f"got {rej}")
    check("却下ログに時刻・キー・遷移・現状態・理由が残る",
          rej and rej[0]["key"] == "EvR1"
          and rej[0]["attempted_status"] == "processing"
          and rej[0]["current_status"] == "done"
          and "terminal state done" in (rej[0].get("reason") or "")
          and rej[0].get("at"), f"got {rej}")
    check("doneへの遅れたmarkは状態を変えない",
          state(base21)["EvR1"]["status"] == "done")
    check("doneへの再doneは却下ではなく通常遷移（ログは増えない）",
          mark(base21, "EvR1", "done") == "done"
          and len(mark_rejected(base21)) == 1)

    base22 = make_base([msg("EvR2", "1794478306.000062")])
    mark(base22, "EvR2", "processing")  # attempts 1
    mark(base22, "EvR2", "failed", "x")
    mark(base22, "EvR2", "processing")  # attempts 2
    mark(base22, "EvR2", "failed", "y")
    mark(base22, "EvR2", "processing")  # attempts 3
    mark(base22, "EvR2", "processing")  # attempts 4 > MAX -> dead
    check("dead化自体は却下ログを作らない",
          mark_rejected(base22) == [])
    check("deadへの遅れたmarkはdeadを返す",
          mark(base22, "EvR2", "done") == "dead")
    rej = mark_rejected(base22)
    check("deadへの遅れたmarkは却下ログがちょうど1行",
          len(rej) == 1, f"got {rej}")
    check("deadの却下ログに現状態deadが残る",
          rej and rej[0]["key"] == "EvR2"
          and rej[0]["attempted_status"] == "done"
          and rej[0]["current_status"] == "dead"
          and "terminal state dead" in (rej[0].get("reason") or ""),
          f"got {rej}")
    check("deadへの遅れたmarkは状態を変えない",
          state(base22)["EvR2"]["status"] == "dead")
    # 却下は毎回記録される（重複排除しない）
    mark(base22, "EvR2", "failed", "late again")
    check("2回目の却下も記録される",
          len(mark_rejected(base22)) == 2, f"got {mark_rejected(base22)}")

    # ログ書き込みが失敗してもmark自体は壊れない（fail-open）
    base23 = make_base([msg("EvR3", "1794478306.000063")])
    mark(base23, "EvR3", "processing")
    mark(base23, "EvR3", "done")
    os.makedirs(os.path.join(base23, "events", "mark_rejected.jsonl"),
                exist_ok=True)  # ログパスをディレクトリにして書き込み不能に
    check("ログが書けなくても却下markはdoneを返す",
          mark(base23, "EvR3", "processing") == "done")
    check("ログが書けなくても状態はdoneのまま",
          state(base23)["EvR3"]["status"] == "done")

    # 18. 却下ログのローテーション（Neiro さんのアーカイブ設計の取り込み）
    # 5MB上限・1世代だけ残す。書き込み失敗時は配送ならぬ mark を止めない。
    def mark_rejected_rotated(base):
        path = os.path.join(base, "events", "mark_rejected.jsonl.1")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    base24 = make_base([msg("EvR4", "1794478306.000064")])
    mark(base24, "EvR4", "processing")
    mark(base24, "EvR4", "done")
    # 上限ちょうどまで旧ログを膨らませてから却下を1回起こす
    log24 = os.path.join(base24, "events", "mark_rejected.jsonl")
    with open(log24, "w", encoding="utf-8") as f:
        f.write(json.dumps({"seed": "old"}, ensure_ascii=False) + "\n")
        f.write(json.dumps({"pad": "x" * (5 * 1024 * 1024)},
                           ensure_ascii=False) + "\n")
    check("上限超過時の却下markはdoneを返す",
          mark(base24, "EvR4", "processing") == "done")
    check("上限超過で旧ログが.1へローテーションする",
          len(mark_rejected_rotated(base24)) == 2
          and mark_rejected_rotated(base24)[0].get("seed") == "old",
          f"got {len(mark_rejected_rotated(base24))} lines")
    check("ローテーション後の現行ログは新しい却下1行だけ",
          len(mark_rejected(base24)) == 1
          and mark_rejected(base24)[0]["key"] == "EvR4",
          f"got {mark_rejected(base24)}")
    # 上限未満ではローテーションしない
    base25 = make_base([msg("EvR5", "1794478306.000065")])
    mark(base25, "EvR5", "processing")
    mark(base25, "EvR5", "done")
    mark(base25, "EvR5", "processing")  # 却下1行・小さいログ
    check("上限未満では.1は作られない",
          mark_rejected_rotated(base25) == []
          and len(mark_rejected(base25)) == 1)
    # ローテーションに失敗しても却下markはdoneを返す（fail-open）
    base26 = make_base([msg("EvR6", "1794478306.000066")])
    mark(base26, "EvR6", "processing")
    mark(base26, "EvR6", "done")
    log26 = os.path.join(base26, "events", "mark_rejected.jsonl")
    with open(log26, "w", encoding="utf-8") as f:
        f.write(json.dumps({"pad": "x" * (5 * 1024 * 1024)},
                           ensure_ascii=False) + "\n")
    os.makedirs(os.path.join(base26, "events", "mark_rejected.jsonl.1"),
                exist_ok=True)  # .1をディレクトリにして削除不能に
    check("ローテーション失敗でも却下markはdoneを返す",
          mark(base26, "EvR6", "processing") == "done")
    check("ローテーション失敗でも状態はdoneのまま",
          state(base26)["EvR6"]["status"] == "done")
    check("ローテーション失敗でも新しい却下は追記される",
          len(mark_rejected(base26)) == 2
          and mark_rejected(base26)[-1]["key"] == "EvR6",
          f"got {len(mark_rejected(base26))} lines")

    # 19. キューの aging (2026-11-23, Neiro さんの実績値の取り込み)。
    # 5分超で1段上がり、15分超で最上位へ。ティア内は古い順。
    # 2026-11-23 の実例: ティア2が宛て投稿の流入で約22分後回しに
    # され続けた (starvation)。Yama さんは FIFO で不要、Neiro さん
    # は 2026-11-21 からこの値で運用中で後回しは出ていない。
    # しきい値は三者統一不要。
    def aged2(event_id, ts, text, seconds_ago, channel_type="channel"):
        m = msg2(event_id, ts, text, channel_type)
        m["received_at"] = (datetime.now(timezone.utc)
                            - timedelta(seconds=seconds_ago)).isoformat()
        return m

    base27 = make_base([
        msg2("EvA1", "1794478306.000071", "雑談です"),
        msg2("EvA2", "1794478306.000072", "やなやな、教えて"),
        msg2("EvA3", "1794478306.000073", "DMです", "im"),
    ])
    items = pending(base27)
    check("新しいものはティア順のまま",
          [it["key"] for it in items] == ["EvA3", "EvA2", "EvA1"],
          f"got {[it['key'] for it in items]}")

    # 5分超のティア2は1段上がり、新しいティア1より先に来る
    base28 = make_base([
        aged2("EvB2", "1794478306.000081", "雑談です", 400),
        msg2("EvB1", "1794478306.000082", "やなやな、教えて"),
        msg2("EvB3", "1794478306.000083", "雑談です"),
    ])
    items = pending(base28)
    check("5分超のティア2は1段上がって新しいティア1より先に来る",
          [it["key"] for it in items] == ["EvB2", "EvB1", "EvB3"],
          f"got {[it['key'] for it in items]}")
    # 5分未満では上がらない
    base29 = make_base([
        aged2("EvC2", "1794478306.000091", "雑談です", 240),
        msg2("EvC1", "1794478306.000092", "やなやな、教えて"),
    ])
    items = pending(base29)
    check("5分未満のティア2は上がらない",
          [it["key"] for it in items] == ["EvC1", "EvC2"],
          f"got {[it['key'] for it in items]}")

    # 15分超のティア2は最上位へ。DMと同ティアになり、古い順で
    # DMより先に来る
    base30 = make_base([
        aged2("EvD3", "1794478306.000101", "雑談です", 1000),
        msg2("EvD1", "1794478306.000102", "DMです", "im"),
        msg2("EvD2", "1794478306.000103", "やなやな、教えて"),
    ])
    items = pending(base30)
    check("15分超のティア2は最上位へ上がりDMより先に来る",
          [it["key"] for it in items] == ["EvD3", "EvD1", "EvD2"],
          f"got {[it['key'] for it in items]}")
    # 15分未満では最上位までは上がらない（1段だけ）
    base31 = make_base([
        aged2("EvE3", "1794478306.000111", "雑談です", 840),
        msg2("EvE1", "1794478306.000112", "DMです", "im"),
        msg2("EvE2", "1794478306.000113", "やなやな、教えて"),
    ])
    items = pending(base31)
    check("15分未満のティア2は1段だけ上がる（DMの後ろ）",
          [it["key"] for it in items] == ["EvE1", "EvE3", "EvE2"],
          f"got {[it['key'] for it in items]}")

    # aging は processing / dead の扱いを変えない
    base32 = make_base([
        aged2("EvF1", "1794478306.000121", "雑談です", 1000),
    ])
    mark(base32, "EvF1", "processing")
    check("古い処理中のものは aging でも pending に出ない",
          pending(base32) == [])
    mark(base32, "EvF1", "done")
    check("古い done は aging でも pending に出ない",
          pending(base32) == [])
    base33 = make_base([
        aged2("EvG1", "1794478306.000131", "雑談です", 1000),
    ])
    mark(base33, "EvG1", "processing")
    mark(base33, "EvG1", "failed", "boom")
    mark(base33, "EvG1", "processing")
    mark(base33, "EvG1", "failed", "boom2")
    mark(base33, "EvG1", "processing")
    mark(base33, "EvG1", "processing")  # attempts 4 > MAX -> dead
    check("古い dead は aging でも pending に出ない",
          pending(base33) == []
          and state(base33)["EvG1"]["status"] == "dead")
    # received_at が無ければ上がらず、落ちもしない
    base34 = make_base([
        msg2("EvH2", "1794478306.000141", "雑談です"),
        msg2("EvH1", "1794478306.000142", "やなやな、教えて"),
    ])
    with open(os.path.join(base34, "events", "inbox.jsonl"),
              encoding="utf-8") as f:
        rows = [json.loads(line) for line in f if line.strip()]
    rows[0].pop("received_at", None)
    with open(os.path.join(base34, "events", "inbox.jsonl"),
              "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    items = pending(base34)
    check("received_at が無ければ上がらない（落ちない）",
          [it["key"] for it in items] == ["EvH1", "EvH2"],
          f"got {[it['key'] for it in items]}")

    # 35. count-pending --json（2026-11-24、Neiro の滞留警告の処理中件数
    # 表示にならった watchdog 改善）：plain は従来どおり未処理数だけ、
    # --json は未処理＋処理中を1スナップショットで返す。
    base35 = make_base([
        msg("EvJ1", "1794478306.000151", "未処理のまま"),
        msg("EvJ2", "1794478306.000152", "処理中"),
        msg("EvJ3", "1794478306.000153", "済み"),
    ])
    mark(base35, "EvJ2", "processing")
    mark(base35, "EvJ3", "processing")
    mark(base35, "EvJ3", "done")
    check("count-pending は未処理数だけを返す（後方互換）",
          count_pending(base35) == 1)
    counts = json.loads(run(["count-pending", "--json"], base35).stdout.strip())
    check("count-pending --json は未処理＋処理中を返す",
          counts == {"pending": 1, "processing": 1, "bot_pending": 0}, f"got {counts}")
    # stale な処理中は sweep で未処理に戻り、処理中には数えない
    base36 = make_base([msg("EvK1", "1794478306.000161", "古い処理中")])
    mark(base36, "EvK1", "processing")
    set_stale(base36, "EvK1", 1)
    counts = json.loads(run(["count-pending", "--json"], base36).stdout.strip())
    check("stale 処理中は sweep 後に未処理として数える",
          counts == {"pending": 1, "processing": 0, "bot_pending": 0}, f"got {counts}")

    # 36. 二段構えの滞留警告（2026-11-25、Neiro のコミット 865c873
    # の取り入れ）：oldest-pending-age --json は人・ボット別に最古の
    # 経過秒と、全体の最古がどちらかを返す。count-pending --json は
    # ボット分の未処理数 bot_pending を追加で返す。判定はイベント
    # 自身の印だけ（bot_id あればボット、無ければ人間）で、IDの
    # ハードコードはしない。素の oldest-pending-age は従来どおり
    # 全体の最古を1数値で返す。
    def oldest_json(base):
        r = run(["oldest-pending-age", "--json"], base)
        assert r.returncode == 0, r.stderr
        return json.loads(r.stdout)

    def bot_msg(event_id, seconds_ago, ts):
        m = aged_msg(event_id, seconds_ago, ts)
        m["bot_id"] = "B0EXAMPLEBOT"
        return m

    base37 = make_base([aged_msg("EvH", 200, "1794478306.000171"),
                        bot_msg("EvB", 400, "1794478306.000172")])
    ages = oldest_json(base37)
    check("oldest --json は人・ボット別の最古を返す",
          ages["human"] is not None and 190 <= ages["human"] <= 220
          and ages["bot"] is not None and 390 <= ages["bot"] <= 420,
          f"got {ages}")
    check("oldest --json は全体の最古の種別を返す",
          ages["oldest_author_kind"] == "bot", f"got {ages}")
    check("素の oldest-pending-age は全体の最古を返す",
          oldest_age(base37) is not None
          and 390 <= oldest_age(base37) <= 420)
    counts = json.loads(run(["count-pending", "--json"], base37).stdout.strip())
    check("count-pending --json はボット分の未処理数を返す",
          counts == {"pending": 2, "processing": 0, "bot_pending": 1},
          f"got {counts}")
    base38 = make_base([bot_msg("EvB2", 200, "1794478306.000181")])
    ages = oldest_json(base38)
    check("ボットだけなら人側は null・最古はボット",
          ages == {"human": None, "bot": ages["bot"],
                   "oldest_author_kind": "bot"}
          and ages["bot"] is not None and 190 <= ages["bot"] <= 220,
          f"got {ages}")
    base39 = make_base([])
    check("未処理が無ければ --json は全て null",
          oldest_json(base39) == {"human": None, "bot": None,
                                   "oldest_author_kind": None},
          f"got {oldest_json(base39)}")
    # 処理中のボットは滞留に数えない（人と同じ）
    base40 = make_base([bot_msg("EvB3", 200, "1794478306.000191")])
    mark(base40, "EvB3", "processing")
    check("処理中のボットは --json の滞留に数えない",
          oldest_json(base40) == {"human": None, "bot": None,
                                   "oldest_author_kind": None})

    print(f"\n{CHECKS[0]} passed, {CHECKS[1]} failed, "
          f"{CHECKS[0] + CHECKS[1]} checks total")
    return 0 if CHECKS[1] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
