#!/usr/bin/env python3
"""Tests for post_metadata.py: direct chat.postMessage with metadata.

Three-way agreement 2026-11-14 in #slack-へようこそ (Yama's metadata
design, Neiro's slack-post-metadata.py reference): replies are sent
by calling chat.postMessage directly with metadata
{"event_type": "yanayana_reply",
 "event_payload": {"event_id": <Slack event_id>}} (event-driven) or
{"request_id": <UUID>} (manual posts), and threads are read with
include_all_metadata=true so a reply can be found by its stamp.

Plain stdlib script, no pytest (same style as the reply_once tests).
Run:
  python3 tests/test_post_metadata.py
Exit 0 when every check passes; each check prints PASS/FAIL.

No real Slack call is made: the module-level tests inject a fake
urlopen, and the CLI tests run against a local stub HTTP server
(via SLACK_API_BASE) with a fake token in a temp slackcli store.
The real events/ directory and the real token store are never
touched.
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import post_metadata

CHECKS = [0, 0]  # [passed, failed]

FAKE_TOKEN = "xoxb-REPLACE-ME"
CHANNEL = "C0EXAMPLE001"
THREAD_TS = "1000.000000"
EVENT_ID = "Ev0TESTEVENTID1"
REQUEST_ID = "123e4567-e89b-12d3-a456-426614174000"


def check(name, cond, detail=""):
    CHECKS[0 if cond else 1] += 1
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not cond else ""))
    return cond


class FakeResponse:
    def __init__(self, body, headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        self._body = body.encode("utf-8") if isinstance(body, str) else body
        self.headers = headers or {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeUrlopen:
    """Scripted urlopen: outcomes are ("ok", dict) / ("http", code,
    dict, headers) / ("network",) / ("badjson", str). Records every
    request (method, url, parsed JSON body, auth header presence)."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []
        self.sleeps = []

    def __call__(self, req, timeout=None):
        body = None
        if getattr(req, "data", None):
            try:
                body = json.loads(req.data.decode("utf-8"))
            except ValueError:
                body = None
        self.calls.append({
            "method": req.get_method(),
            "url": req.full_url,
            "body": body,
            "auth": req.get_header("Authorization"),
        })
        kind = self.outcomes.pop(0) if self.outcomes else ("ok", {"ok": True})
        if kind[0] == "ok":
            return FakeResponse(kind[1])
        if kind[0] == "http":
            _, code, payload = kind[0], kind[1], kind[2]
            headers = kind[3] if len(kind) > 3 else {}
            raise urllib.error.HTTPError(req.full_url, code, "error",
                                        headers, io.BytesIO(b"{}"))
        if kind[0] == "network":
            raise urllib.error.URLError("fake connection reset")
        if kind[0] == "badjson":
            return FakeResponse(kind[1])
        raise AssertionError("unknown outcome %r" % (kind,))

    def sleep(self, secs):
        self.sleeps.append(secs)


def _meta_msg(ts, event_id=None, request_id=None, event_type="yanayana_reply",
              user="U0EXAMPLEBOT", text="hello"):
    payload = {}
    if event_id is not None:
        payload = {"event_id": event_id}
    elif request_id is not None:
        payload = {"request_id": request_id}
    msg = {"ts": ts, "user": user, "text": text}
    if payload:
        msg["metadata"] = {"event_type": event_type, "event_payload": payload}
    return msg


def test_build_payload():
    p = post_metadata.build_payload(CHANNEL, "hi", THREAD_TS, event_id=EVENT_ID)
    check("payload event_id stamp",
          p["metadata"] == {"event_type": "yanayana_reply",
                            "event_payload": {"event_id": EVENT_ID}}
          and p["channel"] == CHANNEL and p["text"] == "hi"
          and p["thread_ts"] == THREAD_TS)
    p = post_metadata.build_payload(CHANNEL, "hi", THREAD_TS,
                                    request_id=REQUEST_ID)
    check("payload request_id stamp",
          p["metadata"]["event_payload"] == {"request_id": REQUEST_ID}
          and p["metadata"]["event_type"] == "yanayana_reply")
    try:
        post_metadata.build_payload(CHANNEL, "hi", THREAD_TS,
                                    event_id=EVENT_ID, request_id=REQUEST_ID)
        check("payload rejects both ids", False, "no ValueError")
    except ValueError:
        check("payload rejects both ids", True)
    try:
        post_metadata.build_payload(CHANNEL, "hi", THREAD_TS)
        check("payload rejects neither id", False, "no ValueError")
    except ValueError:
        check("payload rejects neither id", True)


