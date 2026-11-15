#!/usr/bin/env python3
"""Send Slack thread replies directly via chat.postMessage, with metadata.

Adopted 2026-11-14 from the three-way agreement in #slack-へようこそ
(Yama's metadata design, Neiro's slack-post-metadata.py reference):
slackcli's `messages send` has no way to attach metadata, so thread
replies that need to be matched later go through chat.postMessage
directly.

Stamp shape (agreed with Yama and Neiro):
  metadata: {"event_type": "yanayana_reply",
             "event_payload": {"event_id": "<Slack event_id, Ev...>"}}
  for event-driven replies, or
  metadata: {"event_type": "yanayana_reply",
             "event_payload": {"request_id": "<UUID>"}}
  for manual posts (no originating event).

Read side: threads are read with conversations.replies and
include_all_metadata=true, and matching prefers metadata (our
event_id / request_id) first, exact text match second -- text
matching stays in reply_once.py; this module only does the
metadata-first lookup.

Retry policy (revised 2026-11-15 from Neiro's triple-post fix in
#slack-へようこそ): a POST retries only on HTTP 429 -- Slack
rejected the call before receiving it, so a resend cannot
duplicate. Network errors, timeouts and 5xx are NOT retried on a
POST: the post may already have landed and only the response was
lost (that is exactly how Neiro's identical reply was posted
three times). Arrival is then checked by the caller via the
thread history / metadata stamp (reply_once.py, reconcile_ambiguous.py).
GET reads are idempotent, so they still retry 429, 5xx and network
errors, up to 3 attempts with backoff 1s, 2s (429 honors
Retry-After). Never retry when the API returns ok:false -- a
rejected call is a verdict, not a transient failure. This is
deliberately stricter than Neiro's reference, which also retries a
200 response carrying error "ratelimited"; the agreement is that
ok:false is never retried.

The bot token is read from slackcli's auth store
(~/.config/slackcli/workspaces.json, profile "yanayana" / workspace
"muse-board"), falling back to config.json next to this file
(the listener's token source). The token is never printed, logged
or written anywhere new.

This module only sends and reads; it does not decide whether a
reply is a duplicate and does not write any ledger. It is wired
into the live send path through reply_once.py --event-id (and
send_reply_locked.py --event-id): event-driven replies go through
here with their originating event_id, manual posts keep the
slackcli path.

Usage:
  post_metadata.py --channel C... --thread-ts T... --message "..."
      (--event-id Ev... | --request-id <UUID>)
      [--workspace muse-board] [--event-type yanayana_reply]
      [--message-file FILE] [--dry-run]
  post_metadata.py --channel C... --thread-ts T...
      (--find-event-id Ev... | --find-request-id <UUID>)
      # prints {"ok": true, "found": bool, ...}; sends nothing

Exit codes: 0 = sent / found-or-not-found reported / dry-run ok;
1 = usage error, token not found, send failed, or read failed.

Test hooks (env vars, used by tests/test_post_metadata.py):
  SLACK_API_BASE            default https://slack.com/api
  POST_METADATA_STORE       path to the slackcli store (default
                            ~/.config/slackcli/workspaces.json)
  POST_METADATA_CONFIG      path to config.json (default ./config.json)
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STORE = os.path.expanduser("~/.config/slackcli/workspaces.json")
DEFAULT_CONFIG = os.path.join(BASE, "config.json")

EVENT_TYPE = "yanayana_reply"
MAX_ATTEMPTS = 3
BACKOFF_SECS = (1, 2)
API_BASE = os.environ.get("SLACK_API_BASE", "https://slack.com/api")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        raise RuntimeError("redirect refused, verify the endpoint")


def _api_base():
    # Read at call time so tests can point the module at a local stub.
    return os.environ.get("SLACK_API_BASE", "https://slack.com/api")


def load_workspaces(path=None):
    """slackcli's auth store as a dict, or None when unreadable."""
    if path is None:
        path = os.environ.get("POST_METADATA_STORE", DEFAULT_STORE)
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _token_from_store(workspace, path=None):
    data = load_workspaces(path)
    if not data:
        return None
    entries = data.get("workspaces", {})
    if not isinstance(entries, dict):
        return None
    entry = None
    if workspace is None:
        entry = entries.get(data.get("default_workspace", ""))
    elif workspace in entries:
        entry = entries[workspace]
    else:
        for e in entries.values():
            if isinstance(e, dict) and e.get("workspace_id") == workspace:
                entry = e
                break
            if isinstance(e, dict) and e.get("workspace_name") == workspace:
                entry = e
                break
    if not isinstance(entry, dict):
        return None
    token = entry.get("token")
    return token or None


