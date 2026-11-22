#!/usr/bin/env python3
# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT
"""Host-side verification of the controller port and the stream's line
multiplexer.

Runs the native grblHAL_glowforge binary in null-sink mode and drives two
clients at once: a scripted sender on the Grbl TCP socket that counts every
response it gets, and the port's one client on the Unix socket grbl.ctl in
the state directory. The sender's count is the point: a sender synchronizes
on ok and error, so one response too many or too few desynchronizes every
job after it.

  1. a port jog's status goes to the port and never to the sender, which
     sees a [MSG:] and nothing else
  2. a port jog that errors does not leave its error with the sender: with
     COMPATIBILITY_LEVEL 0 the core holds a parser error against the next
     g-code line, and the next line here is the sender's
  3. the sender always wins: a sender line that arrives during a port jog
     cancels the jog, waits, and gets its own ok, never error:9
  4. a port jog is refused while the sender is active, while a program
     runs, and when its words carry anything a jog does not need; a motion
     word the core's jog grammar rejects comes back as the core's error
  5. the port has one client: a second connection is refused and the first
     keeps working (it never displaces, unlike the Grbl socket); a client
     that closes and reconnects at once is served
  6. the status hook survives a soft reset (the core puts its own report
     handlers back at every reset)
  7. the port's client is the dead-man: closing it cancels its jog
  8. every port operation ships dark, and no port jog runs under an open
     armed window. Under an open window, with M3 modal and S above zero,
     every port jog is refused (busy:armed) and moves nothing; the panel
     operations (the tray's mode among them) add not one FIRE tick, and a
     refused motor release says so. The window stays open through every
     one of them up to the home, the home closes it, and the jog after it
     runs dark
 11. the port's panel operations (release, energize, home, the tray's
     mode) and "envelope open" take the jog's sender checks: refused
     (busy:sender) while a line of the sender's runs (a dwell) and within
     0.3 s of its last line, and refused (busy:mcode) while a job waits at
     a package's M-code
 12. a reply goes to the client that asked: a client that hangs up with a
     manual home outstanding (its jog to the origin running) gets none,
     and the client that connects meanwhile gets only its own replies
  9. "sender out" keeps senders from the network out, and only on an idle
     machine with a quiet sender: it is refused while a sender line runs
     (a dwell), within 2 s of the sender's last line, and while a program
     moves; granted, it drops the sender after a message line, outlives
     the port's client, and "sender in" ends it. A sender from the network
     is one that connects to this host's own address rather than the
     loopback; one turned away while senders are kept out is serial_test.c's
     (grblHAL-glowforge)
 10. (no case here) a canceled port jog cannot leave fire behind it: no
     port jog runs under an open window (rule 8), and a jog canceled by
     the sender's line or the hold is laser_stream_test.py's rule 31

A case stops at once when its controller logs the armed clamp (the host
kept the producer off the CPU past its lead, so the stream faulted the run
on purpose and the case did not run). A case that needs a homed machine,
whose controller logged a reference dropped (late step events outside a
window that owed an axis a full step dropped X and Y's or the lens's: the
host starved the producer, and a reference the case stands on is gone), is
judged at a
check that fails: that failure is the starve. A case that passes passes,
whatever its controller logged. A starved case runs once more from new
controllers, and a second starve fails the harness; no other failure runs
a case again. No session here asks for a stall.

Usage: ctlport_test.py [path/to/grblHAL_glowforge]
"""
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

BIN = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "build-native/grblHAL_glowforge")

# Every wait and every interval here is timed on time.monotonic(), the
# clock the controller runs on (CLOCK_MONOTONIC). A host's wall clock can
# step by seconds when its time is synced, and a deadline on it can end a
# wait before the controller has had its time to answer.


