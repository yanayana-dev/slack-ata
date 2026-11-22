#!/usr/bin/env python3
"""Tests for listener.py self-supervision (added 2026-11-22).

Background: on 2026-11-22 the heartbeat froze for ~11 minutes
(09:42:35 to 09:53:44 JST) while the listener process and its
unit stayed "active" on a VM that had been up 54 minutes -- a
wedged process, not a VM replacement. The fix moves the
heartbeat to a dedicated thread, exits the process when the
socket stays disconnected for 6 consecutive 15s ticks (~90s;
systemd Restart=always revives it), and sends systemd watchdog
keepalives only while healthy (the unit sets WatchdogSec=120).

These tests cover the pure pieces: HealthTracker's tick
accounting, sd_notify's datagram behavior with a fake socket,
supervision_tick's wiring (heartbeat written every tick, ping
only while healthy, exit decision), and heartbeat_payload's
shape. No real socket, systemd, or Slack connection is touched:
the heartbeat path is redirected to a temp file.

Plain stdlib script, no pytest (same style as the other suites).
Run:
  python3 tests/test_listener_supervision.py
Exit 0 when every check passes; each check prints PASS/FAIL.
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import listener

CHECKS = [0, 0]  # [passed, failed]


def check(name, cond):
    CHECKS[0 if cond else 1] += 1
    print(f"{'PASS' if cond else 'FAIL'}: {name}")


# --- HealthTracker -------------------------------------------------------

t = listener.HealthTracker(exit_after_ticks=3)
check("tracker starts not-exitable", not t.should_exit)
t.observe(False)
t.observe(False)
t.observe(False)
check("no exit before first healthy observation", not t.should_exit)
check("unhealthy ticks counted", t.unhealthy_ticks == 3)
t.observe(True)
check("healthy observation resets the count",
      t.healthy and t.unhealthy_ticks == 0 and t.ever_healthy)
t.observe(False)
t.observe(False)
check("below threshold: no exit", not t.should_exit)
t.observe(False)
check("at threshold after having been healthy: exit", t.should_exit)
t.observe(True)
check("recovery clears the exit decision", not t.should_exit)

t = listener.HealthTracker()
check("default threshold is 6 ticks (~90s)", t.exit_after_ticks == 6)


# --- sd_notify -----------------------------------------------------------

class FakeSock:
    def __init__(self):
        self.sent = []
        self.closed = False

    def sendto(self, data, addr):
        self.sent.append((data, addr))

    def close(self):
        self.closed = True


made = []


def fake_factory(family, socktype):
    made.append((family, socktype))
    return made_sock


made_sock = FakeSock()
ok = listener.sd_notify("WATCHDOG=1",
                        environ={"NOTIFY_SOCKET": "/run/systemd/notify"},
                        sock_factory=fake_factory)
check("sd_notify sends to a filesystem socket address",
      ok and made_sock.sent == [(b"WATCHDOG=1", "/run/systemd/notify")]
      and made_sock.closed)
import socket as _socket
check("sd_notify uses AF_UNIX/SOCK_DGRAM",
      made == [(_socket.AF_UNIX, _socket.SOCK_DGRAM)])

made_sock = FakeSock()
ok = listener.sd_notify("WATCHDOG=1",
                        environ={"NOTIFY_SOCKET": "@/run/abstract"},
                        sock_factory=lambda f, s: made_sock)
check("abstract socket address gets a NUL prefix",
      ok and made_sock.sent == [(b"WATCHDOG=1", "\0/run/abstract")])

check("no NOTIFY_SOCKET: no-op",
      listener.sd_notify("WATCHDOG=1", environ={},
                         sock_factory=fake_factory) is False)


class BoomSock:
    def sendto(self, data, addr):
        raise OSError("gone")

    def close(self):
        pass


check("send failure is swallowed and reported as False",
      listener.sd_notify("WATCHDOG=1",
                         environ={"NOTIFY_SOCKET": "/run/x"},
                         sock_factory=lambda f, s: BoomSock()) is False)


# --- heartbeat_payload ---------------------------------------------------

NOW = datetime(2026, 11, 22, 0, 42, 35, tzinfo=timezone.utc)


class FakeSession:
    last_ping_pong_time = NOW.timestamp() - 7.5


class FakeClient:
    current_session = FakeSession()

    def is_connected(self):
        return True


p = listener.heartbeat_payload(FakeClient(), now=NOW)
check("payload carries at/connected/pid",
      p["at"] == NOW.isoformat() and p["connected"] is True
      and p["pid"] == os.getpid())
check("payload reports pong age from the session",
      p.get("last_pong_age_s") == 7.5)


class BareClient:
    current_session = None

    def is_connected(self):
        return False


p = listener.heartbeat_payload(BareClient(), now=NOW)
check("payload without a session omits pong age",
      p["connected"] is False and "last_pong_age_s" not in p)


class NoMethodClient:
    pass


p = listener.heartbeat_payload(NoMethodClient(), now=NOW)
check("payload tolerates a client without is_connected",
      p["connected"] is False)


# --- supervision_tick ----------------------------------------------------

pings = []


def fake_notify(msg):
    pings.append(msg)
    return True


class FlipClient:
    def __init__(self):
        self.connected = True

    def is_connected(self):
        return self.connected


with tempfile.TemporaryDirectory() as d:
    hb_path = os.path.join(d, "heartbeat.json")
    old_path = listener.HEARTBEAT_PATH
    listener.HEARTBEAT_PATH = hb_path
    try:
        client = FlipClient()
        tracker = listener.HealthTracker(exit_after_ticks=3)
        check("healthy tick: no exit",
              listener.supervision_tick(client, tracker,
                                        notify=fake_notify) is False)
        with open(hb_path, encoding="utf-8") as f:
            hb = json.load(f)
        check("healthy tick wrote a connected heartbeat",
              hb["connected"] is True)
        check("healthy tick pinged the systemd watchdog",
              pings == [listener.SD_WATCHDOG_PING])

        client.connected = False
        check("first unhealthy tick: no exit yet",
              listener.supervision_tick(client, tracker,
                                        notify=fake_notify) is False)
        check("unhealthy tick does not ping",
              pings == [listener.SD_WATCHDOG_PING])
        with open(hb_path, encoding="utf-8") as f:
            hb = json.load(f)
        check("unhealthy tick still writes the heartbeat",
              hb["connected"] is False)
        listener.supervision_tick(client, tracker, notify=fake_notify)
        check("third consecutive unhealthy tick: exit",
              listener.supervision_tick(client, tracker,
                                        notify=fake_notify) is True)

        client.connected = True
        check("recovery tick: no exit, count reset",
              listener.supervision_tick(client, tracker,
                                        notify=fake_notify) is False
              and tracker.unhealthy_ticks == 0)

        class RaisingClient:
            def is_connected(self):
                raise RuntimeError("wedged")

        tracker2 = listener.HealthTracker(exit_after_ticks=1)
        tracker2.observe(True)
        check("is_connected raising counts as disconnected",
              listener.supervision_tick(RaisingClient(), tracker2,
                                        notify=fake_notify) is True)
    finally:
        listener.HEARTBEAT_PATH = old_path

print(f"\n{CHECKS[0]} passed, {CHECKS[1]} failed")
sys.exit(1 if CHECKS[1] else 0)