def _token_from_config(path=None):
    if path is None:
        path = os.environ.get("POST_METADATA_CONFIG", DEFAULT_CONFIG)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    token = data.get("bot_token")
    return token or None


def resolve_token(workspace=None, store_path=None, config_path=None):
    """Bot token from the slackcli store, falling back to config.json.

    workspace may be a profile name (muse-board), a workspace id, or
    None for the store's default. Returns None when neither source
    yields a token. The token value is never logged.
    """
    token = _token_from_store(workspace, store_path)
    if token:
        return token
    return _token_from_config(config_path)


def build_payload(channel, text, thread_ts=None, event_id=None,
                  request_id=None, event_type=EVENT_TYPE):
    """chat.postMessage body. event_id / request_id: exactly one."""
    if bool(event_id) == bool(request_id):
        raise ValueError("event_id and request_id: specify exactly one")
    if event_id is not None:
        event_payload = {"event_id": str(event_id)}
    else:
        event_payload = {"request_id": str(request_id)}
    payload = {
        "channel": channel,
        "text": text,
        "metadata": {"event_type": event_type,
                     "event_payload": event_payload},
    }
    if thread_ts:
        payload["thread_ts"] = thread_ts
    return payload


def _is_retryable_http(code, method):
    # 429: Slack rejected the call before receiving it, so a resend
    # cannot duplicate -- retryable for any method. 5xx on a GET
    # read is also retryable (reads are idempotent); on a POST it
    # is not, because the post may already have landed and only the
    # response was lost (Neiro's 2026-11-15 triple post).
    return code == 429 or (method == "GET" and code >= 500)


def _wait_for_retry(attempt, retry_after):
    """Backoff before the next attempt (1s, 2s); 429 honors Retry-After."""
    wait = BACKOFF_SECS[attempt - 1] if attempt <= len(BACKOFF_SECS) else BACKOFF_SECS[-1]
    if retry_after is not None:
        try:
            wait = float(retry_after)
        except (TypeError, ValueError):
            pass
    return max(0.0, wait)


def _request_json(url, token, data=None, method="GET",
                  urlopen=None, sleep=None):
    """One Slack API call with the agreed retry policy.

    Returns the parsed JSON dict. Raises RuntimeError on final
    failure: retryable errors are retried up to MAX_ATTEMPTS; other
    HTTP errors and ok:false are returned/raised immediately without
    retry, as is a non-JSON 200 body. On a POST, only HTTP 429 is
    retryable -- network errors, timeouts and 5xx are raised at
    once, because the post may already have landed and only the
    response was lost; the caller then checks arrival via the
    thread history / metadata stamp instead of blind-resending.
    """
    if urlopen is None:
        urlopen = urllib.request.urlopen
    if sleep is None:
        sleep = time.sleep
    headers = {"Authorization": "Bearer " + token,
               "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) "
                             "AppleWebKit/537.36 (KHTML, like Gecko) "
                             "Chrome/140.0.0.0 Safari/537.36"}
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    last_error = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        req = urllib.request.Request(url, data=body, headers=headers,
                                     method=method)
        try:
            with urlopen(req, timeout=30) as resp:
                raw = resp.read()
                retry_after = None
                try:
                    retry_after = resp.headers.get("Retry-After")
                except AttributeError:
                    pass
        except urllib.error.HTTPError as e:
            if _is_retryable_http(e.code, method) and attempt < MAX_ATTEMPTS:
                sleep(_wait_for_retry(attempt, _header_retry_after(e)))
                continue
            raise RuntimeError("http_%d" % e.code)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_error = e
            # GET reads are idempotent, so a lost read response is
            # safe to retry. A POST is not: the post may already
            # have landed (Neiro's 2026-11-15 triple post).
            if method == "GET" and attempt < MAX_ATTEMPTS:
                sleep(_wait_for_retry(attempt, None))
                continue
            raise RuntimeError("network: %s" % e)
        try:
            result = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise RuntimeError("bad_response")
        if not result.get("ok"):
            # ok:false is a verdict, never retried -- even
            # error "ratelimited" in the body (stricter than
            # Neiro's reference, per the three-way agreement).
            raise RuntimeError("slack: %s" % result.get("error", "unknown_error"))
        return result
    raise RuntimeError("network: %s" % last_error)


