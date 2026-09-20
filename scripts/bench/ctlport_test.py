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
  6. a CR LF sender keeps its count across a port jog, an empty line
     included
  7. the status hook survives a soft reset (the core puts its own report
     handlers back at every reset)
  8. the port's client is the dead-man: closing it cancels its jog
  9. every port operation ships dark. Under an open armed window, with M3
     modal and S above zero, a port jog, a canceled port jog, the refused
     jogs, and the panel operations add steps to the shipped stream and not
     one FIRE tick. A jog block carries the modal spindle state, so the
     stream's mask on jogging is the only thing that keeps a jog dark, and
     this is what holds the port to it

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
PORT = 2397


def fail(msg):
    print("FAIL: %s" % msg)
    sys.exit(1)


class Sender:
    """The scripted sender: every line it receives is classified, and the
    responses (ok, error:N) are counted apart from everything else."""

    def __init__(self, port, eol="\n"):
        self.eol = eol
        self.responses = []         # "ok" / "error:N", in order
        self.other = []             # messages, reports, the banner
        self.lock = threading.Lock()
        for _ in range(50):
            try:
                self.sock = socket.create_connection(("127.0.0.1", port), timeout=1)
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
                return
            if not data:
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
        arrives for it."""
        n = self.count()
        self.sock.sendall((line + self.eol).encode())
        end = time.time() + timeout
        while time.time() < end:
            if self.count() > n:
                with self.lock:
                    return self.responses[n]
            time.sleep(0.01)
        fail("no response to the sender's %r" % line)

    def realtime(self, byte):
        self.sock.sendall(byte)

    def state(self):
        with self.lock:
            n = len(self.other)
        self.sock.sendall(b"?")
        end = time.time() + 2.0
        while time.time() < end:
            with self.lock:
                for line in self.other[n:]:
                    m = re.match(r"<([A-Za-z]+)", line)
                    if m:
                        return m.group(1)
            time.sleep(0.01)
        fail("no status report")

    def wait_state(self, want, timeout=20.0):
        end = time.time() + timeout
        while time.time() < end:
            if self.state() == want:
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
    def __init__(self, eol="\n", laser=False):
        self.workdir = tempfile.mkdtemp(prefix="ctlport-")
        conf = os.path.join(self.workdir, "forgefirm.conf")
        with open(conf, "w") as f:
            f.write("cool_fan_grace_s = 0\nlaser_disarm_s = 30\nhoming_mode = manual\n")
        env = dict(os.environ, GFHOME_CONF=conf, GF_STATE_DIR=self.workdir, FFLOG_STDERR="1")
        env.pop("GFSINK", None)
        env.pop("GF_SWITCH_FILE", None)
        self.dump = os.path.join(self.workdir, "stream.bin")
        self.stop = threading.Event()
        self.pub = None
        if laser:
            verdict = os.path.join(self.workdir, "cooling.state")
            env.update(GFSINK_DUMP=self.dump, GF_VERDICT_FILE=verdict)
            self.pub = threading.Thread(target=publish_verdicts, args=(verdict, self.stop),
                                        daemon=True)
            self.pub.start()
        self.proc = subprocess.Popen([BIN, "-p", str(PORT)], cwd=self.workdir, env=env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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

    def ticks(self):
        """(step ticks, FIRE ticks) shipped so far. A byte with bit 7 set is a
        power byte; in a tick byte 0x25 are the X, Y and Z steps and 0x10 is FIRE."""
        with open(self.dump, "rb") as f:
            data = f.read()
        return (sum(1 for b in data if not b & 0x80 and b & 0x25),
                sum(1 for b in data if not b & 0x80 and b & 0x10))

    def close(self):
        self.port.close()
        self.sender.close()
        self.proc.send_signal(signal.SIGINT)
        try:
            self.proc.wait(5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.stop.set()
        if self.pub is not None:
            self.pub.join(2)
        shutil.rmtree(self.workdir, ignore_errors=True)


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
    s.sender.wait_state("Idle")
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
    t0 = time.time()
    r = s.sender.send("G0 X1", timeout=10.0)
    dt = time.time() - t0
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
    """The window the read gate exists for. The core reads a line's bytes
    in one pass and polls the stream only at the end of line, so a sender
    line that sits in the ring right behind the port's jog line is read
    while the jog it follows is still running, with no poll in between to
    cancel it. A dwell opens the window on purpose: the port's line is
    queued during it, and the sender streams its next line behind that."""
    s.sender.wait_state("Idle")
    s.sender.send("G91")
    n = s.sender.count()
    s.sender.sock.sendall(("G4 P1.5" + s.sender.eol).encode())   # no ok until the dwell ends
    s.quiet()
    s.port.sock.sendall(b"jog G91 X300 F6000\n")                  # queued behind the dwell
    time.sleep(0.2)
    s.sender.sock.sendall(("G0 X1" + s.sender.eol).encode())      # streamed behind the port's line
    end = time.time() + 10.0
    while time.time() < end and s.sender.count() < n + 2:
        time.sleep(0.02)
    got = s.sender.responses[n:n + 2]
    port_reply = s.port.reply()
    if port_reply != "ok":
        fail("[sender-wins-queued] the queued port jog got %r, not ok" % port_reply)
    if got != ["ok", "ok"]:
        fail("[sender-wins-queued] the sender's dwell and the line behind the port jog got %r, "
             "not two ok" % got)
    s.sender.wait_state("Idle")
    print("PASS [sender-wins-queued]: a sender line queued right behind the port's jog line "
          "waited for the cancel and got ok")


class NewlinePoller(threading.Thread):
    """A sender that polls the way LightBurn does: '?' with an end of line
    behind it, on a timer. The '?' is a realtime character; the end of line
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
    '?'<eol> four times a second (LightBurn polls about twice a second): a
    port jog is never refused for it, a long port jog runs whole under it,
    and every poll still draws its one ok, during the jog too. A real line
    still goes first."""
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
        t0 = time.time()
        while time.time() - t0 < 10:
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
        end = time.time() + 10
        while time.time() < end and s.sender.count() <= n:
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


def test_poll_lf():
    poll_case("poll-lf", "\n")


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


def test_crlf():
    s = Session(eol="\r\n")
    try:
        for line in ("G91", "G0 X1", ""):
            if s.sender.send(line) != "ok":
                fail("[crlf] %r did not get ok" % line)
        s.sender.wait_state("Idle")
        s.quiet()
        before = s.sender.count()
        if s.port.request("jog G91 X1 F600") != "ok":
            fail("[crlf] the port jog was refused")
        s.sender.wait_state("Idle")
        for line in ("", "G0 X1", "", ""):
            if s.sender.send(line) != "ok":
                fail("[crlf] %r after the port jog did not get ok" % line)
        time.sleep(0.5)
        got = s.sender.count() - before
        if got != 4:
            fail("[crlf] 4 sender lines after the port jog got %d responses" % got)
        print("PASS [crlf]: a CR LF sender's count is exact across a port jog, empty lines included")
    finally:
        s.close()


def test_port_dark():
    s = Session(laser=True)
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
        s.sender.wait_state("Idle")
        replies["jog-long"] = s.port.request("jog G91 X200 F6000")
        time.sleep(0.4)
        replies["cancel"] = s.port.request("cancel")
        s.sender.wait_state("Idle")
        for words in ("G1 X5 F600", "G91 X5 F600 S900 M3", "G91 X5 F600 M4", "G91 G2 X5 I2 F600"):
            replies["jog " + words] = s.port.request("jog " + words)
        replies["release"] = s.port.request("release")      # refused: a laser job is armed
        replies["energize"] = s.port.request("energize")
        replies["home"] = s.port.request("home")            # manual: declares X0 Y0, moves nothing
        replies["jog-after-home"] = s.port.request("jog G91 X6 F1200")
        s.sender.wait_state("Idle")
        time.sleep(0.5)

        steps1, fire1 = s.ticks()
        if replies["jog"] != "ok" or replies["jog-long"] != "ok" or replies["jog-after-home"] != "ok":
            fail("[dark] a port jog under the open window was refused: %r" % replies)
        if steps1 <= steps0:
            fail("[dark] the port jogs shipped no step: the case would prove nothing")
        if s.sender.saw("laser disarmed"):
            fail("[dark] the armed window closed during the case: %r" % replies)
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
    s.sender.wait_state("Idle")
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
    t0 = time.time()
    s.sender.wait_state("Idle", timeout=6.0)
    print("PASS [dead-man]: closing the port's client canceled its jog (idle after %.2f s)"
          % (time.time() - t0))
    s.port = PortClient(s.path)
    if s.port.state()["port_jog"]:
        fail("[dead-man] a new client inherited the old jog")


def main():
    if not os.path.exists(BIN):
        fail("no controller binary at %s" % BIN)
    s = Session()
    try:
        test_socket_mode(s)
        test_status_routing(s)
        test_error_not_inherited(s)
        test_sender_wins(s)
        test_sender_wins_queued(s)
        test_refusals(s)
        test_single_client(s)
        test_reset_keeps_hook(s)
        test_dead_man(s)
    finally:
        s.close()
    test_crlf()
    test_poll_lf()
    test_poll_crlf()
    test_port_dark()
    print("ALL PASS")


if __name__ == "__main__":
    main()
