#!/usr/bin/env python3
"""Host-side verification of the operator-armed window lifecycle.

Runs the native grblHAL_glowforge binary in null-sink mode (no
hardware, no root; arming is automatic without the physical button)
and drives jobs over TCP, asserting the window's state machine on the
reported messages:

  1. the first laser-on of a job arms exactly once, and the window
     persists across M5/M3 toggles within the job (no re-prompt)
  2. program end (M2) closes the window: the disarm lands promptly,
     and the next job arms afresh
  3. a sender change closes the window: a reconnected client must
     re-arm - the displaced session's consent does not carry over
  4. the spindle-off grace counts down in HOLD, not only in Idle - a
     job abandoned in feed hold disarms after laser_disarm_s
  5. arming is refused while the cooling verdict blocks fire, and no
     armed message appears
  6. with a switch source present (GF_SWITCH_FILE), the arm blocks on the
     button: a press with the lid closed arms, and the lid or the
     interlock loop opening during the wait cancels the job (a clean
     soft reset, no alarm; latch relocked, never armed)
  7. outside the arm wait the button is the pause/resume toggle: a press
     while running feed-holds, a press while held resumes; the arming
     press itself is not a pause press
  8. the lid or the interlock loop opening mid-job cancels the job (the
     default lid_policy): reason reported, armed window closed, a soft
     reset with the position kept (no alarm), the head returns to the
     job start on its own; with lid_policy = hold the stock door hold and
     cycle-start resume apply
  9. the controller publishes its state for the daemon: grbl.settings
     (the $$ view, the derived floor in force from boot) and grbl.state
     (JSON with ts_mono, machine state, sender session, laser window
     and dose model, modals) appear under GF_STATE_DIR, follow the
     sender connection and the armed window, and carry a fresh
     ts_mono
 10. the job start survives a pause: a job paused and resumed by the
     button, then canceled by the lid, returns to where the job began,
     not to where it was paused (the core restarts a held cycle through
     Idle); a job abandoned in a hold and reset ends there, so the next
     job's start is captured afresh where it begins

The disarm grace is shortened via a temp config (GFHOME_CONF), the
cooling verdict is published hermetically (GF_VERDICT_FILE), the same
overrides the emission harness uses, and the switches are driven through
the file-backed test source (GF_SWITCH_FILE: the EV_SW word as an integer).

Usage: laser_lifecycle_test.py [path-to-binary]
       (default ./build-native/grblHAL_glowforge)
"""
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
PORT = 2398

ARMED = "laser armed"
DISARMED = "laser disarmed - latch locked"
BLOCKED = "laser fire blocked"
PROMPT = "press the button to start the laser job"
RESUME_PROMPT = "press the button to resume the laser job"
LID_CANCEL = "lid opened during arm - job canceled"
LOOP_CANCEL = "interlock open during arm - job canceled"

# EV_SW words for the file-backed switch source: bit 2 button, bit 3 doors
# (set = closed), bit 5 interlock loop (set = OPEN).
SW_CLOSED = 1 << 3
SW_PRESSED = SW_CLOSED | (1 << 2)
SW_LID_OPEN = 0
SW_LOOP_OPEN = SW_CLOSED | (1 << 5)


def fail(msg):
    print("FAIL: %s" % msg)
    sys.exit(1)


def read_avail(sock, log, timeout, until=None):
    end = time.time() + timeout
    buf = b""
    while time.time() < end:
        sock.settimeout(max(0.05, end - time.time()))
        try:
            data = sock.recv(4096)
        except socket.timeout:
            data = b""
        except OSError:
            return None
        if data:
            buf += data
            log.append(data.decode(errors="replace"))
            if until:
                text = buf.decode(errors="replace")
                for token in until:
                    if token in text or re.search(r"^%s\b" % re.escape(token), text, re.M):
                        return token
        elif until is None:
            return None
    return None


def send_line(sock, line, log, tolerate_error=False):
    sock.sendall((line + "\n").encode())
    r = read_avail(sock, log, 5.0, until=("ok", "error"))
    if r is None:
        fail("no ok/error for %r" % line)
    if r == "error" and not tolerate_error:
        fail("error response to %r" % line)
    return r


def wait_for(log, needle, timeout, sock=None):
    """Wait until needle appears in the accumulated log (draining the
    socket while waiting when one is given)."""
    end = time.time() + timeout
    while time.time() < end:
        if needle in "".join(log):
            return True
        if sock is not None:
            read_avail(sock, log, 0.2)
        else:
            time.sleep(0.1)
    return False


def wait_idle(sock, log):
    for _ in range(100):
        sock.sendall(b"?")
        read_avail(sock, log, 0.3)
        if re.search(r"<Idle", "".join(log[-3:])):
            return
        time.sleep(0.2)
    fail("controller never returned to Idle")


def publish_verdicts(path, stop, fire_ok, armed_ack=True, ack_after_s=0.0):
    """Stand in for the cooling engine. "armed" is the engine's
    acknowledgment that it has taken the controller's armed window and
    applied the run airflow; the controller refuses to fire on a verdict
    that lacks it, so an engine that never acknowledges (armed_ack
    False) must produce a refused arm and no emission. ack_after_s
    withholds the acknowledgment for that long first, which is the real
    engine's case: it answers on its next tick, and the controller has
    to see the refreshed verdict to get past the arm."""
    t0 = time.monotonic()
    while not stop.is_set():
        acked = armed_ack and time.monotonic() - t0 >= ack_after_s
        body = ('{"ts_mono":%.3f,"fire_ok":%s,"hold":false,'
                '"resume_ok":true,"armed":%s,"reason":""}'
                % (time.clock_gettime(time.CLOCK_MONOTONIC),
                   "true" if fire_ok else "false",
                   "true" if acked else "false"))
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            f.write(body)
        os.replace(tmp, path)
        stop.wait(0.5)


