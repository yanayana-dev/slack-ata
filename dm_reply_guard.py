#!/usr/bin/env python3
"""DM代行の連続返信ガード (dm_reply_guard.py).

Yama さんの DM 代行の安全弁（「自動返信が3回続いたら止める」）の
共有を受けて、うちの DM 代行にも同じ考え方を入れたもの。うちの
代行返信はフックのワーカーが1件ずつ判断して軽く返すだけで自動で
エコーする仕組みではないが、相手が返してくるたびに新しい DM と
して起きるので、理屈の上では往復が続く余地がある。そのブレーキ
として、会話ごとに連続代行回数を数え、上限に達したら自動返信を
止めて あた に報告する。

使い方（dm-inbox フックのワーカーが使う）:
  python3 dm_reply_guard.py check --channel <conversation>
      送信してよいか判定する。ALLOW なら送信可、STOP なら送信せず
      DM を あた への報告に回す。終了コード 0 = ALLOW、1 = STOP。
  python3 dm_reply_guard.py record --channel <conversation> --ts <sent ts>
      送信が成功した後にだけ呼ぶ（Yama さんの直しと同じく、
      「送信と同時に代行の記録を残す」）。成功前に呼んではいけない。
  python3 dm_reply_guard.py reset --channel <conversation>
      手動の逃げ道。連続回数を 0 に戻す。

判定のしかた:
  会話の履歴（直近10件）を読み、末尾から「代行の送信」を数える。
  代行の送信とは、送信者 user が あた (ATA_USER) で、かつ ts が
  record で残したものと一致するもの。相手のメッセージは数えず、
  飛ばす。あた本人のメッセージ（record にない あた の発信）に
  当たったらそこで連鎖は切れる（本人が参加したら回数は 0 に戻る）。
  履歴が読めないときは fail closed で STOP にする。DM 自体は
  あた への報告に回るので、返信が漏れることはない。

  注意: record を呼び忘れた代行送信は、あた本人の発信と区別が
  つかず、連鎖を切る（回数を 0 に戻す）方向に働く。なので record
  は送信成功の直後に必ず呼ぶこと。

状態: events/dm_reply_guard.json -- 会話 ID ごとに
{"count": <連続代行回数>, "proxy_ts": [<送信した ts ...>]}。
flock (events/dm_reply_guard.lock) と tmp+rename で保存する
(dm_poll.py / msgstate.py と同じ形)。

Helper 解決: --helper > $DM_REPLY_GUARD_HELPER > 既定の
custom.slack-user-token ヘルパー。トークンはこのスクリプトを
通らない（ヘルパーが connector の surrogate exchange を持つ）。
"""
import fcntl
import json
import os
import subprocess
import sys
from contextlib import contextmanager

BASE = os.environ.get("SLACK_LISTENER_BASE") or os.path.dirname(os.path.abspath(__file__))
EVENTS = os.path.join(BASE, "events")
GUARD_STATE = os.path.join(EVENTS, "dm_reply_guard.json")
GUARD_LOCK = os.path.join(EVENTS, "dm_reply_guard.lock")

DEFAULT_HELPER = os.path.expanduser(
    "/path/to/slack-user-helper/slack_user_api.py")

ATA_USER = "U0EXAMPLEOWNER"  # あた's own Slack user id in muse取締役会

LIMIT = 3            # 連続代行回数の上限。ここに達したら自動返信を止める
HISTORY_LIMIT = 10   # check で読む履歴の件数
PROXY_TS_KEEP = 10   # 会話ごとに残す proxy ts の上限
HELPER_TIMEOUT = 90


@contextmanager
def guard_lock():
    """Hold the exclusive flock for one state read-modify-write."""
    os.makedirs(EVENTS, exist_ok=True)
    fd = os.open(GUARD_LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def load_guard_state():
    try:
        with open(GUARD_STATE, "r", encoding="utf-8") as f:
            state = json.load(f)
        return state if isinstance(state, dict) else {}
    except Exception:
        return {}


def save_guard_state(state):
    os.makedirs(EVENTS, exist_ok=True)
    tmp = GUARD_STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, GUARD_STATE)


def _ts(msg):
    try:
        return float(msg.get("ts") or 0)
    except (TypeError, ValueError):
        return 0.0


def consecutive_proxy(messages, proxy_ts, ata_user=ATA_USER):
    """代行送信が何回続いているかを履歴から数える。

    末尾（新しい方）から見て、user が あた で ts が proxy_ts に
    あるものを数える。相手のメッセージは飛ばす。あた本人の
    メッセージ（record にない あた の発信）に当たったら連鎖は
    切れるので、そこで止める。
    """
    known = {str(t) for t in (proxy_ts or [])}
    count = 0
    for msg in sorted(messages, key=_ts, reverse=True):
        if msg.get("user") != ata_user:
            continue  # 相手のメッセージは連鎖を切らない
        if str(msg.get("ts")) in known:
            count += 1
        else:
            break  # あた本人が書いた: 連鎖はここで切れる
    return count


