#!/usr/bin/env python3
"""Bot-to-bot round-trip guard (Yama's rule, adopted 2026-11-12):
in a thread, bots may exchange at most 3 round trips without a human
post in between. Before Yanayana replies to a bot in a thread, run:

  botguard.py --channel <C...> --thread-ts <ts>

Prints JSON: {"decision": "ALLOW"|"BLOCK", "bot_stretch": n,
"yanayana_posts": m, "reason": "..."}
bot_stretch = number of consecutive bot messages at the end of the
thread (since the last human message). BLOCK when the stretch is
already >= 6 bot messages (3 round trips) or Yanayana itself has
posted >= 3 times within the stretch.
On any error the guard fails open (ALLOW) and says so in "reason",
so a guard bug never silences a legitimate reply.
"""
import argparse
import json
import os
import subprocess
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
SLACKCLI = os.path.join(BASE, "bin", "slackcli")
OWN_BOT_USER = "U0EXAMPLEBOT"  # yanayana
MAX_STRETCH = 6       # 3 round trips = 6 bot posts
MAX_OWN_IN_STRETCH = 3


def fail_open(reason):
    print(json.dumps({"decision": "ALLOW", "bot_stretch": None,
                      "yanayana_posts": None, "reason": reason}))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", required=True)
    ap.add_argument("--thread-ts", required=True)
    args = ap.parse_args()

    try:
        out = subprocess.run(
            [SLACKCLI, "conversations", "read", args.channel,
             "--thread-ts", args.thread_ts, "--limit", "50", "--json"],
            capture_output=True, text=True, timeout=60,
        ).stdout
        start = out.find("{")
        data = json.loads(out[start:])
        msgs = data.get("messages", [])
    except Exception as e:
        return fail_open(f"guard check failed: {e}")

    msgs = sorted(msgs, key=lambda m: float(m.get("ts", 0)))
    stretch = 0
    own = 0
    for m in reversed(msgs):
        if m.get("bot_id"):
            stretch += 1
            if m.get("user") == OWN_BOT_USER:
                own += 1
        else:
            break

    if stretch >= MAX_STRETCH or own >= MAX_OWN_IN_STRETCH:
        decision, reason = "BLOCK", "bot round-trip limit reached (3 exchanges without a human post)"
    else:
        decision, reason = "ALLOW", "within bot round-trip limit"
    print(json.dumps({"decision": decision, "bot_stretch": stretch,
                      "yanayana_posts": own, "reason": reason}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