class Session:
    """One controller process with the lifecycle overrides applied."""

    def __init__(self, name, fire_ok=True, disarm_s=2, switches=None, conf_extra="",
                 armed_ack=True, ack_after_s=0.0):
        self.name = name
        self.workdir = tempfile.mkdtemp(prefix="laser-lifecycle-")
        conf = os.path.join(self.workdir, "forgefirm.conf")
        with open(conf, "w") as f:
            # The fan grace at 0 keeps the arm's acknowledgment budget at its
            # 5 s floor, so the refusal cases stay short; a case that wants
            # the real budget sets cool_fan_grace_s in conf_extra.
            f.write("laser_disarm_s = %d\ncool_fan_grace_s = 0\n%s" % (disarm_s, conf_extra))
        verdict = os.path.join(self.workdir, "cooling.state")
        env = dict(os.environ, GF_VERDICT_FILE=verdict, GFHOME_CONF=conf,
                   GF_STATE_DIR=self.workdir, FFLOG_STDERR="1")
        env.pop("GFSINK", None)
        env.pop("GF_SWITCH_FILE", None)
        self.switch_file = None
        if switches is not None:
            self.switch_file = os.path.join(self.workdir, "switches")
            self.set_switches(switches)
            env["GF_SWITCH_FILE"] = self.switch_file
        self.stop = threading.Event()
        self.pub = threading.Thread(target=publish_verdicts,
                                    args=(verdict, self.stop, fire_ok, armed_ack, ack_after_s),
                                    daemon=True)
        self.pub.start()
        self.proc = subprocess.Popen([BIN, "-p", str(PORT)],
                                     cwd=self.workdir, env=env,
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE)
        self.log = []
        self.sock = self.connect()

    def connect(self):
        for _ in range(50):
            try:
                s = socket.create_connection(("127.0.0.1", PORT), timeout=1)
                read_avail(s, self.log, 0.5)    # banner
                return s
            except OSError:
                time.sleep(0.1)
        err = b""
        if self.proc.poll() is not None:
            err = self.proc.stderr.read() or b""
        fail("[%s] cannot connect to the controller (exit=%s)\n%s"
             % (self.name, self.proc.poll(), err.decode(errors="replace")))

    def set_switches(self, word):
        tmp = self.switch_file + ".tmp"
        with open(tmp, "w") as f:
            f.write("%d\n" % word)
        os.replace(tmp, self.switch_file)

    def send_raw(self, line):
        """Send a line without waiting for ok/error (the arm wait blocks
        the gcode stream, so the ok only comes once the button is pressed)."""
        self.sock.sendall((line + "\n").encode())

    def press_button(self, hold_s=0.15):
        """A momentary press: word with the button bit, then without."""
        base = SW_CLOSED if self.switch_word_closed() else SW_LID_OPEN
        self.set_switches(base | (1 << 2))
        time.sleep(hold_s)
        self.set_switches(base)

    def switch_word_closed(self):
        try:
            with open(self.switch_file) as f:
                return int(f.read().strip() or "0", 0) & SW_CLOSED
        except (OSError, ValueError):
            return True

    def state(self):
        """One '?' report's state word (e.g. 'Run', 'Hold:0', 'Door:1', 'Idle')."""
        self.sock.sendall(b"?")
        read_avail(self.sock, self.log, 0.3)
        m = re.findall(r"<([A-Za-z]+(?::\d)?)", "".join(self.log[-4:]))
        return m[-1] if m else ""

    def wait_state(self, prefix, timeout):
        end = time.time() + timeout
        while time.time() < end:
            st = self.state()
            if st.startswith(prefix):
                return st
            time.sleep(0.1)
        return None

    def mpos_x(self):
        self.sock.sendall(b"?")
        read_avail(self.sock, self.log, 0.3)
        m = re.findall(r"MPos:(-?[\d.]+)", "".join(self.log[-4:]))
        return float(m[-1]) if m else None

    def armed_count(self):
        return "".join(self.log).count(ARMED)

    def disarmed_count(self):
        return "".join(self.log).count(DISARMED)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass
        self.proc.send_signal(signal.SIGINT)
        try:
            self.proc.wait(5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.stop.set()
        self.pub.join(2)
        shutil.rmtree(self.workdir, ignore_errors=True)


def read_state(sess, key=None, timeout=8.0):
    """The published grbl.state as text, polled until `key` appears (the
    publisher runs on the protocol thread's pace; a '?' keeps it ticking)."""
    path = os.path.join(sess.workdir, "grbl.state")
    deadline = time.time() + timeout
    text = ""
    while time.time() < deadline:
        sess.sock.sendall(b"?")
        read_avail(sess.sock, sess.log, 0.2)
        try:
            with open(path) as f:
                text = f.read()
        except OSError:
            text = ""
        if text.endswith("\n") and (key is None or key in text):
            return text
        time.sleep(0.2)
    return text


def test_status_files():
    print("state files: settings and state published, following the edges")
    s = Session("state-files", disarm_s=30)
    try:
        st = read_state(s, '"connected":true')
        assert '"state":"Idle"' in st, "state file lacks Idle: %r" % st
        assert '"connected":true' in st, "sender not reported connected: %r" % st
        assert '"armed":false' in st, "armed before any job: %r" % st
        assert '"model":"density"' in st and '"floor_pct":10' in st, \
            "model/floor missing (the floor must be derived from boot): %r" % st
        assert '"curve":"bench-default"' in st, \
            "the curve in force is not published: %r" % st
        assert '"modals":"[GC:' in st, "modal report missing: %r" % st
        ts0 = float(st.split('"ts_mono":')[1].split(',')[0])

        cfg = open(os.path.join(s.workdir, "grbl.settings")).read()
        assert "$35=" in cfg and "$30=" in cfg, "settings file lacks $ lines"

        # The derived floor is in force from boot, before any arm.
        assert "$35=10" in cfg, "the derived floor is not in $$ from boot: %r" % [
            l for l in cfg.splitlines() if l.startswith("$35")]
        # The armed window reaches the file.
        send_line(s.sock, "G91", s.log)
        send_line(s.sock, "M3 S100", s.log)
        st = read_state(s, '"armed":true')
        assert '"armed":true' in st, "armed window not published: %r" % st
        send_line(s.sock, "M5", s.log)
        send_line(s.sock, "M2", s.log)

        # A reconnect bumps the generation and stays connected.
        gen0 = int(st.split('"generation":')[1].split(',')[0])
        s.sock.close()
        s.sock = s.connect()
        st = read_state(s, '"connected":true')
        gen1 = int(st.split('"generation":')[1].split(',')[0])
        assert gen1 > gen0, "generation did not advance on reconnect (%d -> %d)" % (gen0, gen1)
        ts1 = float(st.split('"ts_mono":')[1].split(',')[0])
        assert ts1 > ts0, "ts_mono did not advance"
        print("PASS [state-files]: published, armed/switch/revert followed, "
              "generation %d -> %d" % (gen0, gen1))
    finally:
        s.close()


def test_laser_keys_reload():
    """M102 reloads the corner gamma, the curve, and the floor inside an
    armed job, synchronized, and the state file follows them."""
    print("laser keys reload: M102 takes rewritten laser keys mid-job")
    s = Session("keys-reload", disarm_s=30, conf_extra="laser_corner_gamma = 1.5\n")
    try:
        st = read_state(s, '"gamma":1.50')
        assert '"gamma":1.50' in st, "the gamma from the conf is not in force at boot: %r" % st
        send_line(s.sock, "G91", s.log)
        send_line(s.sock, "M4 S100", s.log)
        st = read_state(s, '"armed":true')
        assert '"armed":true' in st, "armed window not published: %r" % st
        send_line(s.sock, "G1 X2 F600", s.log)
        conf = os.path.join(s.workdir, "forgefirm.conf")
        with open(conf, "w") as f:
            f.write("laser_disarm_s = 30\nlaser_corner_gamma = 1.25\nlaser_dose_curve = off\n"
                    "laser_floor_density = 0\n")
        send_line(s.sock, "M102", s.log)
        assert wait_for(s.log, "laser keys reloaded: corner gamma 1.25, curve off, floor 0 %", 5, s.sock), \
            "M102 did not report the reload: %r" % s.log[-6:]
        st = read_state(s, '"gamma":1.25')
        assert '"gamma":1.25' in st and '"curve":"off"' in st and '"floor_pct":0' in st, \
            "the reloaded keys did not reach the state file: %r" % st
        assert '"armed":true' in st, "the reload closed the armed window: %r" % st
        send_line(s.sock, "G1 X2 F600", s.log)
        send_line(s.sock, "M5", s.log)
        send_line(s.sock, "M2", s.log)
        print("PASS [keys-reload]: gamma 1.50 -> 1.25, the curve off, the floor 0, inside the armed job")
    finally:
        s.close()


def test_job_window():
    """Rules 1 + 2: arm once per job, persist across M5/M3, close at M2,
    fresh consent for the next job. The grace is set far beyond the test
    horizon so only the program-end close can produce the disarm."""
    s = Session("job-window", disarm_s=60)
    try:
        send_line(s.sock, "M4 S100", s.log)
        send_line(s.sock, "G1 X1 F600", s.log)
        if not wait_for(s.log, ARMED, 5, s.sock):
            fail("[job-window] first laser-on did not arm")
        send_line(s.sock, "M5", s.log)
        send_line(s.sock, "M3 S200", s.log)
        send_line(s.sock, "G1 X0 F600", s.log)
        wait_idle(s.sock, s.log)
        if s.armed_count() != 1:
            fail("[job-window] M5/M3 inside one job re-prompted the arm "
                 "(armed messages: %d)" % s.armed_count())
        send_line(s.sock, "M2", s.log)
        if not wait_for(s.log, DISARMED, 5, s.sock):
            fail("[job-window] program end (M2) did not close the armed window")
        send_line(s.sock, "M4 S100", s.log)
        send_line(s.sock, "G1 X1 F600", s.log)
        if not wait_for(s.log, ARMED, 5, s.sock) or s.armed_count() != 2:
            fail("[job-window] next job after M2 did not arm afresh")
        wait_idle(s.sock, s.log)
        print("PASS [job-window]: armed once per job, M2 closed the window, "
              "next job re-armed")
    finally:
        s.close()


def test_sender_change_mid_job():
    """Rule 3, the hard case: the sender changes while the job is still
    running with the spindle on. The window closes and the job is HELD
    where the cut stopped, so the next sender finds it in Hold rather than
    at the end of a dark pass. Its cycle start goes through the resume
    gate, which re-arms first: with no button on a host build that is
    immediate, and the job then runs to its end lit."""
    s = Session("sender-change-mid-job", disarm_s=60)
    try:
        send_line(s.sock, "M4 S100", s.log)
        send_line(s.sock, "G1 X5 F60", s.log)             # 5 s of motion
        if not wait_for(s.log, ARMED, 5, s.sock):
            fail("[sender-change-mid-job] first laser-on did not arm")
        time.sleep(1.0)
        s.sock.close()                                     # mid-move, spindle on
        time.sleep(0.5)
        # The disarm and hold messages are written while no client is
        # connected, and output with no client is discarded, so the new
        # session cannot see them; the Hold state and the re-arm below are
        # the evidence.
        s.sock = s.connect()
        st = s.wait_state("Hold", 5)
        if st is None:
            fail("[sender-change-mid-job] the job was not held on the sender change "
                 "(state %r)" % s.state())
        x_held = s.mpos_x()
        if x_held is None or not 0.5 < x_held < 4.5:
            fail("[sender-change-mid-job] held outside the move (MPos X %s)" % x_held)
        before = s.armed_count()
        s.sock.sendall(b"~")                               # the new sender resumes
        end = time.time() + 15
        while time.time() < end and s.armed_count() != before + 1:
            read_avail(s.sock, s.log, 0.2)
        if s.armed_count() != before + 1:
            fail("[sender-change-mid-job] the resume after a mid-job sender change did "
                 "not re-arm")
        wait_idle(s.sock, s.log)
        x_end = s.mpos_x()
        if x_end is None or abs(x_end - 5.0) > 0.01:
            fail("[sender-change-mid-job] the resumed job did not finish the move "
                 "(MPos X %s)" % x_end)
        send_line(s.sock, "M5", s.log)
        print("PASS [sender-change-mid-job]: the job held at X=%.3f on the sender change, "
              "re-armed on the resume and finished at X=%.3f" % (x_held, x_end))
    finally:
        s.close()


def test_sender_change_holds_and_rearms():
    """Rule 3 on a machine with a button: the held job's resume from the
    new sender prompts for the press (collected from the poll, never
    inside the core's held state), and the press re-arms and resumes."""
    s = Session("sender-change-rearm", disarm_s=60, switches=SW_CLOSED)
    try:
        s.send_raw("M4 S100")
        if not wait_for(s.log, PROMPT, 5, s.sock):
            fail("[sender-change-rearm] no arm prompt")
        s.press_button()
        if not wait_for(s.log, ARMED, 5, s.sock):
            fail("[sender-change-rearm] the press did not arm")
        send_line(s.sock, "G1 X5 F60", s.log)             # 5 s of motion
        time.sleep(1.0)
        s.sock.close()                                     # mid-move, spindle on
        time.sleep(0.5)
        s.sock = s.connect()
        if s.wait_state("Hold", 5) is None:
            fail("[sender-change-rearm] the job was not held on the sender change")
        before = s.armed_count()
        s.sock.sendall(b"~")
        if not wait_for(s.log, RESUME_PROMPT, 5, s.sock):
            fail("[sender-change-rearm] the resume did not prompt for the button")
        if s.wait_state("Hold", 2) is None:
            fail("[sender-change-rearm] the job left Hold before the press")
        s.press_button()
        end = time.time() + 10
        while time.time() < end and s.armed_count() != before + 1:
            read_avail(s.sock, s.log, 0.2)
        if s.armed_count() != before + 1:
            fail("[sender-change-rearm] the press did not re-arm the held job")
        wait_idle(s.sock, s.log)
        x_end = s.mpos_x()
        if x_end is None or abs(x_end - 5.0) > 0.01:
            fail("[sender-change-rearm] the resumed job did not finish the move "
                 "(MPos X %s)" % x_end)
        send_line(s.sock, "M5", s.log)
        print("PASS [sender-change-rearm]: the held job prompted on ~, the press re-armed "
              "it, and it finished at X=%.3f" % x_end)
    finally:
        s.close()


def test_sender_change_then_reset():
    """A new sender that does not want the held job resets it: a soft
    reset from the hold ends the job with the position kept and no alarm."""
    s = Session("sender-change-reset", disarm_s=60)
    try:
        send_line(s.sock, "M4 S100", s.log)
        send_line(s.sock, "G1 X5 F60", s.log)
        if not wait_for(s.log, ARMED, 5, s.sock):
            fail("[sender-change-reset] first laser-on did not arm")
        time.sleep(1.0)
        s.sock.close()
        time.sleep(0.5)
        s.sock = s.connect()
        if s.wait_state("Hold", 5) is None:
            fail("[sender-change-reset] the job was not held on the sender change")
        s.sock.sendall(b"\x18")
        st = s.wait_state("Idle", 5)
        if st is None:
            fail("[sender-change-reset] a reset from the held job did not end in Idle "
                 "(state %r)" % s.state())
        print("PASS [sender-change-reset]: a reset from the held job ends in Idle, no alarm")
    finally:
        s.close()


def test_resume_after_grace():
    """Rule 4, the other half: a job whose window closed while it sat in
    Hold can still be resumed. The sender's ~ prompts for the button, the
    press re-arms, and the cut finishes lit (the controller must not sit
    in the arm wait inside the held state, where a press is never read)."""
    s = Session("resume-after-grace", disarm_s=2, switches=SW_CLOSED)
    try:
        s.send_raw("M4 S100")
        if not wait_for(s.log, PROMPT, 5, s.sock):
            fail("[resume-after-grace] no arm prompt")
        s.press_button()
        if not wait_for(s.log, ARMED, 5, s.sock):
            fail("[resume-after-grace] the press did not arm")
        send_line(s.sock, "G1 X5 F60", s.log)             # 5 s of motion
        time.sleep(1.0)
        s.sock.sendall(b"!")                                # feed hold
        if not wait_for(s.log, DISARMED, 10, s.sock):
            fail("[resume-after-grace] the window did not close in the hold")
        before = s.armed_count()
        s.sock.sendall(b"~")
        if not wait_for(s.log, RESUME_PROMPT, 5, s.sock):
            fail("[resume-after-grace] the resume did not prompt for the button")
        if s.wait_state("Hold", 2) is None:
            fail("[resume-after-grace] the job left Hold before the press")
        s.press_button()
        end = time.time() + 10
        while time.time() < end and s.armed_count() != before + 1:
            read_avail(s.sock, s.log, 0.2)
        if s.armed_count() != before + 1:
            fail("[resume-after-grace] the press did not re-arm the held job")
        wait_idle(s.sock, s.log)
        x_end = s.mpos_x()
        if x_end is None or abs(x_end - 5.0) > 0.01:
            fail("[resume-after-grace] the resumed job did not finish the move "
                 "(MPos X %s)" % x_end)
        send_line(s.sock, "M5", s.log)
        print("PASS [resume-after-grace]: ~ prompted, the press re-armed the held job, "
              "and it finished at X=%.3f" % x_end)
    finally:
        s.close()


def test_rx_overrun_aborts():
    """A sender that writes past the RX ring (Bf: is the contract) loses
    lines, and a job with lines missing is not the job the sender wrote:
    the controller reports the overrun, stops the way a ^X stops it
    (alarm, window closed) and never runs on with a hole in it. A clean
    job after the reset arms again."""
    s = Session("rx-overrun", disarm_s=60)
    try:
        send_line(s.sock, "M4 S100", s.log)
        send_line(s.sock, "G1 X1 F600", s.log)
        if not wait_for(s.log, ARMED, 5, s.sock):
            fail("[rx-overrun] first laser-on did not arm")
        job = "".join("G1 X%d F60\n" % (2 + (i % 2)) for i in range(120))   # ~1.3 KB at once
        s.sock.sendall(job.encode())
        if not wait_for(s.log, "RX overrun", 10, s.sock):
            fail("[rx-overrun] the overrun was not reported to the sender")
        if not s.wait_state("Alarm", 5):
            fail("[rx-overrun] the overrun did not stop the job (state %s)" % s.state())
        if s.disarmed_count() < 1:
            read_avail(s.sock, s.log, 1.0)
        if s.disarmed_count() < 1:
            fail("[rx-overrun] the overrun did not close the armed window")
        send_line(s.sock, "$X", s.log)
        before = s.armed_count()
        send_line(s.sock, "M4 S100", s.log)
        send_line(s.sock, "G1 X0 F600", s.log)
        end = time.time() + 5
        while time.time() < end and s.armed_count() != before + 1:
            read_avail(s.sock, s.log, 0.2)
        if s.armed_count() != before + 1:
            fail("[rx-overrun] a clean job after the overrun did not arm")
        wait_idle(s.sock, s.log)
        print("PASS [rx-overrun]: the overrun was reported, the job stopped in alarm with "
              "the window closed, and the next clean job armed")
    finally:
        s.close()


def test_sender_change():
    """Rule 3: a reconnected sender must re-arm. The grace is set far
    beyond the test horizon so only the sender change can close the
    window; the first job ends in M5 and the next M4 is a fresh laser-on."""
    s = Session("sender-change", disarm_s=60)
    try:
        send_line(s.sock, "M4 S100", s.log)
        send_line(s.sock, "G1 X1 F600", s.log)
        if not wait_for(s.log, ARMED, 5, s.sock):
            fail("[sender-change] first laser-on did not arm")
        send_line(s.sock, "M5", s.log)
        wait_idle(s.sock, s.log)
        s.sock.close()
        time.sleep(0.5)
        s.sock = s.connect()
        before = s.armed_count()
        send_line(s.sock, "M4 S100", s.log)
        send_line(s.sock, "G1 X0 F600", s.log)
        end = time.time() + 5
        while time.time() < end and s.armed_count() != before + 1:
            read_avail(s.sock, s.log, 0.2)
        if s.armed_count() != before + 1:
            fail("[sender-change] the reconnected sender inherited the "
                 "displaced session's armed window")
        wait_idle(s.sock, s.log)
        print("PASS [sender-change]: displaced consent did not survive; "
              "the new sender re-armed")
    finally:
        s.close()


def test_hold_grace():
    """Rule 4: the disarm grace counts down in HOLD."""
    s = Session("hold-grace", disarm_s=2)
    try:
        send_line(s.sock, "M4 S100", s.log)
        send_line(s.sock, "G1 X30 F60", s.log)     # ~30 s move
        if not wait_for(s.log, ARMED, 5, s.sock):
            fail("[hold-grace] job did not arm")
        time.sleep(1.0)
        s.sock.sendall(b"!")                        # feed hold
        if not wait_for(s.log, DISARMED, 10, s.sock):
            fail("[hold-grace] a job abandoned in Hold stayed armed past "
                 "the disarm grace")
        print("PASS [hold-grace]: the armed window closed while parked in Hold")
    finally:
        s.close()


def test_verdict_blocks_arm():
    """Rule 5: a blocking cooling verdict refuses the arm."""
    s = Session("verdict-blocked", fire_ok=False)
    try:
        send_line(s.sock, "M4 S100", s.log, tolerate_error=True)
        send_line(s.sock, "G1 X1 F600", s.log, tolerate_error=True)
        if not wait_for(s.log, BLOCKED, 5, s.sock):
            fail("[verdict-blocked] no refusal message for a blocking verdict")
        if ARMED in "".join(s.log):
            fail("[verdict-blocked] armed despite a blocking cooling verdict")
        print("PASS [verdict-blocked]: arming refused while fire_ok=false")
    finally:
        s.close()


def test_arm_waits_for_engine_ack():
    """Rule 5: the arm does not fire on a verdict computed before it.

    The armed window is reported to the cooling engine when it opens,
    and the engine answers by applying the cut airflow and the flow
    interrogation for the job. The verdict standing on file until that
    answer arrives is the one computed for the idle session that
    preceded the arm, and at idle nothing is wrong, so it says fire is
    fine. Firing on it puts the beam on the work with the fans still at
    their idle duty - observed on the bench as a burn of a few
    millimeters before the engine's first tick took the job into a hold.

    Here the engine never acknowledges: fire_ok stays true throughout,
    so the only thing that can refuse the job is the missing
    acknowledgment. The arm must be refused and no window may open."""
    s = Session("arm-ack", fire_ok=True, armed_ack=False)
    try:
        # The arm blocks in the acknowledgment wait, so the ok for these
        # lines only arrives once it gives up: send without waiting.
        s.send_raw("M4 S100")
        s.send_raw("G1 X1 F600")
        if not wait_for(s.log, BLOCKED, 15, s.sock):
            fail("[arm-ack] a job whose cooling engine never took it was not "
                 "refused (the arm fired on the pre-arm verdict)")
        if ARMED in "".join(s.log):
            fail("[arm-ack] the armed window opened without the engine's "
                 "acknowledgment")
        print("PASS [arm-ack]: the arm refused a verdict that predates it")
    finally:
        s.close()


def test_arm_proceeds_on_a_late_ack():
    """Rule 5: the acknowledgment arriving late is the ordinary case.

    The real engine answers on its next tick, so the arm has to observe
    a verdict refreshed under it and then go on to open the window. This
    is the case that proves the wait is a wait and not a refusal: the
    controller must keep polling the verdict file while it is blocked in
    the arm, or every job on the machine would fail here."""
    ack_s = 2.0
    s = Session("arm-ack-late", fire_ok=True, ack_after_s=ack_s)
    try:
        t0 = time.time()
        s.send_raw("M4 S100")
        s.send_raw("G1 X1 F600")
        if not wait_for(s.log, ARMED, 15, s.sock):
            fail("[arm-ack-late] the arm never completed once the engine took "
                 "the job (the verdict is not refreshed inside the wait)")
        dt = time.time() - t0
        if dt < ack_s - 0.5:
            fail("[arm-ack-late] armed after %.1f s, before the engine took the "
                 "job at %.1f s" % (dt, ack_s))
        if BLOCKED in "".join(s.log):
            fail("[arm-ack-late] a late acknowledgment was reported as a refusal")
        print("PASS [arm-ack-late]: the arm waited %.1f s for the engine, then armed" % dt)
    finally:
        s.close()


def test_arm_waits_for_the_fans():
    """Rule 5: the acknowledgment budget follows the fan grace. The
    engine acknowledges once the fans are at their floors, seconds after
    the arm; with cool_fan_grace_s at 8 the controller waits up to 13 s
    for it, and an acknowledgment at 9 s arms the job rather than
    refusing it at the old 5 s budget."""
    ack_s = 9.0
    s = Session("arm-fans", fire_ok=True, ack_after_s=ack_s,
                conf_extra="cool_fan_grace_s = 8\n")
    try:
        t0 = time.time()
        s.send_raw("M4 S100")
        s.send_raw("G1 X1 F600")
        if not wait_for(s.log, ARMED, 20, s.sock):
            fail("[arm-fans] the arm never completed with the acknowledgment at %.0f s "
                 "(the budget did not follow cool_fan_grace_s)" % ack_s)
        dt = time.time() - t0
        if dt < ack_s - 0.5:
            fail("[arm-fans] armed after %.1f s, before the fans were up at %.1f s" % (dt, ack_s))
        if BLOCKED in "".join(s.log):
            fail("[arm-fans] the wait for the fans was reported as a refusal")
        print("PASS [arm-fans]: the arm waited %.1f s for the fans, then armed" % dt)
    finally:
        s.close()


def test_sigterm_mid_job():
    """Rule 5: a termination signal during an armed job is a STOP, not a
    "finish the job first". The supervisor's expected-stop path (mode
    switch, diagnostics, the emergency lever) delivers SIGTERM; the
    controller must bring motion to a controlled stop, close the armed
    window (latch relocked) and exit promptly - long before the job
    would have ended and inside the supervisor's SIGKILL grace."""
    s = Session("sigterm-mid-job", disarm_s=60)
    try:
        send_line(s.sock, "M4 S100", s.log)
        send_line(s.sock, "G1 X300 F60", s.log)          # a 5-minute move
        if not wait_for(s.log, ARMED, 5, s.sock):
            fail("[sigterm-mid-job] the job did not arm")
        # Let the run get under way.
        deadline = time.time() + 5
        running = False
        while time.time() < deadline and not running:
            s.sock.sendall(b"?")
            read_avail(s.sock, s.log, 0.3)
            running = "<Run" in "".join(s.log[-5:])
        if not running:
            fail("[sigterm-mid-job] the job never reported Run")
        t0 = time.time()
        s.proc.send_signal(signal.SIGTERM)
        # Keep draining the sender socket so the disarm report is captured.
        exited = False
        while time.time() - t0 < 10:
            read_avail(s.sock, s.log, 0.2)
            if s.proc.poll() is not None:
                exited = True
                break
        dt = time.time() - t0
        if not exited:
            fail("[sigterm-mid-job] the controller did not exit within 10 s of "
                 "SIGTERM during a job (it must stop, not finish the job)")
        if dt > 3.0:
            fail("[sigterm-mid-job] exit took %.1f s after SIGTERM (want < 3 s)" % dt)
        if s.disarmed_count() < 1:
            fail("[sigterm-mid-job] no disarm (latch relock) reported before exit")
        if s.proc.returncode != 0:
            fail("[sigterm-mid-job] exit status %s (want a clean 0)" % s.proc.returncode)
        print("PASS [sigterm-mid-job]: SIGTERM during a job stopped it, relocked "
              "the latch and exited in %.2f s" % dt)
    finally:
        s.close()


def test_button_wait_arms():
    """Rule 6a: with a switch source the arm blocks on the button; a press
    with the lid closed arms; the window is not opened before it."""
    s = Session("button-wait", disarm_s=60, switches=SW_CLOSED)
    try:
        s.send_raw("M4 S100")
        s.send_raw("G1 X1 F600")
        if not wait_for(s.log, PROMPT, 5, s.sock):
            fail("[button-wait] no button prompt with a switch source present")
        read_avail(s.sock, s.log, 1.0)
        if ARMED in "".join(s.log):
            fail("[button-wait] armed before the button was pressed")
        s.set_switches(SW_PRESSED)
        if not wait_for(s.log, ARMED, 5, s.sock):
            fail("[button-wait] the button press did not arm")
        s.set_switches(SW_CLOSED)
        wait_idle(s.sock, s.log)
        print("PASS [button-wait]: the arm blocked on the button and the press armed")
    finally:
        s.close()


def test_lid_open_in_wait():
    """Rule 6b: the lid opening during the button wait cancels the job:
    the reason is reported, an alarm is raised, the window never opens.
    A press with the lid still open must not arm either."""
    s = Session("lid-in-wait", disarm_s=60, switches=SW_CLOSED)
    try:
        s.send_raw("M4 S100")
        s.send_raw("G1 X1 F600")
        if not wait_for(s.log, PROMPT, 5, s.sock):
            fail("[lid-in-wait] no button prompt")
        s.set_switches(SW_LID_OPEN)
        if not wait_for(s.log, LID_CANCEL, 5, s.sock):
            fail("[lid-in-wait] lid open during the wait was not reported as a cancel")
        if not wait_for(s.log, "for help]", 5, s.sock):
            fail("[lid-in-wait] no reset banner after the lid-open cancel (a clean cancel, not an alarm)")
        if "ALARM" in "".join(s.log):
            fail("[lid-in-wait] an alarm was raised on the lid-open cancel")
        # Press with the lid still open: nothing may arm.
        s.set_switches(1 << 2)
        read_avail(s.sock, s.log, 1.0)
        if ARMED in "".join(s.log):
            fail("[lid-in-wait] armed after a lid-open cancel")
        # Interlock variant on a fresh session, so the alarm state does not
        # mask the check.
        s.close()
        s = Session("loop-in-wait", disarm_s=60, switches=SW_CLOSED)
        s.send_raw("M4 S100")
        s.send_raw("G1 X1 F600")
        if not wait_for(s.log, PROMPT, 5, s.sock):
            fail("[loop-in-wait] no button prompt")
        s.set_switches(SW_LOOP_OPEN)
        if not wait_for(s.log, LOOP_CANCEL, 5, s.sock):
            fail("[loop-in-wait] interlock open during the wait was not reported as a cancel")
        if ARMED in "".join(s.log):
            fail("[loop-in-wait] armed despite the open interlock loop")
        print("PASS [lid-in-wait]: lid or interlock open during the arm wait "
              "canceled the job and never armed")
    finally:
        s.close()


def start_armed_move(s, tag, gcode="G1 X30 F60"):
    """Arm through the button and get a long move under way; returns once
    the controller reports Run."""
    s.send_raw("M4 S100")
    s.send_raw(gcode)
    if not wait_for(s.log, PROMPT, 5, s.sock):
        fail("[%s] no button prompt" % tag)
    s.press_button()
    if not wait_for(s.log, ARMED, 5, s.sock):
        fail("[%s] the button press did not arm" % tag)
    if not s.wait_state("Run", 5):
        fail("[%s] the job never reported Run" % tag)


def test_button_pause_resume():
    """Rule 7: outside the arm wait the button is the pause/resume toggle -
    a press while running is a feed hold, a press while held is a cycle
    start; the arming press itself is not a pause press."""
    s = Session("button-toggle", disarm_s=60, switches=SW_CLOSED)
    try:
        start_armed_move(s, "button-toggle")
        time.sleep(0.5)
        if s.state().startswith("Hold"):
            fail("[button-toggle] the arming press was taken as a pause press")
        s.press_button()
        if not s.wait_state("Hold", 3):
            fail("[button-toggle] a press while running did not feed-hold (state %s)" % s.state())
        if not wait_for(s.log, "button pressed - job paused", 2, s.sock):
            fail("[button-toggle] no pause message")
        s.press_button()
        if not s.wait_state("Run", 3):
            fail("[button-toggle] a press while held did not resume (state %s)" % s.state())
        if not wait_for(s.log, "button pressed - job resumed", 2, s.sock):
            fail("[button-toggle] no resume message")
        s.sock.sendall(b"\x18")
        print("PASS [button-toggle]: press paused (Hold), press resumed (Run); the arming press did not")
    finally:
        s.close()


def test_lid_cancels_and_returns():
    """Rule 8 (lid_policy = cancel, the default): the lid opening mid-job
    parks the job and cancels it - reason reported, armed window closed,
    a soft reset (no alarm: position kept), then the head returns on its
    own to the job start; the sender sees the reset banner. The interlock
    loop is the same event with its own reason."""
    s = Session("lid-cancel", disarm_s=60, switches=SW_CLOSED)
    try:
        start_armed_move(s, "lid-cancel", gcode="G1 X20 F600")
        time.sleep(0.5)
        x_mid = s.mpos_x()
        s.set_switches(SW_LID_OPEN)
        if not wait_for(s.log, "lid opened - job canceled", 5, s.sock):
            fail("[lid-cancel] the lid open did not cancel the job")
        if not wait_for(s.log, DISARMED, 5, s.sock):
            fail("[lid-cancel] the cancel did not close the armed window")
        if not wait_for(s.log, "for help]", 5, s.sock):
            fail("[lid-cancel] no reset banner after the cancel")
        if not wait_for(s.log, "returned to the job start", 15, s.sock):
            fail("[lid-cancel] the head did not report returning to the job start")
        st = s.wait_state("Idle", 5)
        if not st:
            fail("[lid-cancel] not Idle after the return (state %s)" % s.state())
        if "ALARM" in "".join(s.log):
            fail("[lid-cancel] an alarm was raised on the cancel (position should be kept)")
        x_end = s.mpos_x()
        if x_end is None or abs(x_end) > 0.05:
            fail("[lid-cancel] head not back at the job start: MPos X=%s (was %s mid-job)" % (x_end, x_mid))
        print("PASS [lid-cancel]: lid open mid-job -> canceled, disarmed, reset banner, "
              "returned to X=%.3f (mid-job X=%.3f), Idle, no alarm" % (x_end, x_mid))
        s.close()
        # Interlock variant.
        s = Session("loop-cancel", disarm_s=60, switches=SW_CLOSED)
        start_armed_move(s, "loop-cancel", gcode="G1 X20 F600")
        time.sleep(0.5)
        s.set_switches(SW_LOOP_OPEN)
        if not wait_for(s.log, "interlock open - job canceled", 5, s.sock):
            fail("[loop-cancel] the interlock open did not cancel the job")
        if not wait_for(s.log, "returned to the job start", 15, s.sock):
            fail("[loop-cancel] no return to the job start after the interlock cancel")
        print("PASS [loop-cancel]: interlock open mid-job -> canceled and returned")
    finally:
        s.close()


def test_pause_then_lid_cancel_returns_to_the_job_start():
    """Rule 9: the button pause + resume does not move the job start. The
    lid cancel after a resume returns the head to where the job began;
    the pause point is not a job start. Then the other side: a job left
    in a hold and soft-reset is over, and the next job from that spot
    returns to that spot, not to the earlier job's start."""
    s = Session("pause-lid-cancel", disarm_s=60, switches=SW_CLOSED)
    try:
        start_armed_move(s, "pause-lid-cancel", gcode="G1 X30 F600")
        time.sleep(0.6)
        s.press_button()
        if not s.wait_state("Hold", 3):
            fail("[pause-lid-cancel] the press did not feed-hold (state %s)" % s.state())
        time.sleep(0.5)
        x_hold = s.mpos_x()
        if x_hold is None or x_hold < 1.0:
            fail("[pause-lid-cancel] the hold landed too close to the start to tell (X=%s)" % x_hold)
        s.press_button()
        if not s.wait_state("Run", 3):
            fail("[pause-lid-cancel] the second press did not resume (state %s)" % s.state())
        time.sleep(0.4)
        s.set_switches(SW_LID_OPEN)
        if not wait_for(s.log, "lid opened - job canceled", 5, s.sock):
            fail("[pause-lid-cancel] the lid open did not cancel the job")
        if not wait_for(s.log, "returned to the job start", 15, s.sock):
            fail("[pause-lid-cancel] the head did not report returning to the job start")
        if not s.wait_state("Idle", 5):
            fail("[pause-lid-cancel] not Idle after the return (state %s)" % s.state())
        x_end = s.mpos_x()
        if x_end is None or abs(x_end) > 0.05:
            fail("[pause-lid-cancel] the head returned to X=%s, not to the job start X=0 "
                 "(paused at X=%.3f: the resume was taken as a new job start)" % (x_end, x_hold))
        print("PASS [pause-lid-cancel]: paused at X=%.3f, resumed, lid cancel returned to X=%.3f"
              % (x_hold, x_end))
        s.close()

        # A job abandoned in a hold and reset is over: the next job starts
        # where it starts.
        s = Session("hold-reset-restart", disarm_s=60, switches=SW_CLOSED)
        start_armed_move(s, "hold-reset-restart", gcode="G1 X30 F600")
        time.sleep(0.6)
        s.press_button()
        if not s.wait_state("Hold", 3):
            fail("[hold-reset-restart] the press did not feed-hold (state %s)" % s.state())
        time.sleep(0.5)
        s.sock.sendall(b"\x18")
        if not wait_for(s.log, "for help]", 5, s.sock):
            fail("[hold-reset-restart] no reset banner")
        if not s.wait_state("Idle", 5):
            fail("[hold-reset-restart] not Idle after the reset (state %s)" % s.state())
        x_new_start = s.mpos_x()
        if x_new_start is None or x_new_start < 1.0:
            fail("[hold-reset-restart] the reset did not keep the position (X=%s)" % x_new_start)
        read_avail(s.sock, s.log, 0.3)
        start_armed_move(s, "hold-reset-restart", gcode="G1 X60 F600")     # absolute: past the old start
        time.sleep(0.5)
        s.set_switches(SW_LID_OPEN)
        if not wait_for(s.log, "returned to the job start", 15, s.sock):
            fail("[hold-reset-restart] no return to the job start after the lid cancel")
        if not s.wait_state("Idle", 5):
            fail("[hold-reset-restart] not Idle after the return (state %s)" % s.state())
        x_end = s.mpos_x()
        if x_end is None or abs(x_end - x_new_start) > 0.05:
            fail("[hold-reset-restart] the head returned to X=%s, not to this job's start X=%.3f "
                 "(the earlier job's start survived the reset)" % (x_end, x_new_start))
        print("PASS [hold-reset-restart]: a job reset from a hold ended there; the next job "
              "returned to its own start X=%.3f" % x_end)
    finally:
        s.close()


def test_lid_policy_hold():
    """lid_policy = hold keeps stock grblHAL behavior: the lid parks the job
    in Door and a cycle start resumes it once closed."""
    s = Session("lid-hold", disarm_s=60, switches=SW_CLOSED, conf_extra="lid_policy = hold\n")
    try:
        start_armed_move(s, "lid-hold", gcode="G1 X20 F60")
        time.sleep(0.5)
        s.set_switches(SW_LID_OPEN)
        if not s.wait_state("Door", 3):
            fail("[lid-hold] the lid open did not park the job in Door (state %s)" % s.state())
        read_avail(s.sock, s.log, 1.0)
        if "job canceled" in "".join(s.log):
            fail("[lid-hold] the hold policy canceled the job")
        s.set_switches(SW_CLOSED)
        time.sleep(0.5)
        s.sock.sendall(b"~")
        if not s.wait_state("Run", 3):
            fail("[lid-hold] cycle start did not resume the held job (state %s)" % s.state())
        s.sock.sendall(b"\x18")
        print("PASS [lid-hold]: with lid_policy=hold the lid parked the job in Door and ~ resumed it")
    finally:
        s.close()


def main():
    if not os.path.isfile(BIN):
        fail("controller binary not found at %s" % BIN)
    test_job_window()
    test_status_files()
    test_laser_keys_reload()
    test_sender_change()
    test_sender_change_mid_job()
    test_sender_change_holds_and_rearms()
    test_sender_change_then_reset()
    test_rx_overrun_aborts()
    test_hold_grace()
    test_resume_after_grace()
    test_button_wait_arms()
    test_lid_open_in_wait()
    test_button_pause_resume()
    test_lid_cancels_and_returns()
    test_pause_then_lid_cancel_returns_to_the_job_start()
    test_lid_policy_hold()
    test_verdict_blocks_arm()
    test_arm_waits_for_engine_ack()
    test_arm_proceeds_on_a_late_ack()
    test_arm_waits_for_the_fans()
    test_sigterm_mid_job()
    print("PASS: the armed-window lifecycle holds")


if __name__ == "__main__":
    main()
