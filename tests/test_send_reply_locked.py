#!/usr/bin/env python3
"""Tests for send_reply_locked.py: check -> send -> record serialized.

Yama's 2026-11-14 question in #slack-へようこそ (thread
1794583788.831849): the pre-send check and the send are separate
steps, so two concurrent workers can both check "not replied" and
both send. The wrapper holds one flock across replylog check,
reply_once.py (check + send + record), and the replylog record.

Plain stdlib script, no pytest (same style as the reply_once tests).
Run:
  python3 tests/test_send_reply_locked.py
Exit 0 when every check passes; each check prints PASS/FAIL.

The wrapper, reply_once.py and replylog.py are copied into a temp
dir with a fake bin/slackcli (thread read returns the parent only;
send sleeps briefly to open a real race window and logs each send),
so no real Slack call is made and the real events/ directory is
never touched.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

CHECKS = [0, 0]  # [passed, failed]


def check(name, cond, detail=""):
    CHECKS[0 if cond else 1] += 1
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not cond else ""))
    return cond


FAKE_SLACKCLI = '''#!/usr/bin/env python3
import json, sys, time, os
args = sys.argv[1:]
base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if args[:2] == ["conversations", "read"]:
    msgs = [{"ts": "1000.000000", "thread_ts": "1000.000000",
             "user": "U0OTHER", "text": "parent"}]
    print(json.dumps({"channel_id": "C0EXAMPLE001", "message_count": 1,
                      "messages": msgs}, ensure_ascii=False))
    sys.exit(0)
if args[:2] == ["messages", "send"]:
    time.sleep(0.5)
    ts = f"{time.time():.6f}"
    with open(os.path.join(base, "events", "fake_sends.log"), "a") as f:
        f.write(ts + "\\n")
    print(json.dumps({"ok": True, "ts": ts}))
    sys.exit(0)
print("fake slackcli: unknown command", args, file=sys.stderr)
sys.exit(1)
'''

CHANNEL = "C0EXAMPLE001"
THREAD_TS = "1000.000000"
TARGET_TS = "2000.000000"
MESSAGE = "test reply text"


def setup_tmp():
    tmp = tempfile.mkdtemp(prefix="sendlock-test-")
    for name in ("send_reply_locked.py", "reply_once.py", "replylog.py",
                 "post_metadata.py", "msgstate.py", "inbox_state.py"):
        shutil.copy(os.path.join(ROOT, name), os.path.join(tmp, name))
    os.makedirs(os.path.join(tmp, "bin"))
    os.makedirs(os.path.join(tmp, "events"))
    fake = os.path.join(tmp, "bin", "slackcli")
    with open(fake, "w", encoding="utf-8") as f:
        f.write(FAKE_SLACKCLI)
    os.chmod(fake, 0o755)
    for name in ("replies.jsonl", "replied.jsonl"):
        open(os.path.join(tmp, "events", name), "w").close()
    return tmp


def run_wrapper(tmp, target=TARGET_TS, message=MESSAGE, extra=None,
                env_extra=None):
    cmd = [sys.executable, os.path.join(tmp, "send_reply_locked.py"),
           "--channel", CHANNEL, "--thread-ts", THREAD_TS,
           "--target-ts", target, "--message", message]
    if extra:
        cmd += extra
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                          env=env)


def count_lines(path):
    try:
        with open(path, encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())
    except FileNotFoundError:
        return 0


class StubSlackAPI(BaseHTTPRequestHandler):
    """Minimal Slack API stub for the metadata path: reads return the
    parent only (no stamp yet); posts record their payload and
    return a fresh ts. post_metadata.py talks to it via
    SLACK_API_BASE, with the token from a fake store file."""

    posts = []
    reads = []

    def log_message(self, *args):
        pass

    def _json(self, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        StubSlackAPI.reads.append(parsed.path)
        if parsed.path.endswith("/conversations.replies"):
            self._json({"ok": True,
                        "messages": [{"ts": THREAD_TS,
                                      "thread_ts": THREAD_TS,
                                      "user": "U0OTHER",
                                      "text": "parent"}],
                        "has_more": False})
        else:
            self._json({"ok": False, "error": "unknown_method"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        StubSlackAPI.posts.append(body)
        if self.path.endswith("/chat.postMessage"):
            self._json({"ok": True, "ts": "5000.000001",
                        "channel": CHANNEL})
        else:
            self._json({"ok": False, "error": "unknown_method"})


def test_metadata_end_to_end(tmp):
    """--event-id through the locked wrapper: the reply is sent via
    the stub API with the yanayana_reply stamp, both ledgers record
    it, and a re-run sends nothing more."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), StubSlackAPI)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        StubSlackAPI.posts = []
        StubSlackAPI.reads = []
        store = os.path.join(tmp, "store.json")
        with open(store, "w", encoding="utf-8") as f:
            json.dump({"workspaces": {"muse-board": {"token": "fake-test-token"}},
                       "default_workspace": "muse-board"}, f)
        env_extra = {
            "SLACK_API_BASE": f"http://127.0.0.1:{port}/api",
            "POST_METADATA_STORE": store,
        }
        sends_log = os.path.join(tmp, "events", "fake_sends.log")
        replies = os.path.join(tmp, "events", "replies.jsonl")
        replied = os.path.join(tmp, "events", "replied.jsonl")
        before_posts = len(StubSlackAPI.posts)
        r = run_wrapper(tmp, target="6000.000000", message="meta reply",
                        extra=["--event-id", "Ev0TEST00001"],
                        env_extra=env_extra)
        check("metadata run exits 0",
              r.returncode == 0,
              f"rc={r.returncode} out={r.stdout.strip()} err={r.stderr.strip()[:200]}")
        check("metadata run posts exactly once via the API",
              len(StubSlackAPI.posts) - before_posts == 1,
              f"posts={len(StubSlackAPI.posts) - before_posts}")
        check("metadata run does not use the slackcli send path",
              count_lines(sends_log) == 1,
              f"sends={count_lines(sends_log)}")
        if StubSlackAPI.posts:
            payload = StubSlackAPI.posts[-1]
            check("posted payload carries the yanayana_reply stamp",
                  payload.get("metadata") == {
                      "event_type": "yanayana_reply",
                      "event_payload": {"event_id": "Ev0TEST00001"}},
                  f"metadata={payload.get('metadata')}")
            check("posted payload is in the right thread",
                  payload.get("channel") == CHANNEL
                  and payload.get("thread_ts") == THREAD_TS)
        check("metadata run records both ledgers",
              count_lines(replies) == 3 and count_lines(replied) == 2,
              f"replies={count_lines(replies)} replied={count_lines(replied)}")
        r2 = run_wrapper(tmp, target="6000.000000", message="meta reply",
                         extra=["--event-id", "Ev0TEST00001"],
                         env_extra=env_extra)
        check("metadata re-run exits 0 without reposting",
              r2.returncode == 0 and "already-replied" in r2.stdout
              and len(StubSlackAPI.posts) - before_posts == 1,
              f"rc={r2.returncode} out={r2.stdout.strip()}")
    finally:
        server.shutdown()
        server.server_close()