def _header_retry_after(error):
    try:
        return error.headers.get("Retry-After")
    except AttributeError:
        return None


def post(payload, token, urlopen=None, sleep=None):
    """POST chat.postMessage. Returns (True, result) or (False, reason)."""
    try:
        result = _request_json(_api_base() + "/chat.postMessage", token,
                               data=payload, method="POST",
                               urlopen=urlopen, sleep=sleep)
    except RuntimeError as e:
        return False, str(e)
    return True, result


def send_reply(channel, thread_ts, text, event_id=None, request_id=None,
               workspace=None, token=None, event_type=EVENT_TYPE,
               urlopen=None, sleep=None):
    """Build the metadata payload and send it. Returns (ok, result)."""
    try:
        payload = build_payload(channel, text, thread_ts,
                                event_id=event_id, request_id=request_id,
                                event_type=event_type)
    except ValueError as e:
        return False, str(e)
    if token is None:
        token = resolve_token(workspace)
    if not token:
        return False, "token_not_found"
    return post(payload, token, urlopen=urlopen, sleep=sleep)


def read_thread(channel, thread_ts, token, urlopen=None, sleep=None,
                limit=200, oldest=None):
    """All messages in a thread, oldest first, with metadata attached.

    conversations.replies with include_all_metadata=true, paged by
    cursor. Raises RuntimeError when a page cannot be read after the
    agreed retries (fail-closed: the caller must not decide from a
    partial thread). With oldest set, only messages from that ts
    onward are read (the windowed duplicate check's 60s-before-target
    window).
    """
    messages = []
    cursor = None
    while True:
        params = {
            "channel": channel,
            "ts": thread_ts,
            "limit": str(limit),
            "include_all_metadata": "true",
        }
        if oldest is not None:
            params["oldest"] = str(oldest)
        if cursor:
            params["cursor"] = cursor
        query = urllib.parse.urlencode(params)
        result = _request_json(
            _api_base() + "/conversations.replies?" + query, token,
            method="GET", urlopen=urlopen, sleep=sleep)
        messages.extend(result.get("messages", []))
        cursor = (result.get("response_metadata") or {}).get("next_cursor")
        if not result.get("has_more") or not cursor:
            break
    return messages


def stamp_event_id(message, event_type=EVENT_TYPE):
    """event_id from our metadata stamp on a message, else None.

    Only a stamp with our event_type and an event_id payload counts:
    a request_id stamp (manual post) or another bot's stamp carries
    no event_id for this event, so it stays a plain text candidate
    for the caller's text check instead of being excluded.
    """
    meta = (message or {}).get("metadata") or {}
    if meta.get("event_type") != event_type:
        return None
    payload = meta.get("event_payload") or {}
    value = payload.get("event_id")
    return str(value) if value is not None else None


def find_by_metadata(messages, event_id=None, request_id=None,
                     event_type=EVENT_TYPE):
    """First message whose metadata matches, else None.

    event_id / request_id: exactly one. The match requires our
    event_type (yanayana_reply) as well as the payload value, so a
    coincidentally equal payload from another bot's stamp cannot
    match. Metadata is preferred over text: callers that also
    text-match should try this first.
    """
    if bool(event_id) == bool(request_id):
        raise ValueError("event_id and request_id: specify exactly one")
    key = "event_id" if event_id is not None else "request_id"
    wanted = str(event_id if event_id is not None else request_id)
    for m in messages:
        meta = m.get("metadata") or {}
        if meta.get("event_type") != event_type:
            continue
        payload = meta.get("event_payload") or {}
        if str(payload.get(key)) == wanted:
            return m
    return None


