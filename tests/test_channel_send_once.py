#!/usr/bin/env python3
"""Tests for channel_send_once.py (bounded channel-post dedupe).

Adopted 2026-11-12 from Neiro's share in #slack-へようこそ: Neiro took
Yanayana's bounded-check proposal (last 50 channel messages) into
their slack-send-once.sh; this helper brings the same check to
Yanayana's own channel posts, which reply_once.py (thread replies
only) did not cover.

Plain stdlib script, no pytest (same style as
test_reply_once_recovery.py). Run:
  python3 tests/test_channel_send_once.py
Exit 0 when every check passes; each check prints PASS/FAIL.

The module's send / channel-read functions are stubbed and the record
files are redirected to a temp dir, so no real Slack call is made and
the real events/ directory is never touched.
"""
import importlib.util
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SPEC = importlib.util.spec_from_file_location(
    "channel_send_once", os.path.join(ROOT, "channel_send_once.py"))
cso = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cso)
ORIG_LAST = cso.channel_last_yanayana_texts  # real fn; setup() stubs it
ORIG_SENT = cso.already_sent  # real fn; some tests stub it
ORIG_READ = cso.channel_read_page_retry  # real fn; setup() restores it

CHECKS = [0, 0]  # [passed, failed]


def check(name, cond, detail=""):
    CHECKS[0 if cond else 1] += 1
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not cond else ""))
    return cond


MSG = "test channel post"
ARGS = ["--channel", "C0TEST", "--message", MSG]
CHECK_ARGS = ["--channel", "C0TEST", "--message", MSG, "--check-only"]


def setup(tmp, name):
    sub = os.path.join(tmp, name)
    os.makedirs(sub, exist_ok=True)
    cso.POSTS = os.path.join(sub, "channel_posts.jsonl")
    cso.AMBIGUOUS = os.path.join(sub, "ambiguous_channel_posts.jsonl")
    cso.EVENTS = sub
    cso.already_sent = ORIG_SENT
    cso.channel_last_yanayana_texts = lambda *a, **k: []
    # The other-author duplicate check reads the channel itself, so
    # stub that read too: an empty page means "no other author has
    # this text". Tests that exercise the real read restore ORIG_READ.
    cso.channel_read_page_retry = lambda *a, **k: {"messages": []}
    cso.send = lambda *a, **k: "444.4"


def read_jsonl(path):
    try:
        with open(path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        return []


def test_check_only_duplicate_in_channel(tmp):
    """check-only: text in last 50 -> already-sent, no send."""
    setup(tmp, "dup_channel")
    cso.channel_last_yanayana_texts = lambda *a, **k: ["other", MSG]
    sent = []
    cso.send = lambda *a, **k: sent.append(True) or "444.4"
    rc = cso.main(list(CHECK_ARGS))
    check("check-only + text in channel -> exit 0", rc == 0, f"rc={rc}")
    check("check-only + text in channel -> no send attempted", sent == [])


def test_check_only_new_text(tmp):
    """check-only: text absent -> not-sent."""
    setup(tmp, "new_text")
    rc = cso.main(list(CHECK_ARGS))
    check("check-only + new text -> exit 0", rc == 0, f"rc={rc}")


def test_normal_send(tmp):
    """Normal send records the post and exits 0."""
    setup(tmp, "normal")
    rc = cso.main(list(ARGS))
    recs = read_jsonl(cso.POSTS)
    check("normal send -> exit 0", rc == 0, f"rc={rc}")
    check("normal send -> record with post_ts",
          len(recs) == 1 and recs[0].get("post_ts") == "444.4"
          and recs[0].get("recovered") is False)
    check("normal send -> no ambiguous record",
          read_jsonl(cso.AMBIGUOUS) == [])


def test_record_hit_blocks_without_live_read(tmp):
    """Record hit short-circuits: live read raising still blocks."""
    setup(tmp, "record_hit")
    cso.channel_last_yanayana_texts = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("read failed"))
    cso.record_post("C0TEST", MSG, "444.4")
    sent = []
    cso.send = lambda *a, **k: sent.append(True) or "444.4"
    rc = cso.main(list(ARGS))
    check("record hit -> exit 0", rc == 0, f"rc={rc}")
    check("record hit -> no send attempted", sent == [])