def test_state_key_finalizes(tmp):
    """--state-key: the wrapper marks the msgstate entry done itself.

    The bookkeeping seam (2026-11-18 / 11-22 / 11-23): the hook flow
    used to send, record and mark done as three separate agent steps;
    a run that ended after the send left the answered message
    pending. Here one inbox message is claimed (processing), the
    wrapper sends with --state-key, and the state must be done with
    no further step. A second message whose reply is already in the
    replylog must also be finalized by the already-replied path
    without any send.
    """
    events = os.path.join(tmp, "events")
    inbox = [{"event_id": "Ev0STATEKEY1", "channel": CHANNEL,
              "ts": "7000.000000", "thread_ts": THREAD_TS,
              "user": "U0OTHER", "text": "question one",
              "received_at": "2026-11-23T00:00:00+00:00"},
             {"event_id": "Ev0STATEKEY2", "channel": CHANNEL,
              "ts": "4000.000000", "thread_ts": THREAD_TS,
              "user": "U0OTHER", "text": "question two",
              "received_at": "2026-11-23T00:01:00+00:00"}]
    with open(os.path.join(events, "inbox.jsonl"), "w", encoding="utf-8") as f:
        for rec in inbox:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    state = {"Ev0STATEKEY1": {"status": "processing", "attempts": 1},
             "Ev0STATEKEY2": {"status": "processing", "attempts": 1}}
    with open(os.path.join(events, "message_state.json"), "w", encoding="utf-8") as f:
        json.dump(state, f)

    def read_state():
        with open(os.path.join(events, "message_state.json"), encoding="utf-8") as f:
            return json.load(f)

    sends_log = os.path.join(events, "fake_sends.log")
    before = count_lines(sends_log)
    r = run_wrapper(tmp, target="7000.000000", message="state key reply",
                    extra=["--state-key", "Ev0STATEKEY1"])
    check("state-key send exits 0", r.returncode == 0,
          f"rc={r.returncode} out={r.stdout.strip()} err={r.stderr.strip()[:200]}")
    check("state-key send actually sent once",
          count_lines(sends_log) == before + 1)
    check("state-key send marks the entry done",
          read_state().get("Ev0STATEKEY1", {}).get("status") == "done",
          f"state={read_state().get('Ev0STATEKEY1')}")

    # Already-replied via the pre-seeded replylog row for 4000.000000
    # (written by the earlier check in main): no send, still done.
    r = run_wrapper(tmp, target="4000.000000",
                    extra=["--state-key", "Ev0STATEKEY2"])
    check("state-key already-replied exits 0 without sending",
          r.returncode == 0 and "already-replied" in r.stdout
          and count_lines(sends_log) == before + 1,
          f"rc={r.returncode} out={r.stdout.strip()}")
    check("state-key already-replied marks the entry done",
          read_state().get("Ev0STATEKEY2", {}).get("status") == "done",
          f"state={read_state().get('Ev0STATEKEY2')}")