def run_helper(helper, args):
    """Run one helper subcommand and return its parsed JSON stdout."""
    r = subprocess.run([sys.executable, helper] + args,
                       capture_output=True, text=True, timeout=HELPER_TIMEOUT)
    if r.returncode != 0:
        raise RuntimeError(
            f"helper {' '.join(args[:1])} exited {r.returncode}: "
            f"{r.stderr.strip()[:200]}")
    try:
        data = json.loads(r.stdout)
    except Exception:
        raise RuntimeError(
            f"helper {' '.join(args[:1])} gave non-JSON output: "
            f"{r.stdout.strip()[:200]}")
    if isinstance(data, dict) and data.get("ok") is False:
        raise RuntimeError(
            f"helper {' '.join(args[:1])} ok:false: {data.get('error')}")
    return data


def fetch_history(helper, channel):
    data = run_helper(helper, ["history", "--channel", channel,
                               "--limit", str(HISTORY_LIMIT)])
    return data.get("messages", [])


def cmd_check(helper, channel):
    """ALLOW/STOP を判定する。終了コード 0 = ALLOW、1 = STOP。"""
    with guard_lock():
        state = load_guard_state()
        entry = state.get(channel) or {}
        try:
            messages = fetch_history(helper, channel)
        except Exception as e:
            print(f"STOP consecutive=? limit={LIMIT} reason=history-unreadable: {e}")
            return 1
        run = consecutive_proxy(messages, entry.get("proxy_ts"))
        if run != entry.get("count", 0):
            # 履歴が正。あた本人の参加などで連鎖が切れていたら
            # 記録側もそれに合わせる（自己修復）。
            entry["count"] = run
            state[channel] = entry
            save_guard_state(state)
        if run >= LIMIT:
            print(f"STOP consecutive={run} limit={LIMIT} "
                  f"reason=limit-reached: report to Ata instead of auto-replying")
            return 1
        print(f"ALLOW consecutive={run} limit={LIMIT}")
        return 0


def cmd_record(channel, sent_ts):
    """送信成功の直後にだけ呼ぶ。連続回数を1増やす。"""
    with guard_lock():
        state = load_guard_state()
        entry = state.get(channel) or {"count": 0, "proxy_ts": []}
        proxy_ts = [str(t) for t in entry.get("proxy_ts", [])]
        if str(sent_ts) not in proxy_ts:
            proxy_ts.append(str(sent_ts))
            entry["proxy_ts"] = proxy_ts[-PROXY_TS_KEEP:]
            entry["count"] = entry.get("count", 0) + 1
        state[channel] = entry
        save_guard_state(state)
        print(f"recorded channel={channel} consecutive={entry['count']} limit={LIMIT}")
        return 0


def cmd_reset(channel):
    """手動の逃げ道。連続回数を 0 に戻す。"""
    with guard_lock():
        state = load_guard_state()
        state[channel] = {"count": 0, "proxy_ts": []}
        save_guard_state(state)
        print(f"reset channel={channel} consecutive=0 limit={LIMIT}")
        return 0


def main(argv):
    if len(argv) < 2 or argv[1] not in ("check", "record", "reset"):
        print(__doc__.split("\n\n")[0], file=sys.stderr)
        print("usage: dm_reply_guard.py {check|record|reset} --channel <conv> [--ts <ts>] [--helper PATH]",
              file=sys.stderr)
        return 2
    cmd = argv[1]
    args = argv[2:]
    channel = None
    sent_ts = None
    helper = os.environ.get("DM_REPLY_GUARD_HELPER") or DEFAULT_HELPER
    i = 0
    while i < len(args):
        if args[i] == "--channel" and i + 1 < len(args):
            channel = args[i + 1]
            i += 2
        elif args[i] == "--ts" and i + 1 < len(args):
            sent_ts = args[i + 1]
            i += 2
        elif args[i] == "--helper" and i + 1 < len(args):
            helper = args[i + 1]
            i += 2
        else:
            print(f"unknown argument: {args[i]}", file=sys.stderr)
            return 2
    if not channel:
        print("--channel is required", file=sys.stderr)
        return 2
    if cmd == "check":
        return cmd_check(helper, channel)
    if cmd == "record":
        if not sent_ts:
            print("--ts is required for record", file=sys.stderr)
            return 2
        return cmd_record(channel, sent_ts)
    return cmd_reset(channel)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