def test_post_success_and_headers():
    fake = FakeUrlopen([("ok", {"ok": True, "ts": "1794660000.111111",
                                "channel": CHANNEL})])
    ok, result = post_metadata.post(
        post_metadata.build_payload(CHANNEL, "hi", THREAD_TS, event_id=EVENT_ID),
        FAKE_TOKEN, urlopen=fake, sleep=fake.sleep)
    check("post success returns ts",
          ok and result.get("ts") == "1794660000.111111")
    check("post sends one request with metadata + auth",
          len(fake.calls) == 1
          and fake.calls[0]["body"]["metadata"]["event_payload"] == {"event_id": EVENT_ID}
          and fake.calls[0]["auth"] == "Bearer " + FAKE_TOKEN
          and fake.calls[0]["method"] == "POST")


def test_post_retries():
    fake = FakeUrlopen([
        ("http", 429, {"ok": False, "error": "ratelimited"}, {"Retry-After": "0"}),
        ("http", 500, {"ok": False}, {}),
        ("ok", {"ok": True, "ts": "1794660000.222222"}),
    ])
    ok, result = post_metadata.post(
        post_metadata.build_payload(CHANNEL, "hi", THREAD_TS, event_id=EVENT_ID),
        FAKE_TOKEN, urlopen=fake, sleep=fake.sleep)
    check("post retries 429 then 500 and succeeds",
          ok and result.get("ts") == "1794660000.222222"
          and len(fake.calls) == 3, f"calls={len(fake.calls)}")
    check("429 honors Retry-After 0", fake.sleeps and fake.sleeps[0] == 0.0,
          f"sleeps={fake.sleeps}")

    fake = FakeUrlopen([
        ("network",),
        ("ok", {"ok": True, "ts": "1794660000.333333"}),
    ])
    ok, result = post_metadata.post(
        post_metadata.build_payload(CHANNEL, "hi", THREAD_TS, event_id=EVENT_ID),
        FAKE_TOKEN, urlopen=fake, sleep=fake.sleep)
    check("post retries a network error and succeeds",
          ok and len(fake.calls) == 2, f"calls={len(fake.calls)}")


def test_post_no_retry():
    fake = FakeUrlopen([("ok", {"ok": False, "error": "channel_not_found"})])
    ok, reason = post_metadata.post(
        post_metadata.build_payload(CHANNEL, "hi", THREAD_TS, event_id=EVENT_ID),
        FAKE_TOKEN, urlopen=fake, sleep=fake.sleep)
    check("post never retries ok:false",
          not ok and reason == "slack: channel_not_found"
          and len(fake.calls) == 1, f"calls={len(fake.calls)} reason={reason}")

    fake = FakeUrlopen([("ok", {"ok": False, "error": "ratelimited"})])
    ok, reason = post_metadata.post(
        post_metadata.build_payload(CHANNEL, "hi", THREAD_TS, event_id=EVENT_ID),
        FAKE_TOKEN, urlopen=fake, sleep=fake.sleep)
    check("post never retries body ratelimited either",
          not ok and len(fake.calls) == 1, f"calls={len(fake.calls)}")

    fake = FakeUrlopen([("http", 400, {"ok": False, "error": "invalid_arg"}, {})])
    ok, reason = post_metadata.post(
        post_metadata.build_payload(CHANNEL, "hi", THREAD_TS, event_id=EVENT_ID),
        FAKE_TOKEN, urlopen=fake, sleep=fake.sleep)
    check("post never retries a 400",
          not ok and reason == "http_400" and len(fake.calls) == 1)

    fake = FakeUrlopen([("http", 500, {"ok": False}, {})] * 3)
    ok, reason = post_metadata.post(
        post_metadata.build_payload(CHANNEL, "hi", THREAD_TS, event_id=EVENT_ID),
        FAKE_TOKEN, urlopen=fake, sleep=fake.sleep)
    check("post gives up after 3 attempts on persistent 500",
          not ok and reason == "http_500" and len(fake.calls) == 3,
          f"calls={len(fake.calls)} reason={reason}")

    fake = FakeUrlopen([("network",)] * 3)
    ok, reason = post_metadata.post(
        post_metadata.build_payload(CHANNEL, "hi", THREAD_TS, event_id=EVENT_ID),
        FAKE_TOKEN, urlopen=fake, sleep=fake.sleep)
    check("post gives up after 3 network failures",
          not ok and reason.startswith("network:") and len(fake.calls) == 3)


