# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""The event stream's button and its telemetry, on the machine.

Its own module, so that no other test's fingerprint moves. The stream is
opened straight at forgectrl's read-only listener, as a client off the
machine would, from a loopback source address of its own so that it takes
a place of its own among the three.
"""

import json
import threading
import time

from ..catalog import test
from .exthost import _stream

SOURCE = "127.0.0.9"


def _reader(sock, seen, stop):
    """Every event on the stream into seen, as (name, data), until stop."""
    buf = b""
    sock.settimeout(1.0)
    while not stop.is_set():
        try:
            k = sock.recv(4096)
        except OSError:
            continue
        if not k:
            break
        buf += k
        while b"\n\n" in buf:
            block, buf = buf.split(b"\n\n", 1)
            name, data = None, None
            for line in block.decode("utf-8", "replace").split("\n"):
                if line.startswith("event: "):
                    name = line[7:]
                elif line.startswith("data: "):
                    data = line[6:]
            if name:
                try:
                    seen.append((name, json.loads(data or "null"), time.time()))
                except ValueError:
                    seen.append((name, data, time.time()))


@test("events.button-telemetry", title="The event stream tells a press of the button, and the telemetry",
      subsystem="forgectrl", kind="auto", mode="grbl", est_min=2,
      covers=[("forgectrl", "src/events.*"), ("forgectrl", "src/main.c"), ("forgectrl", "src/status.*")],
      description="A stream opened at forgectrl's read-only listener gets a telemetry.tick within 12 s, "
                  "and it is what the daemon already holds: the cooling phase, the verdict, fire_ok, the "
                  "two coolant temperatures, the controller's state, and the lid, with no sensor read of "
                  "its own. The machine idle and nothing waiting for the button, one press (the bench "
                  "fixture's, or the operator's) arrives as two button events, pressed true and then false, "
                  "in that order: the button is looked at 25 times a second, so a press shorter than the "
                  "rest of the stream's 5 Hz look is not lost. That a press the machine is waiting for - a "
                  "job arming or armed, a wizard under the lease, the daemon's own wait - is no event is "
                  "forgectrl's events_test's, as are update.available and setup.flag, which a machine in "
                  "use cannot be made to show on demand.")
def button_telemetry(ctx):
    fc = ctx.forgectrl
    ev = ctx.evidence
    ctx.check(fc.wait_idle(timeout=30, abort=ctx.aborted), "machine not idle")
    code, why, sock = _stream("127.0.0.1", keep=True, source=SOURCE)
    ctx.check(code == 200 and sock, "the event stream: %s %s", code, why)
    seen, stop = [], threading.Event()
    th = threading.Thread(target=_reader, args=(sock, seen, stop), daemon=True)
    th.start()
    try:
        ctx.wait_for(lambda: any(n == "telemetry.tick" for n, _d, _t in seen), 14)
        tick = next((d for n, d, _t in seen if n == "telemetry.tick"), None)
        ev["telemetry"] = tick
        ctx.log("telemetry.tick: %s", tick)
        ctx.check(isinstance(tick, dict) and set(tick) == {"phase", "verdict", "fire_ok", "down_c", "up_c", "state", "lid"},
                  "the telemetry's fields: %s", tick)
        ctx.check(tick.get("lid") in ("closed", "open") and isinstance(tick.get("down_c"), (int, float)),
                  "its values: %s", tick)
        mark = len(seen)

        def both():
            got = [d for n, d, _t in seen[mark:] if n == "button"]
            return got if len(got) >= 2 else None
        ctx.act("button", "press", until=both, text="Press the button once: nothing is waiting for it, and "
                "the press is only reported.")
        presses = [d for n, d, _t in seen[mark:] if n == "button"]
        ev["button"] = presses
        ctx.log("button events: %s", presses)
        ctx.check(presses[:2] == [{"pressed": True}, {"pressed": False}], "a press is pressed, then released: %s",
                  presses)
    finally:
        stop.set()
        try:
            sock.close()
        except OSError:
            pass
        th.join(3)