def test_cannot_verify(tmp):
    """Live read fails and no record -> exit 2, no blind send."""
    setup(tmp, "cannot_verify")
    cso.channel_last_yanayana_texts = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("read failed"))
    sent = []
    cso.send = lambda *a, **k: sent.append(True) or "444.4"
    rc = cso.main(list(ARGS))
    check("cannot-verify -> exit 2", rc == 2, f"rc={rc}")
    check("cannot-verify -> no send attempted", sent == [])
    check("cannot-verify -> no record written",
          read_jsonl(cso.POSTS) == [])


def test_send_lost_post_found(tmp):
    """Send raises, recovery finds the text -> delivered, exit 0."""
    setup(tmp, "found")
    # Bypass the pre-send check (as test_reply_once_recovery.py does):
    # the stubbed live channel already "contains" the text, which is
    # exactly what the recovery check must find.
    cso.already_sent = lambda *a, **k: None
    cso.send = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("timeout"))
    cso.channel_last_yanayana_texts = lambda *a, **k: [MSG]
    rc = cso.main(list(ARGS))
    recs = read_jsonl(cso.POSTS)
    check("lost-send + post found -> exit 0", rc == 0, f"rc={rc}")
    check("lost-send + post found -> recovered record",
          len(recs) == 1 and recs[0].get("recovered") is True)
    check("lost-send + post found -> no ambiguous record",
          read_jsonl(cso.AMBIGUOUS) == [])


def test_send_lost_post_absent(tmp):
    """Send raises, recovery confirms absent -> ambiguous, exit 3."""
    setup(tmp, "absent")
    cso.send = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("timeout"))
    rc = cso.main(list(ARGS))
    amb = read_jsonl(cso.AMBIGUOUS)
    check("lost-send + post absent -> exit 3", rc == 3, f"rc={rc}")
    check("lost-send + post absent -> ambiguous record",
          len(amb) == 1 and amb[0].get("outcome") == "ambiguous_post_outcome")
    check("lost-send + post absent -> no delivered record",
          read_jsonl(cso.POSTS) == [])


def test_send_lost_reads_fail(tmp):
    """Send raises, recovery reads fail -> ambiguous, exit 3."""
    setup(tmp, "reads_fail")
    cso.already_sent = lambda *a, **k: None
    cso.send = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("timeout"))
    cso.channel_last_yanayana_texts = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("read failed"))
    rc = cso.main(list(ARGS))
    amb = read_jsonl(cso.AMBIGUOUS)
    check("lost-send + reads fail -> exit 3", rc == 3, f"rc={rc}")
    check("lost-send + reads fail -> ambiguous record",
          len(amb) == 1 and "could not verify" in amb[0].get("reason", ""))
    check("lost-send + reads fail -> no delivered record",
          read_jsonl(cso.POSTS) == [])


def test_duplicate_of_other_blocks(tmp):
    """Other author's identical text in last 50 -> exit 4, no send.

    Parroting guard for parent posts (2026-11-14, from Yama's
    question in #slack-へようこそ): the thread side already had it,
    the channel side only looked at Yanayana's own posts.
    """
    setup(tmp, "dup_other")
    page = {"messages": [{"user": "UOTHER", "text": MSG, "ts": "111.1"},
                         {"user": cso.YANAYANA_USER, "text": "mine"}]}
    cso.channel_read_page_retry = lambda *a, **k: page
    sent = []
    cso.send = lambda *a, **k: sent.append(True) or "444.4"
    rc = cso.main(list(ARGS))
    check("duplicate of other author -> exit 4", rc == 4, f"rc={rc}")
    check("duplicate of other author -> no send attempted", sent == [])
    check("duplicate of other author -> no record written",
          read_jsonl(cso.POSTS) == [])
    check("duplicate of other author -> no ambiguous record",
          read_jsonl(cso.AMBIGUOUS) == [])