def test_find_by_metadata():
    msgs = [
        _meta_msg("1000.000000", text="parent"),
        {"ts": "1001.000000", "user": "U0OTHER", "text": "question"},
        _meta_msg("1002.000000", event_id=EVENT_ID, text="our reply"),
        _meta_msg("1003.000000", request_id=REQUEST_ID, text="manual reply"),
        _meta_msg("1004.000000", event_id=EVENT_ID, event_type="other_reply",
                  text="another bot with same id"),
        _meta_msg("1005.000000", event_id="Ev0OTHER00002", text="other event"),
    ]
    found = post_metadata.find_by_metadata(msgs, event_id=EVENT_ID)
    check("find_by_metadata hits by event_id",
          found is not None and found["ts"] == "1002.000000")
    found = post_metadata.find_by_metadata(msgs, request_id=REQUEST_ID)
    check("find_by_metadata hits by request_id",
          found is not None and found["ts"] == "1003.000000")
    check("find_by_metadata miss returns None",
          post_metadata.find_by_metadata(msgs, event_id="Ev0MISSING1") is None)
    check("find_by_metadata ignores another bot's event_type",
          post_metadata.find_by_metadata(
              [msgs[4]], event_id=EVENT_ID) is None)
    check("find_by_metadata ignores messages without metadata",
          post_metadata.find_by_metadata(
              [msgs[0], msgs[1]], event_id=EVENT_ID) is None)
    try:
        post_metadata.find_by_metadata(msgs, event_id=EVENT_ID,
                                       request_id=REQUEST_ID)
        check("find_by_metadata rejects both ids", False, "no ValueError")
    except ValueError:
        check("find_by_metadata rejects both ids", True)


def test_read_thread_paging():
    page1 = {"ok": True, "messages": [
        _meta_msg("1000.000000", text="parent"),
        _meta_msg("1001.000000", event_id=EVENT_ID, text="our reply"),
    ], "has_more": True, "response_metadata": {"next_cursor": "abc"}}
    page2 = {"ok": True, "messages": [
        _meta_msg("1002.000000", request_id=REQUEST_ID, text="manual"),
    ], "has_more": False, "response_metadata": {"next_cursor": ""}}
    fake = FakeUrlopen([("ok", page1), ("ok", page2)])
    msgs = post_metadata.read_thread(CHANNEL, THREAD_TS, FAKE_TOKEN,
                                     urlopen=fake, sleep=fake.sleep)
    check("read_thread pages and concatenates",
          len(msgs) == 3 and msgs[1]["ts"] == "1001.000000")
    check("read_thread asks for include_all_metadata",
          all("include_all_metadata=true" in c["url"] for c in fake.calls)
          and "cursor=abc" in fake.calls[1]["url"],
          f"urls={[c['url'] for c in fake.calls]}")
    fake = FakeUrlopen([("ok", page1),
                        ("http", 500, {"ok": False}, {}),
                        ("ok", page2)])
    msgs = post_metadata.read_thread(CHANNEL, THREAD_TS, FAKE_TOKEN,
                                     urlopen=fake, sleep=fake.sleep)
    check("read_thread retries a failed page",
          len(msgs) == 3 and len(fake.calls) == 3)
    fake = FakeUrlopen([("ok", {"ok": False, "error": "channel_not_found"})])
    try:
        post_metadata.read_thread(CHANNEL, THREAD_TS, FAKE_TOKEN,
                                  urlopen=fake, sleep=fake.sleep)
        check("read_thread raises on ok:false", False, "no exception")
    except RuntimeError:
        check("read_thread raises on ok:false", True)