def main():
    tmp = setup_tmp()
    try:
        sends_log = os.path.join(tmp, "events", "fake_sends.log")
        replies = os.path.join(tmp, "events", "replies.jsonl")
        replied = os.path.join(tmp, "events", "replied.jsonl")

        # 1. Two concurrent runs for the same target: exactly one send.
        results = []
        def _run():
            results.append(run_wrapper(tmp))
        t1 = threading.Thread(target=_run)
        t2 = threading.Thread(target=_run)
        t1.start(); t2.start(); t1.join(); t2.join()
        check("concurrent runs both exit 0",
              all(r.returncode == 0 for r in results),
              "; ".join(f"rc={r.returncode} out={r.stdout.strip()} err={r.stderr.strip()[:100]}" for r in results))
        check("concurrent runs send exactly once", count_lines(sends_log) == 1,
              f"sends={count_lines(sends_log)}")
        check("replies.jsonl has exactly one entry", count_lines(replies) == 1)
        check("replied.jsonl has exactly one entry", count_lines(replied) == 1)
        if count_lines(replies) == 1 and count_lines(replied) == 1:
            with open(replies, encoding="utf-8") as f:
                a = json.loads(f.readline())
            with open(replied, encoding="utf-8") as f:
                b = json.loads(f.readline())
            check("both ledgers record the same sent ts",
                  a.get("sent_ts") == b.get("reply_ts") and a.get("target_ts") == TARGET_TS)

        # 2. Re-run for the same target: already-replied, no new send.
        r = run_wrapper(tmp)
        check("re-run exits 0 without sending",
              r.returncode == 0 and "already-replied" in r.stdout
              and count_lines(sends_log) == 1,
              f"rc={r.returncode} out={r.stdout.strip()}")

        # 3. Dry-run for a fresh target: no send.
        r = run_wrapper(tmp, target="3000.000000", extra=["--dry-run"])
        check("dry-run exits 0 without sending",
              r.returncode == 0 and "would-send" in r.stdout
              and count_lines(sends_log) == 1,
              f"rc={r.returncode} out={r.stdout.strip()}")

        # 4. replylog already FOUND blocks the send before reply_once.
        with open(replies, "a", encoding="utf-8") as f:
            f.write(json.dumps({"channel": CHANNEL, "target_ts": "4000.000000",
                                "sent_ts": "4000.000001",
                                "thread_ts": THREAD_TS}) + "\n")
        r = run_wrapper(tmp, target="4000.000000")
        check("pre-seeded replylog entry blocks send",
              r.returncode == 0 and "already-replied (replylog)" in r.stdout
              and count_lines(sends_log) == 1,
              f"rc={r.returncode} out={r.stdout.strip()}")

        # 5. Metadata path end to end (stub Slack API).
        test_metadata_end_to_end(tmp)

        # 6. --state-key finalizes msgstate inside the wrapper.
        test_state_key_finalizes(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{CHECKS[0]} passed, {CHECKS[1]} failed")
    return 1 if CHECKS[1] else 0


if __name__ == "__main__":
    sys.exit(main())