def test_duplicate_of_other_scope(tmp):
    """Other-author check skips Yanayana's own posts, ignores
    different text, and matches after stripping whitespace."""
    setup(tmp, "dup_other_scope")
    page = {"messages": [{"user": cso.YANAYANA_USER, "text": MSG, "ts": "111.1"},
                         {"user": "UOTHER", "text": "something else"}]}
    cso.channel_read_page_retry = lambda *a, **k: page
    check("other-author check skips own identical text",
          cso.channel_duplicate_of_other("C0TEST", MSG) is None)
    check("other-author check ignores different text",
          cso.channel_duplicate_of_other("C0TEST", "brand new") is None)
    page2 = {"messages": [{"user": "UOTHER", "text": "  " + MSG + "\n",
                           "ts": "222.2"}]}
    cso.channel_read_page_retry = lambda *a, **k: page2
    check("other-author check strips whitespace",
          cso.channel_duplicate_of_other("C0TEST", MSG) == "222.2")


def test_duplicate_check_read_failure(tmp):
    """Other-author check read fails -> exit 2, no blind send."""
    setup(tmp, "dup_read_fail")
    cso.channel_read_page_retry = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("read failed"))
    sent = []
    cso.send = lambda *a, **k: sent.append(True) or "444.4"
    rc = cso.main(list(ARGS))
    check("duplicate check read fails -> exit 2", rc == 2, f"rc={rc}")
    check("duplicate check read fails -> no send attempted", sent == [])
    check("duplicate check read fails -> no record written",
          read_jsonl(cso.POSTS) == [])


def test_single_call_latest_50(tmp):
    """One read with --limit 50 is the whole bounded window.

    Yama's 2026-11-12 question showed the 20-page oldest-first scan
    was unnecessary: slackcli returns the latest N messages in one
    call. The real read fn must therefore issue exactly one call,
    with --limit 50, and return only Yanayana's texts from that page.
    """
    setup(tmp, "single_call")
    cso.channel_last_yanayana_texts = ORIG_LAST  # test the real fn
    cso.channel_read_page_retry = ORIG_READ  # test the real read fn
    page = {"messages": [{"user": cso.YANAYANA_USER, "text": "mine-1"},
                         {"user": "UOTHER", "text": "not-mine"},
                         {"user": cso.YANAYANA_USER, "text": "mine-2"}],
            "has_more": True, "next_oldest": "99.0"}
    calls = {"n": 0, "argv": None}

    class FakeRun:
        def __init__(self, argv):
            self.stdout = "- Fetching...\n" + json.dumps(page)
            self.stderr = ""
            self.returncode = 0

    real_run = cso.subprocess.run

    def fake_run(argv, **kw):
        calls["n"] += 1
        calls["argv"] = list(argv)
        return FakeRun(argv)

    cso.subprocess.run = fake_run
    try:
        got = cso.channel_last_yanayana_texts("C0TEST")
    finally:
        cso.subprocess.run = real_run
    check("single call -> exactly one read", calls["n"] == 1, f"n={calls['n']}")
    check("single call -> --limit 50 requested",
          calls["argv"] is not None and "--limit" in calls["argv"]
          and calls["argv"][calls["argv"].index("--limit") + 1] == "50",
          f"argv={calls['argv']}")
    check("single call -> only Yanayana's texts returned",
          got == ["mine-1", "mine-2"], f"got={got}")
    check("single call -> does not follow has_more to a next page",
          calls["n"] == 1)


def main():
    with tempfile.TemporaryDirectory() as tmp:
        test_check_only_duplicate_in_channel(tmp)
        test_check_only_new_text(tmp)
        test_normal_send(tmp)
        test_record_hit_blocks_without_live_read(tmp)
        test_cannot_verify(tmp)
        test_send_lost_post_found(tmp)
        test_send_lost_post_absent(tmp)
        test_send_lost_reads_fail(tmp)
        test_duplicate_of_other_blocks(tmp)
        test_duplicate_of_other_scope(tmp)
        test_duplicate_check_read_failure(tmp)
        test_single_call_latest_50(tmp)
    print(f"\n{CHECKS[0]} passed, {CHECKS[1]} failed")
    return 0 if CHECKS[1] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