def free_port():
    """A TCP port nothing on this host holds: two harness runs on one host
    never meet on a fixed number."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


PORT = free_port()


def fail(msg):
    # A check that fails in a session the host starved fails on the starve:
    # the armed clamp, or a dropped reference the case stood on.
    watch_fail_check()
    print("FAIL: %s" % msg)
    sys.exit(1)


# The controller's line when the producer maps an event behind the ship
# cursor inside an armed window: it missed its lead, and the stream faults
# on purpose (ALARM:17, dark).
CLAMP_LINE = "late events while the laser is armed"

# The start of the controller's line when late step events outside an
# armed window owed an axis a full step and dropped its reference, X and
# Y's or the lens's (the move completes, but steps can have been lost while
# every counter agrees). In a session that asks for no stall and needs a
# homed machine it means the host starved the producer.
DROP_LINE = "gfstream: late step events dropped the "


class HostStarved(Exception):
    """The host kept the producer off the CPU past its lead: inside an
    armed window the stream faulted the run on purpose, or outside one a
    reference a case needs was dropped. The case did not run
    (run_case runs it once more)."""


class StderrWatch:
    """Reads a controller's stderr while it runs, so the pipe never fills
    and blocks it. The session asks for no producer stall, so the clamp
    line means the host starved the producer: check(), which every wait,
    every failure and the session's close call, then raises HostStarved at
    once, rather than leave the case to fail at a later check that the
    fault broke. The drop line in a session that needs a homed machine
    (homed) means the same, but breaks nothing a case has already checked:
    only fail_check(), at a check that fails, raises on it."""

    def __init__(self, proc, name, homed=False):
        self.name = name
        self.armed_check = True
        self.homed = homed
        self.lines = []
        self.clamped = False
        self.dropped = False
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self.read, args=(proc.stderr,), daemon=True)
        self.thread.start()

    def read(self, pipe):
        for raw in iter(pipe.readline, b""):
            line = raw.decode(errors="replace")
            with self.lock:
                self.lines.append(line)
                if CLAMP_LINE in line:
                    self.clamped = True
                if DROP_LINE in line:
                    self.dropped = True

    def text(self):
        with self.lock:
            return "".join(self.lines)

    def fail_check(self):
        """A check failed: a dropped reference the case stands on is the
        starve that failed it; else the armed clamp, as check()."""
        if self.dropped and self.homed and self.armed_check and not self.clamped:
            self.armed_check = False
            raise HostStarved("[%s] host starved the producer: late step events dropped a reference "
                              "the case stands on, and a check failed after it" % self.name)
        self.check()

    def check(self):
        if not (self.clamped and self.armed_check):
            return
        self.armed_check = False
        # The run's summary line, with the worst lag, follows the fault
        # once the stream has ramped down.
        end = time.monotonic() + 2.0
        while True:
            text = self.text()
            m = re.search(r"max behind ([\d.]+) ms", text[text.index(CLAMP_LINE):])
            if m or time.monotonic() >= end:
                break
            time.sleep(0.05)
        raise HostStarved("[%s] host starved the producer (max behind %s ms): the stream faulted "
                          "the armed run on purpose, and the case did not run"
                          % (self.name, m.group(1) if m else "?"))


WATCH = None    # the running controller's StderrWatch


def watch_check():
    if WATCH is not None:
        WATCH.check()


def watch_fail_check():
    if WATCH is not None:
        WATCH.fail_check()


def run_case(case):
    """Run one case. A case the host starved (HostStarved) runs once more,
    from new controllers; a second starve fails the harness. No other
    failure runs a case again."""
    try:
        return case()
    except HostStarved as e:
        print("RERUN: %s" % e)
    try:
        return case()
    except HostStarved as e:
        fail("%s; the rerun was starved too" % e)


# A line that asks for motion: a jog, a home, or a line with an axis word.
MOTION_LINE = re.compile(r"^\$J=|^\$H|(?<![A-Z$])[XYZ][-+]?[\d.]")
# A status report, and the position in it.
REPORT = re.compile(r"<([A-Za-z]+)")
POSITION = re.compile(r"[MW]Pos:([-\d.]+,[-\d.]+,[-\d.]+)")
# A motion the controller took that shows no sign of running this long into
# a wait for Idle (no report in another state, the position where it was)
# is one that had nowhere to go: the wait takes the next Idle.
MOVE_SEEN_S = 1.5
# How long a status poll waits for its report, the '?' sent again every 2 s
# meanwhile (Sender.report): past a host's pause of a few seconds.
REPORT_WAIT_S = 8.0


class Sender:
    """The scripted sender: every line it receives is classified, and the
    responses (ok, error:N) are counted apart from everything else."""

    def __init__(self, port, eol="\n", host="127.0.0.1"):
        self.eol = eol
        self.responses = []         # "ok" / "error:N", in order
        self.other = []             # messages, reports, the banner
        self.lock = threading.Lock()
        self.closed = False         # the controller closed the socket
        # A motion line the controller took and no wait has seen run yet,
        # and the position the last report gave when it was sent.
        self.pending = False
        self.pending_from = None
        for _ in range(50):
            try:
                self.sock = socket.create_connection((host, port), timeout=1)
                break
            except OSError:
                time.sleep(0.1)
        else:
            fail("cannot connect to the controller")
        self.sock.settimeout(0.2)
        self.alive = True
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()
        time.sleep(0.5)             # the banner

    def _read(self):
        buf = b""
        while self.alive:
            try:
                data = self.sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                self.closed = True
                return
            if not data:
                self.closed = True
                return
            buf += data
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                line = raw.decode(errors="replace").strip()
                if not line:
                    continue
                with self.lock:
                    if line == "ok" or line.startswith("error:"):
                        self.responses.append(line)
                    else:
                        self.other.append(line)

    def count(self):
        with self.lock:
            return len(self.responses)

    def send(self, line, timeout=5.0):
        """Send one line and return its response: the one response that
        arrives for it. A motion line answered ok is pending until a wait
        sees it run (wait_state), from where a fresh poll put the head just
        before it went: a frame shift or the port's move since the last
        poll moves the reported position with no motion of this line's."""
        motion = bool(MOTION_LINE.search(line))
        before = self.report()[1] if motion else None
        n = self.count()
        self.sock.sendall((line + self.eol).encode())
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            watch_check()
            if self.count() > n:
                with self.lock:
                    r = self.responses[n]
                if motion and r == "ok":
                    self.pending, self.pending_from = True, before
                return r
            time.sleep(0.01)
        fail("no response to the sender's %r" % line)

    def realtime(self, byte):
        if byte == b"\x18":
            self.pending = False        # a reset drops what the planner held
        self.sock.sendall(byte)

    def report(self):
        """The state and the position (None when the report has none) in
        the answer to a fresh poll. A '?' with no answer in 2 s is sent
        again, for up to REPORT_WAIT_S in all: a reset drops a '?' in
        flight, and a shared host can stop the controller's process for
        seconds (measured: 3.5 s, its watchdogs recording nothing), which is
        no fault of the controller's."""
        with self.lock:
            n = len(self.other)
        self.sock.sendall(b"?")
        start = time.monotonic()
        asked = start
        while time.monotonic() - start < REPORT_WAIT_S:
            watch_check()
            with self.lock:
                for line in self.other[n:]:
                    m = REPORT.match(line)
                    if m:
                        p = POSITION.search(line)
                        return m.group(1), (p.group(1) if p else None)
            if time.monotonic() - asked >= 2.0:
                asked = time.monotonic()
                self.sock.sendall(b"?")
            time.sleep(0.01)
        fail("no status report in %.0f s" % REPORT_WAIT_S)

    def state(self):
        return self.report()[0]

    def wait_state(self, want, timeout=20.0):
        """The state in the answers to fresh polls. Idle counts in two
        answers in a row, and after a motion line the controller took, only
        once the motion has shown: a report in another state, or the
        position away from where it stood when the line went, or
        MOVE_SEEN_S with neither (a move to where the head already is). The
        Idle a jog answers before it starts is not the jog's end."""
        start = time.monotonic()
        end = start + timeout
        need = 2 if want == "Idle" else 1
        seen = 0
        while time.monotonic() < end:
            st, pos = self.report()
            if self.pending and (st != "Idle" or time.monotonic() - start >= MOVE_SEEN_S or
                                 (pos is not None and self.pending_from is not None and pos != self.pending_from)):
                self.pending = False
            seen = seen + 1 if st == want and not (want == "Idle" and self.pending) else 0
            if seen == need:
                return
            time.sleep(0.1)
        fail("the controller never reached %s (now %s)" % (want, self.state()))

    def saw(self, needle):
        with self.lock:
            return sum(needle in line for line in self.other)

    def close(self):
        self.alive = False
        try:
            self.sock.close()
        except OSError:
            pass