def find_reply(channel, thread_ts, token, event_id=None, request_id=None,
               urlopen=None, sleep=None):
    """Read the thread and find our reply by its metadata stamp."""
    messages = read_thread(channel, thread_ts, token,
                           urlopen=urlopen, sleep=sleep)
    return find_by_metadata(messages, event_id=event_id,
                            request_id=request_id)


def _print_json(obj):
    print(json.dumps(obj, ensure_ascii=False))


def main(argv):
    channel = thread_ts = message = message_file = None
    event_id = request_id = None
    find_event_id = find_request_id = None
    workspace = None
    event_type = EVENT_TYPE
    dry_run = False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--channel" and i + 1 < len(argv):
            channel = argv[i + 1]; i += 2
        elif a == "--thread-ts" and i + 1 < len(argv):
            thread_ts = argv[i + 1]; i += 2
        elif a == "--message" and i + 1 < len(argv):
            message = argv[i + 1]; i += 2
        elif a == "--message-file" and i + 1 < len(argv):
            message_file = argv[i + 1]; i += 2
        elif a == "--event-id" and i + 1 < len(argv):
            event_id = argv[i + 1]; i += 2
        elif a == "--request-id" and i + 1 < len(argv):
            request_id = argv[i + 1]; i += 2
        elif a == "--find-event-id" and i + 1 < len(argv):
            find_event_id = argv[i + 1]; i += 2
        elif a == "--find-request-id" and i + 1 < len(argv):
            find_request_id = argv[i + 1]; i += 2
        elif a == "--workspace" and i + 1 < len(argv):
            workspace = argv[i + 1]; i += 2
        elif a == "--event-type" and i + 1 < len(argv):
            event_type = argv[i + 1]; i += 2
        elif a == "--dry-run":
            dry_run = True; i += 1
        else:
            print(f"unknown argument: {a}", file=sys.stderr)
            return 1
    if not channel or not thread_ts:
        print("usage: post_metadata.py --channel C... --thread-ts T... "
              "(--message TEXT | --message-file FILE) "
              "(--event-id Ev... | --request-id UUID) [--dry-run] | "
              "(--find-event-id Ev... | --find-request-id UUID)",
              file=sys.stderr)
        return 1
    finding = find_event_id is not None or find_request_id is not None
    if finding:
        if (find_event_id is None) == (find_request_id is None):
            print("find: specify exactly one of --find-event-id / "
                  "--find-request-id", file=sys.stderr)
            return 1
        token = resolve_token(workspace)
        if not token:
            _print_json({"ok": False, "error": "token_not_found"})
            return 1
        try:
            found = find_reply(channel, thread_ts, token,
                               event_id=find_event_id,
                               request_id=find_request_id)
        except RuntimeError as e:
            _print_json({"ok": False, "error": str(e)})
            return 1
        if found:
            _print_json({"ok": True, "found": True, "ts": found.get("ts"),
                         "text": found.get("text", "")})
        else:
            _print_json({"ok": True, "found": False})
        return 0

    if message is not None:
        text = message
    elif message_file:
        try:
            with open(message_file, encoding="utf-8") as f:
                text = f.read()
        except OSError as e:
            _print_json({"ok": False, "error": "read_message: %s" % e})
            return 1
    else:
        print("send: --message or --message-file required", file=sys.stderr)
        return 1
    try:
        payload = build_payload(channel, text, thread_ts,
                                event_id=event_id, request_id=request_id,
                                event_type=event_type)
    except ValueError as e:
        _print_json({"ok": False, "error": str(e)})
        return 1
    if dry_run:
        # Dry run sends nothing and reads no token.
        _print_json({"ok": True, "dry_run": True, "payload": payload})
        return 0
    token = resolve_token(workspace)
    if not token:
        _print_json({"ok": False, "error": "token_not_found"})
        return 1
    ok, result = post(payload, token)
    if ok:
        _print_json({"ok": True, "ts": result.get("ts"),
                     "channel": result.get("channel", channel)})
        return 0
    _print_json({"ok": False, "error": result})
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