def test_find_reply():
    page = {"ok": True, "messages": [
        _meta_msg("1000.000000", text="parent"),
        _meta_msg("1001.000000", event_id=EVENT_ID, text="our reply"),
    ], "has_more": False, "response_metadata": {}}
    fake = FakeUrlopen([("ok", page)])
    found = post_metadata.find_reply(CHANNEL, THREAD_TS, FAKE_TOKEN,
                                     event_id=EVENT_ID,
                                     urlopen=fake, sleep=fake.sleep)
    check("find_reply returns the stamped message",
          found is not None and found["ts"] == "1001.000000")


def test_resolve_token():
    tmp = tempfile.mkdtemp(prefix="postmeta-token-")
    try:
        store = os.path.join(tmp, "workspaces.json")
        with open(store, "w", encoding="utf-8") as f:
            json.dump({"workspaces": {"yanayana": {
                "token": "xoxb-REPLACE-ME",
                "workspace_id": "T0EXAMPLE001",
                "workspace_name": "muse-board",
                "user_id": "U0EXAMPLEBOT", "profile": "yanayana"}},
                "default_workspace": "yanayana"}, f)
        config = os.path.join(tmp, "config.json")
        with open(config, "w", encoding="utf-8") as f:
            json.dump({"bot_token": "xoxb-REPLACE-ME"}, f)
        check("resolve_token prefers the slackcli store",
              post_metadata.resolve_token(store_path=store, config_path=config)
              == "xoxb-REPLACE-ME")
        check("resolve_token finds the store by workspace id",
              post_metadata.resolve_token("T0EXAMPLE001", store_path=store,
                                          config_path=config)
              == "xoxb-REPLACE-ME")
        check("resolve_token falls back to config.json",
              post_metadata.resolve_token(store_path=os.path.join(tmp, "missing.json"),
                                          config_path=config)
              == "xoxb-REPLACE-ME")
        check("resolve_token returns None when both are missing",
              post_metadata.resolve_token(store_path=os.path.join(tmp, "missing.json"),
                                          config_path=os.path.join(tmp, "nope.json"))
              is None)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


class StubHandler(BaseHTTPRequestHandler):
    requests = []
    post_response = {"ok": True, "ts": "1794660000.444444", "channel": CHANNEL}
    replies_pages = []

    def log_message(self, *a):
        pass

    def _send_json(self, payload, code=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        StubHandler.requests.append({
            "method": "POST", "path": self.path,
            "body": json.loads(raw.decode("utf-8")) if raw else None,
            "auth": self.headers.get("Authorization"),
        })
        self._send_json(StubHandler.post_response)

    def do_GET(self):
        StubHandler.requests.append({
            "method": "GET", "path": self.path,
            "auth": self.headers.get("Authorization"),
        })
        if self.replies_pages:
            self._send_json(self.replies_pages.pop(0))
        else:
            self._send_json({"ok": True, "messages": [], "has_more": False,
                             "response_metadata": {}})


def _cli_env(tmp, port):
    store = os.path.join(tmp, "workspaces.json")
    with open(store, "w", encoding="utf-8") as f:
        json.dump({"workspaces": {"yanayana": {
            "token": FAKE_TOKEN, "workspace_id": "T0EXAMPLE001",
            "workspace_name": "muse-board", "user_id": "U0EXAMPLEBOT",
            "profile": "yanayana"}},
            "default_workspace": "yanayana"}, f)
    config = os.path.join(tmp, "config.json")
    with open(config, "w", encoding="utf-8") as f:
        json.dump({"bot_token": "xoxb-REPLACE-ME"}, f)
    env = dict(os.environ)
    env["SLACK_API_BASE"] = f"http://127.0.0.1:{port}/api"
    env["POST_METADATA_STORE"] = store
    env["POST_METADATA_CONFIG"] = config
    return env


def _run_cli(env, *args):
    cmd = [sys.executable, os.path.join(ROOT, "post_metadata.py")] + list(args)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env)