class PortClient:
    def __init__(self, path):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(5.0)
        self.sock.connect(path)
        self.buf = b""

    def request(self, line):
        self.sock.sendall((line + "\n").encode())
        return self.reply()

    def reply(self):
        while b"\n" not in self.buf:
            data = self.sock.recv(4096)
            if not data:
                return None
            self.buf += data
        raw, self.buf = self.buf.split(b"\n", 1)
        return raw.decode()

    def state(self):
        return json.loads(self.request("state"))

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def publish_verdicts(path, stop):
    """Stand in for the cooling engine: a clean verdict that acknowledges the
    armed window, refreshed twice a second. A laser line arms against it."""
    while not stop.is_set():
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            f.write('{"ts_mono":%.3f,"fire_ok":true,"verdict":"OK","hold":false,'
                    '"resume_ok":true,"armed":true,"reason":""}'
                    % time.clock_gettime(time.CLOCK_MONOTONIC))
        os.replace(tmp, path)
        stop.wait(0.5)


class Session:
    """One controller. homed: the case needs a homed machine, so a dropped
    homing reference that a check failed after is a starve (StderrWatch).
    env_extra adds to the controller's environment (the host stall knobs;
    a session that asks for a stall is never homed). conf_extra adds lines
    to the settings and state_files puts files in the state directory, both
    before the controller starts (what it reads only there: the microstep
    mode, the lens marker)."""

    def __init__(self, eol="\n", laser=False, name="ctlport", homed=False, env_extra=None,
                 conf_extra="", state_files=None):
        global WATCH
        self.workdir = tempfile.mkdtemp(prefix="ctlport-")
        conf = os.path.join(self.workdir, "forgefirm.conf")
        with open(conf, "w") as f:
            f.write("cool_fan_grace_s = 0\nlaser_disarm_s = 30\nhoming_mode = manual\n" + conf_extra)
        for fname, text in (state_files or {}).items():
            with open(os.path.join(self.workdir, fname), "w") as f:
                f.write(text)
        env = dict(os.environ, GFHOME_CONF=conf, GF_STATE_DIR=self.workdir, FFLOG_STDERR="1")
        env.pop("GFSINK", None)
        env.pop("GF_SWITCH_FILE", None)
        env.update(env_extra or {})
        self.dump = os.path.join(self.workdir, "stream.bin")
        self.stop = threading.Event()
        self.pub = None
        if laser:
            verdict = os.path.join(self.workdir, "cooling.state")
            env.update(GFSINK_DUMP=self.dump, GF_VERDICT_FILE=verdict)
            self.pub = threading.Thread(target=publish_verdicts, args=(verdict, self.stop),
                                        daemon=True)
            self.pub.start()
        env["FORGECTRL_PORT"] = str(free_port())     # no report listener here: a closed port of its own
        self.proc = subprocess.Popen([BIN, "-p", str(PORT)], cwd=self.workdir, env=env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.watch = WATCH = StderrWatch(self.proc, name, homed)
        self.sender = Sender(PORT, eol)
        self.path = os.path.join(self.workdir, "grbl.ctl")
        for _ in range(50):
            if os.path.exists(self.path):
                break
            time.sleep(0.1)
        else:
            fail("the controller did not create %s" % self.path)
        self.port = PortClient(self.path)

    def quiet(self):
        """Past the port's sender-quiet interval."""
        time.sleep(0.45)

    def port_idle(self, timeout=20.0):
        """The port's own jog is over: Idle, and no port jog in hand. A
        jog is judged by its own end, never by an Idle that may come
        before it starts."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            watch_check()
            st = self.port.state()
            if st["state"] == "Idle" and not st["port_jog"]:
                return st
            time.sleep(0.05)
        fail("the port never saw its jog end: %r" % self.port.state())

    def ticks(self):
        """(step ticks, FIRE ticks) shipped so far. A byte with bit 7 set is a
        power byte; in a tick byte 0x25 are the X, Y and Z steps and 0x10 is FIRE."""
        with open(self.dump, "rb") as f:
            data = f.read()
        return (sum(1 for b in data if not b & 0x80 and b & 0x25),
                sum(1 for b in data if not b & 0x80 and b & 0x10))

    def close(self):
        global WATCH
        self.port.close()
        self.sender.close()
        self.proc.send_signal(signal.SIGINT)
        try:
            self.proc.wait(5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.watch.thread.join(2)
        if WATCH is self.watch:
            WATCH = None
        self.stop.set()
        if self.pub is not None:
            self.pub.join(2)
        shutil.rmtree(self.workdir, ignore_errors=True)
        self.watch.check()      # a starve after the case's last wait


def test_socket_mode(s):
    mode = os.stat(s.path).st_mode & 0o777
    if mode != 0o600:
        fail("[mode] grbl.ctl is %o, not 600" % mode)
    print("PASS [mode]: grbl.ctl is a socket with mode 600")


def test_status_routing(s):
    if s.sender.send("G91") != "ok" or s.sender.send("G0 X1") != "ok":
        fail("[routing] the sender's own lines did not get ok")
    s.sender.wait_state("Idle")
    s.quiet()
    before = s.sender.count()
    x0 = s.port.state()["mpos"][0]
    r = s.port.request("jog G91 X2 F600")
    if r != "ok":
        fail("[routing] the port jog got %r, not ok" % r)
    s.port_idle()
    # The sender polled its status all the way through the jog. A realtime
    # character is not a line: it cancels nothing, and the jog ran whole.
    moved = s.port.state()["mpos"][0] - x0
    if abs(moved - 2.0) > 0.01:
        fail("[routing] the sender's status polls cut the port jog short: moved %.3f of 2" % moved)
    if s.sender.count() != before:
        fail("[routing] the sender got a response for the port's line: %r"
             % s.sender.responses[before:])
    if not s.sender.saw("[MSG:Panel jog]"):
        fail("[routing] the sender got no [MSG:] for the port jog")
    if s.sender.send("G0 X1") != "ok":
        fail("[routing] the sender's next line did not get ok")
    print("PASS [routing]: the port jog's ok went to the port and the jog ran whole under the "
          "sender's status polls; the sender saw a [MSG:] and its count is intact")


def test_error_not_inherited(s):
    s.sender.wait_state("Idle")
    s.quiet()
    before = s.sender.count()
    r = s.port.request("jog G91 X1")            # no feed rate: the core refuses it
    if not (r or "").startswith("error:"):
        fail("[error] a jog with no feed rate got %r, not an error" % r)
    if s.sender.count() != before:
        fail("[error] the sender got the port's error: %r" % s.sender.responses[before:])
    r2 = s.sender.send("G0 X1")
    if r2 != "ok":
        fail("[error] the sender's next g-code line got %r: it inherited the port's %s" % (r2, r))
    print("PASS [error]: the port got %s; the sender's next line ran and got ok" % r)


def test_sender_wins(s):
    s.sender.wait_state("Idle")
    s.quiet()
    x0 = s.port.state()["mpos"][0]
    # Fast, so the cancel is a real deceleration: the core stays in the jog
    # state for the length of it, and a sender line read in that window is
    # the error:9 the hold exists to prevent. A slow jog stops at once and
    # would pass with no hold at all.
    if s.port.request("jog G91 X300 F6000") != "ok":    # 3 s if nothing stops it
        fail("[sender-wins] the long port jog was refused")
    time.sleep(0.7)
    st = s.port.state()
    if st["state"] != "Jog" or not st["port_jog"]:
        fail("[sender-wins] not jogging for the port: %r" % st)
    t0 = time.monotonic()
    r = s.sender.send("G0 X1", timeout=10.0)
    dt = time.monotonic() - t0
    if r != "ok":
        fail("[sender-wins] the sender's line during a port jog got %r, not ok" % r)
    s.sender.wait_state("Idle")
    st = s.port.state()
    if st["port_jog"]:
        fail("[sender-wins] the port still claims the jog: %r" % st)
    moved = st["mpos"][0] - x0
    if moved > 200:
        fail("[sender-wins] the port jog was not canceled (X moved %.1f mm)" % moved)
    print("PASS [sender-wins]: the sender's line canceled the port jog, waited %.2f s, "
          "and got ok" % dt)


def test_sender_wins_queued(s):
    """A port jog never queues behind a line of the sender's: during the
    sender's dwell (a line of its own running, Idle to the core) the jog is
    refused, busy:sender, and the sender's dwell and the line it streams
    next each get their ok, the head moved by the sender alone."""
    s.sender.wait_state("Idle")
    s.sender.send("G91")
    x0 = s.port.state()["mpos"][0]
    n = s.sender.count()
    s.sender.sock.sendall(("G4 P1.5" + s.sender.eol).encode())   # no ok until the dwell ends
    s.quiet()
    port_reply = s.port.request("jog G91 X300 F6000")
    time.sleep(0.2)
    s.sender.sock.sendall(("G0 X1" + s.sender.eol).encode())
    end = time.monotonic() + 10.0
    while time.monotonic() < end and s.sender.count() < n + 2:
        time.sleep(0.02)
    got = s.sender.responses[n:n + 2]
    if port_reply != "busy:sender":
        fail("[sender-wins-queued] a port jog during the sender's dwell got %r, not busy:sender" % port_reply)
    if got != ["ok", "ok"]:
        fail("[sender-wins-queued] the sender's dwell and the line after it got %r, not two ok" % got)
    s.sender.wait_state("Idle")
    moved = s.port.state()["mpos"][0] - x0
    if abs(moved - 1.0) > 0.01:
        fail("[sender-wins-queued] X moved %.3f mm, not the sender's 1 mm" % moved)
    print("PASS [sender-wins-queued]: a port jog during the sender's dwell was refused, and the "
          "sender's lines got their ok")


class NewlinePoller(threading.Thread):
    """A sender that polls '?' with an end of line behind it, on a timer,
    as some Grbl senders do. The '?' is a realtime character; the end of line
    is an empty line, which the core answers ok."""

    def __init__(self, sender, period=0.25):
        super().__init__(daemon=True)
        self.sender, self.period = sender, period
        self.polls = 0
        self.halt = threading.Event()

    def run(self):
        while not self.halt.is_set():
            self.sender.sock.sendall(b"?" + self.sender.eol.encode())
            self.polls += 1
            self.halt.wait(self.period)

    def stop(self):
        self.halt.set()
        self.join()


def poll_case(tag, eol):
    """An empty line is not the sender speaking. With a sender polling
    '?'<eol> four times a second: a port jog is never refused for it, a
    long port jog runs whole under it, and every poll still draws its one
    ok, during the jog too. A real line still goes first."""
    s = Session(eol=eol)
    try:
        s.sender.wait_state("Idle")
        s.quiet()
        before = s.sender.count()
        poller = NewlinePoller(s.sender)
        poller.start()
        time.sleep(0.6)
        refused = []
        for i in range(8):                  # spread across the poll period
            r = s.port.request("jog G91 X0.5 F3000")
            if r != "ok":
                refused.append(r)
            time.sleep(0.11)
        if refused:
            fail("[%s] %d of 8 port jogs were refused under a status poll: %s" % (tag, len(refused), refused))
        while s.port.state()["state"] != "Idle":
            time.sleep(0.05)
        x0 = s.port.state()["mpos"][0]
        if s.port.request("jog G91 X60 F3000") != "ok":     # 1.2 s and more: several polls long
            fail("[%s] the long port jog was refused under a status poll" % tag)
        t0 = time.monotonic()
        while time.monotonic() - t0 < 10:
            st = s.port.state()
            if st["state"] == "Idle" and not st["port_jog"]:
                break
            time.sleep(0.05)
        moved = s.port.state()["mpos"][0] - x0
        if abs(moved - 60.0) > 0.02:
            fail("[%s] a status poll cut the port jog short: moved %.3f of 60" % (tag, moved))
        polls_mid = poller.polls
        # A real line still goes first: it cancels a port jog and draws its ok.
        x1 = s.port.state()["mpos"][0]
        if s.port.request("jog G91 X-300 F6000") != "ok":
            fail("[%s] the fast port jog was refused" % tag)
        time.sleep(0.7)
        poller.stop()                       # the real line's response is then the only one outstanding
        time.sleep(0.3)
        n = s.sender.count()
        s.sender.sock.sendall(("G4 P0" + eol).encode())
        end = time.monotonic() + 10
        while time.monotonic() < end and s.sender.count() <= n:
            time.sleep(0.01)
        if s.sender.count() != n + 1 or s.sender.responses[n] != "ok":
            fail("[%s] the sender's real line under a port jog got %r" % (tag, s.sender.responses[n:]))
        s.sender.wait_state("Idle")
        back = x1 - s.port.state()["mpos"][0]
        if back > 250:
            fail("[%s] the sender's real line did not cancel the port jog: it ran %.1f of 300" % (tag, back))
        time.sleep(0.5)
        got = s.sender.count() - before
        want = poller.polls + 1             # one ok per poll's empty line, and the G4
        if got != want:
            fail("[%s] %d polls and one line drew %d responses, not %d" % (tag, poller.polls, got, want))
        print("PASS [%s]: under a '?'+EOL poll 8 of 8 port jogs were accepted, a 60 mm port jog ran whole "
              "(%d polls into it), every poll drew its ok (%d), and a real line still canceled a port jog at "
              "%.1f of 300" % (tag, polls_mid, poller.polls, back))
    finally:
        s.close()


def test_poll_crlf():
    poll_case("poll-crlf", "\r\n")


def test_refusals(s):
    s.sender.wait_state("Idle")
    s.sender.send("G90")
    r = s.port.request("jog G91 X1 F600")               # inside the quiet interval
    if r != "busy:sender":
        fail("[refusals] a jog right after a sender line got %r, not busy:sender" % r)
    s.quiet()
    for words in ("G91 X1 F600 $H", "g91 x1 f600", "G91 X1 F600;M3", ""):
        r = s.port.request("jog " + words)
        if r != "error:invalid":
            fail("[refusals] jog %r got %r, not error:invalid" % (words, r))
    x0 = s.port.state()["mpos"][0]
    r = s.port.request("jog G1 X5 F600")                # a motion mode is not a jog word
    if not (r or "").startswith("error:"):
        fail("[refusals] jog G1 got %r, not the core's error" % r)
    r = s.port.request("jog G91 X1 F600 S500 M3")
    if not (r or "").startswith("error:"):
        fail("[refusals] a jog with S and M3 got %r, not the core's error" % r)
    time.sleep(0.3)
    if abs(s.port.state()["mpos"][0] - x0) > 0.001:
        fail("[refusals] a refused jog moved the machine")
    s.sender.send("G91")
    s.sender.send("G1 X40 F300")                        # a program: 8 s of Run
    s.quiet()
    st = s.port.state()
    r = s.port.request("jog G91 Y1 F600")
    if r != "busy:state":
        fail("[refusals] a jog during a program (%s) got %r, not busy:state" % (st["state"], r))
    s.sender.realtime(b"\x18")                          # end the program
    time.sleep(1.0)
    s.sender.send("$X")
    print("PASS [refusals]: busy:sender inside the quiet interval, error:invalid for stray "
          "characters, the core's error for G1 and for S/M3, busy:state during a program")


PANEL_OPS = ("release", "energize", "home", "tray in", "envelope open")


def test_panel_refusals(s):
    """Rule 11: the panel operations and "envelope open" wait for the
    sender as a jog does, and never run inside a job held at an M-code."""
    s.sender.wait_state("Idle")
    s.quiet()
    n = s.sender.count()
    s.sender.sock.sendall(b"G4 P1.5\n")                # a line of the sender's runs
    time.sleep(0.4)
    got = {op: s.port.request(op) for op in PANEL_OPS + ("jog G91 X1 F600",)}
    if any(r != "busy:sender" for r in got.values()):
        fail("[panel-refusals] during the sender's dwell: %r, wanted busy:sender for each" % got)
    end = time.monotonic() + 5                          # a dwell is Idle: its ok is its end
    while s.sender.count() <= n and time.monotonic() < end:
        time.sleep(0.02)
    if s.sender.count() <= n:
        fail("[panel-refusals] the dwell was never answered")
    got = {}
    for op in PANEL_OPS:
        s.sender.send("G90")
        got[op] = s.port.request(op)
    if any(r != "busy:sender" for r in got.values()):
        fail("[panel-refusals] inside 0.3 s of the sender's line: %r, wanted busy:sender for each" % got)
    s.quiet()
    if s.port.request("mcodes 160") != "ok":
        fail("[panel-refusals] the M-code table was refused")
    s.sender.sock.sendall(b"M160\n")
    end = time.monotonic() + 5
    while s.port.state().get("mcode") is None and time.monotonic() < end:
        time.sleep(0.05)
    seq = (s.port.state().get("mcode") or {}).get("seq")
    if seq is None:
        fail("[panel-refusals] the job never waited at its M-code")
    got = {op: s.port.request(op) for op in PANEL_OPS[:4]}
    if any(r != "busy:mcode" for r in got.values()):
        fail("[panel-refusals] with a job at its M-code: %r, wanted busy:mcode for each" % got)
    if s.port.request("mcode_result %d ok" % seq) != "ok":
        fail("[panel-refusals] the M-code's answer was refused")
    s.sender.wait_state("Idle")
    s.port.request("mcodes -")
    s.quiet()
    print("PASS [panel-refusals]: release, energize, home, the tray and envelope open refused during a "
          "dwell and inside the quiet interval (busy:sender), and at a job's M-code (busy:mcode)")


def test_reply_to_asker():
    """Rule 12: a reply goes to the client that asked."""
    s = Session(name="asker", conf_extra="manual_home_x = 100\n")
    try:
        s.quiet()
        s.port.sock.sendall(b"home\n")                  # its jog to the origin: about a second
        time.sleep(0.2)
        s.port.close()
        s.port = PortClient(s.path)
        r = s.port.request("state")
        if not (r or "").startswith("{"):
            fail("[asker] the client that connected got %r for its state: the gone client's reply" % r)
        s.port_idle()
        print("PASS [asker]: the gone client's home was answered to no one, and the next client got its own state")
    finally:
        s.close()


def test_single_client(s):
    second = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    second.settimeout(3.0)
    second.connect(s.path)
    try:
        second.sendall(b"state\n")
        data = second.recv(100)
    except OSError:
        data = b""
    second.close()
    if data:
        fail("[single-client] a second port client was served: %r" % data)
    if s.port.state().get("state") is None:
        fail("[single-client] the first client stopped working")
    # A client that closes and reconnects at once is not a second client: the
    # port has to notice the hang-up before it judges the new connection.
    for i in range(5):
        s.port.close()
        s.port = PortClient(s.path)
        try:
            ok = s.port.state().get("state") is not None
        except (OSError, TypeError, ValueError):
            ok = False
        if not ok:
            fail("[single-client] reconnect %d right after a close was refused" % (i + 1))
    print("PASS [single-client]: the second connection was refused, the first kept working, and "
          "five reconnects right after a close were each served")


def test_port_dark():
    s = Session(laser=True, name="dark")
    try:
        s.sender.send("G91")
        s.sender.send("M3 S500")
        s.sender.send("G1 X10 F1500")
        s.sender.wait_state("Idle")
        time.sleep(0.5)
        if not s.sender.saw("laser armed"):
            fail("[dark] the laser line did not arm: the case would prove nothing")
        steps0, fire0 = s.ticks()
        if fire0 == 0:
            fail("[dark] the armed G1 shipped no FIRE tick: the dump cannot see emission")
        s.quiet()

        replies = {}
        replies["state"] = s.port.request("state")
        replies["jog"] = s.port.request("jog G91 X8 F1200")
        replies["jog-long"] = s.port.request("jog G91 X200 F6000")
        replies["cancel"] = s.port.request("cancel")
        for words in ("G1 X5 F600", "G91 X5 F600 S900 M3", "G91 X5 F600 M4", "G91 G2 X5 I2 F600"):
            replies["jog " + words] = s.port.request("jog " + words)
        replies["release"] = s.port.request("release")      # refused: a laser job is armed
        replies["energize"] = s.port.request("energize")
        time.sleep(0.3)
        steps_jogs, _ = s.ticks()
        closed_before_home = s.sender.saw("laser disarmed")
        replies["home"] = s.port.request("home")            # manual: declares X0 Y0, moves nothing
        replies["tray out"] = s.port.request("tray out")    # the Z frame moves, the head does not
        replies["tray in"] = s.port.request("tray in")
        replies["jog-after-home"] = s.port.request("jog G91 X6 F1200")
        s.port_idle()
        time.sleep(0.5)

        steps1, fire1 = s.ticks()
        # A job sits at Idle with its window open (the grace after its last
        # move): a port jog would move the head under its next cut, so every
        # one is refused before the core sees it, and nothing moves.
        refused = [k for k in replies if k.startswith("jog") and k != "jog-after-home"]
        if any(replies[k] != "busy:armed" for k in refused) or steps_jogs != steps0:
            fail("[dark] a port jog under the open window was not refused (%d steps): %r"
                 % (steps_jogs - steps0, replies))
        if replies["jog-after-home"] != "ok":
            fail("[dark] the port jog after the home closed the window was refused: %r" % replies)
        if not (replies["release"] or "").startswith("error:"):
            fail("[dark] the motor release under the armed window got %r, not a refusal: %r"
                 % (replies["release"], replies))
        if replies["tray out"] != "ok" or replies["tray in"] != "ok":
            fail("[dark] the tray op under the open window was refused: %r" % replies)
        if steps1 <= steps0:
            fail("[dark] the port jogs shipped no step: the case would prove nothing")
        if closed_before_home:
            fail("[dark] the armed window closed before the home: %r" % replies)
        if not s.sender.saw("laser disarmed"):
            fail("[dark] the home left the armed window open: %r" % replies)
        if fire1 != fire0:
            fail("[dark] port operations under an open window with M3 S500 shipped %d FIRE ticks: %r"
                 % (fire1 - fire0, replies))
        print("PASS [dark]: under an open armed window with M3 S500, %d port operations shipped "
              "%d steps and no FIRE tick" % (len(replies), steps1 - steps0))
    finally:
        s.close()


def test_reset_keeps_hook(s):
    s.sender.realtime(b"\x18")
    time.sleep(1.0)
    s.sender.send("$X")
    s.sender.wait_state("Idle")
    s.quiet()
    before = s.sender.count()
    if s.port.request("jog G91 X1 F600") != "ok":
        fail("[reset] the port jog after a soft reset was refused")
    s.port_idle()
    if s.sender.count() != before:
        fail("[reset] after a soft reset the port's ok went to the sender")
    print("PASS [reset]: the status hook survives a soft reset")


def test_dead_man(s):
    s.sender.wait_state("Idle")
    s.quiet()
    if s.port.request("jog G91 X60 F200") != "ok":
        fail("[dead-man] the long port jog was refused")
    time.sleep(0.5)
    s.port.close()
    t0 = time.monotonic()
    s.sender.wait_state("Idle", timeout=6.0)
    print("PASS [dead-man]: closing the port's client canceled its jog (idle after %.2f s)"
          % (time.monotonic() - t0))
    s.port = PortClient(s.path)
    if s.port.state()["port_jog"]:
        fail("[dead-man] a new client inherited the old jog")


def own_address():
    """This host's own address on the network, which a connection to it
    comes from: a sender from the network, as the controller sees it."""
    u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        u.connect(("192.0.2.1", 9))         # TEST-NET: routes, sends nothing
        addr = u.getsockname()[0]
    except OSError:
        addr = ""
    finally:
        u.close()
    if not addr or addr.startswith("127."):
        fail("[sender-out] this host has no address but the loopback; the test needs one")
    return addr


def wait_closed(sender, what, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end and not sender.closed:
        time.sleep(0.05)
    if not sender.closed:
        fail("[sender-out] %s was not closed" % what)


def test_sender_out():
    s = Session()
    try:
        own = own_address()
        if s.port.state().get("sender_out") is not False:
            fail("[sender-out] the state does not say the sender is let in")
        net = Sender(PORT, host=own)        # displaces the session's own sender
        net.wait_state("Idle")

        # A line that runs: a dwell is Idle to the core, and still the sender's.
        net.sock.sendall(b"G4 P3" + net.eol.encode())
        time.sleep(1.0)
        got = s.port.request("sender out")
        if got != "busy:sender":
            fail("[sender-out] during the sender's dwell: %r, wanted busy:sender" % got)
        end = time.monotonic() + 5
        while net.count() < 1 and time.monotonic() < end:
            time.sleep(0.05)
        # A sender that just finished a line is not done with the machine.
        if net.send("G21") != "ok":
            fail("[sender-out] G21 was not answered ok")
        got = s.port.request("sender out")
        if got != "busy:sender":
            fail("[sender-out] within 2 s of the sender's last line: %r, wanted busy:sender" % got)
        # A program that moves.
        net.sock.sendall(b"G91 G1 X20 F300" + net.eol.encode())
        time.sleep(0.8)
        got = s.port.request("sender out")
        if got != "busy:state":
            fail("[sender-out] while a program moves: %r, wanted busy:state" % got)
        net.wait_state("Idle")
        print("PASS [sender-out]: refused during a dwell, within 2 s of a line, and while a program moves")

        time.sleep(2.3)
        got = s.port.request("sender out")
        if got != "ok":
            fail("[sender-out] an idle machine and a quiet sender: %r, wanted ok" % got)
        wait_closed(net, "the connected sender")
        if not net.saw("[MSG:The machine is in use"):
            fail("[sender-out] the dropped sender was not told why: %r" % net.other[-3:])
        st = s.port.state()
        if st.get("sender_out") is not True or st.get("sender") is not False:
            fail("[sender-out] the state after it: %r" % st)
        print("PASS [sender-out]: granted, it dropped the sender after a message line")

        s.port.close()
        s.port = PortClient(s.path)
        if s.port.state().get("sender_out") is not True:
            fail("[sender-out] it did not outlive the port's client")
        if s.port.request("sender out") != "ok":
            fail("[sender-out] asked again, it was not ok")
        if s.port.request("sender in") != "ok":
            fail("[sender-out] sender in was not ok")
        if s.port.state().get("sender_out") is not False:
            fail("[sender-out] sender in did not end it")
        again = Sender(PORT, host=own)
        if again.closed or again.send("G21") != "ok":
            fail("[sender-out] after sender in, a sender from the network was not let in")
        again.close()
        print("PASS [sender-out]: it outlives the port's client, and sender in lets the network back in")
    finally:
        s.close()


def main():
    if not os.path.exists(BIN):
        fail("no controller binary at %s" % BIN)
    run_case(test_shared_session)
    for case in (test_poll_crlf, test_port_dark, test_reply_to_asker, test_sender_out):
        run_case(case)
    print("ALL PASS")


def test_shared_session():
    """The cases that share one controller."""
    s = Session()
    try:
        test_socket_mode(s)
        test_status_routing(s)
        test_error_not_inherited(s)
        test_sender_wins(s)
        test_sender_wins_queued(s)
        test_refusals(s)
        test_panel_refusals(s)
        test_single_client(s)
        test_reset_keeps_hook(s)
        test_dead_man(s)
    finally:
        s.close()


if __name__ == "__main__":
    main()
