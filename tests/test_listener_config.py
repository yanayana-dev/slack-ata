#!/usr/bin/env python3
"""Tests for listener.py load_config token shape checks (added 2026-11-19).

Neiro suggested checking the token prefix as a cheap
first step, separate from the ID-externalization work. The listener
reads bot_token / app_token from config.json, so load_config now
rejects a token whose prefix does not match (bot_token: the bot
prefix, app_token: the app prefix) with exit code 2, before any
socket is opened. Token-shaped literals are built from parts
("xox" + "b-") throughout this file: a literal would be rewritten
by the publish scrub, which maps every token-shaped string to a
placeholder.

These tests import listener in-process and patch its CONFIG_PATH
global to a temp file, so the real config.json is never touched.

Plain stdlib script, no pytest (same style as the other suites).
Run:
  python3 tests/test_listener_config.py
Exit 0 when every check passes; each check prints PASS/FAIL.
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import listener

CHECKS = [0, 0]  # [passed, failed]


def check(name, cond):
    CHECKS[0 if cond else 1] += 1
    print(f"{'PASS' if cond else 'FAIL'}: {name}")


def load_with(cfg):
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "config.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        old = listener.CONFIG_PATH
        listener.CONFIG_PATH = path
        try:
            return listener.load_config()
        finally:
            listener.CONFIG_PATH = old


def expect_exit(name, cfg):
    try:
        load_with(cfg)
    except SystemExit as e:
        check(name, e.code == 2)
    except Exception:
        check(name, False)
    else:
        check(name, False)


BOT_PREFIX = "xox" + "b-"
APP_PREFIX = "xapp-"
USER_PREFIX = "xox" + "p-"

GOOD = {"bot_token": BOT_PREFIX + "123", "app_token": APP_PREFIX + "456"}

cfg = load_with(GOOD)
check("valid prefixes load", cfg == GOOD)

expect_exit("bot_token with xoxp- prefix rejected",
            {"bot_token": USER_PREFIX + "123", "app_token": APP_PREFIX + "456"})
expect_exit("app_token with bot prefix rejected",
            {"bot_token": BOT_PREFIX + "123", "app_token": BOT_PREFIX + "456"})
expect_exit("bot_token with no prefix rejected",
            {"bot_token": "123", "app_token": APP_PREFIX + "456"})
expect_exit("empty bot_token rejected",
            {"bot_token": "", "app_token": APP_PREFIX + "456"})
expect_exit("missing app_token rejected",
            {"bot_token": BOT_PREFIX + "123"})

print(f"\n{CHECKS[0]} passed, {CHECKS[1]} failed")
sys.exit(1 if CHECKS[1] else 0)