def test_cli():
    server = ThreadingHTTPServer(("127.0.0.1", 0), StubHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    tmp = tempfile.mkdtemp(prefix="postmeta-cli-")
    try:
        env = _cli_env(tmp, port)

        # Dry-run: no token read, no HTTP call.
        StubHandler.requests.clear()
        r = _run_cli(env, "--channel", CHANNEL, "--thread-ts", THREAD_TS,
                     "--message", "hello", "--event-id", EVENT_ID, "--dry-run")
        out = json.loads(r.stdout) if r.stdout.strip() else {}
        check("CLI dry-run prints the payload and sends nothing",
              r.returncode == 0 and out.get("dry_run") is True
              and out["payload"]["metadata"]["event_payload"] == {"event_id": EVENT_ID}
              and len(StubHandler.requests) == 0,
              f"rc={r.returncode} out={r.stdout.strip()}")

        # Send: one POST to the stub with metadata and the fake token.
        StubHandler.requests.clear()
        r = _run_cli(env, "--channel", CHANNEL, "--thread-ts", THREAD_TS,
                     "--message", "hello", "--request-id", REQUEST_ID)
        out = json.loads(r.stdout) if r.stdout.strip() else {}
        check("CLI send exits 0 with the reply ts",
              r.returncode == 0 and out.get("ok") is True
              and out.get("ts") == "1794660000.444444",
              f"rc={r.returncode} out={r.stdout.strip()}")
        check("CLI send posts metadata + bearer auth to the stub",
              len(StubHandler.requests) == 1
              and StubHandler.requests[0]["body"]["metadata"]["event_payload"]
              == {"request_id": REQUEST_ID}
              and StubHandler.requests[0]["auth"] == "Bearer " + FAKE_TOKEN,
              f"requests={StubHandler.requests}")

        # Find: hit.
        StubHandler.requests.clear()
        StubHandler.replies_pages = [{"ok": True, "messages": [
            _meta_msg("1000.000000", text="parent"),
            _meta_msg("1001.000000", event_id=EVENT_ID, text="our reply"),
        ], "has_more": False, "response_metadata": {}}]
        r = _run_cli(env, "--channel", CHANNEL, "--thread-ts", THREAD_TS,
                     "--find-event-id", EVENT_ID)
        out = json.loads(r.stdout) if r.stdout.strip() else {}
        check("CLI find reports a metadata hit",
              r.returncode == 0 and out.get("found") is True
              and out.get("ts") == "1001.000000",
              f"rc={r.returncode} out={r.stdout.strip()}")
        check("CLI find reads with include_all_metadata",
              len(StubHandler.requests) == 1
              and "include_all_metadata=true" in StubHandler.requests[0]["path"],
              f"requests={StubHandler.requests}")

        # Find: miss.
        StubHandler.requests.clear()
        StubHandler.replies_pages = [{"ok": True, "messages": [
            _meta_msg("1000.000000", text="parent"),
        ], "has_more": False, "response_metadata": {}}]
        r = _run_cli(env, "--channel", CHANNEL, "--thread-ts", THREAD_TS,
                     "--find-request-id", REQUEST_ID)
        out = json.loads(r.stdout) if r.stdout.strip() else {}
        check("CLI find reports a miss with exit 0",
              r.returncode == 0 and out.get("found") is False,
              f"rc={r.returncode} out={r.stdout.strip()}")

        # Usage error: neither stamp given.
        r = _run_cli(env, "--channel", CHANNEL, "--thread-ts", THREAD_TS,
                     "--message", "hello")
        check("CLI rejects a send with no stamp",
              r.returncode == 1, f"rc={r.returncode} out={r.stdout.strip()}")
    finally:
        server.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    test_build_payload()
    test_post_success_and_headers()
    test_post_retries()
    test_post_no_retry()
    test_find_by_metadata()
    test_read_thread_paging()
    test_find_reply()
    test_resolve_token()
    test_cli()
    print(f"\n{CHECKS[0]} passed, {CHECKS[1]} failed")
    return 1 if CHECKS[1] else 0


if __name__ == "__main__":
    sys.exit(main())
