#!/usr/bin/env python3
# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT
"""Host-side verification of the operator-armed window lifecycle.

Runs the native grblHAL_glowforge binary in null-sink mode (no
hardware, no root; arming is automatic without the physical button)
and drives jobs over TCP, asserting the window's state machine on the
reported messages:

  1. the first laser-on of a job arms exactly once; the window's
     persistence across S changes and M5/M3 toggles inside the job is
     laser_arm_test.c case I's
  2. program end (M2) closing the window, and the next job arming
     afresh: laser_stream_test session K
  3. a sender change closes the window: a reconnected client must
     re-arm - the displaced session's consent does not carry over; the
     disarm and the relock at the change are laser_arm_test.c case H,
     and a new sender's soft reset of the held job is the core's reset
     from a completed hold, session hold-reset-restart under rule 10
  4. the spindle-off grace counts down in HOLD, not only in Idle - a
     job abandoned in feed hold disarms after laser_disarm_s
  5. arming refused while the cooling verdict blocks fire, or before
     the engine acknowledges the session: laser_arm_test.c cases C, A2
     and A3, and cooling_test.c's arm-time gate; the wait for the fans
     is laser_arm_test.c case A4
  6. with a switch source present (GF_SWITCH_FILE), the arm blocks on the
     button: a press with the lid closed arms, and the lid or the
     interlock loop opening during the wait cancels the job (a clean
     soft reset, no alarm; latch relocked, never armed)
  7. outside the arm wait the button is the pause/resume toggle: a press
     while running feed-holds, a press while held resumes; the arming
     press itself is not a pause press; a travel job (no laser, no arm
     wait) takes the same toggle, and its resumed move lands on its target
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
 11. the cooling verdict's pause tier holding the job under the open
     window without a latch write, and the clean verdict resuming it with
     no press and no second arm: laser_stream_test rule 24 and
     laser_arm_test.c case V1
 12. the verdict's fail tier ending the job (disarmed, ALARM:3, nothing
     resumed): laser_stream_test rule 25 and laser_arm_test.c case V2
 13. a sender change during a re-arm cancels it: the press that follows
     resumes nothing, the job stays held, and the new sender's own ~
     prompts afresh
 14. a jog not holding the armed window open: laser_arm_test.c case S10
 15. a press counts only after the button has been seen up: a press that
     began before the wait is not consent (the harness's own presses
     follow a fresh prompt for the same reason)
 16. a job paused on the button is not resumable past a lid open: the lid
     cancels it from the hold - reason reported, the reset banner, no
     alarm, the return to the job start - and a later press resumes
     nothing
 17. a jog cancel (0x85) stops a jog short of its target, Idle; a ^X
     mid-move asks the kernel for its ramp (cnc/stop in GFSINK_ATTR_LOG)
     and lands in Alarm with the position kept: MPos is the X that the
     shipped stream (GFSINK_DUMP) moved, an unreferenced lens is still
     held after $X, and the head does not return to the job start; $X
     recovers to Idle and the next jog ships steps
 18. a ^X during the web-service homing session keeps the lens hold: the
     session's command is ended, the stream owes no stop, the reset lands
     in Alarm, and after $X a Z jog is still refused (error 15) with MPos
     Z unmoved, while a program Z move hits the pinned lens's soft limit
     (ALARM:2)

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


def publish_verdicts(path, stop, verdict):
    """Stand in for the cooling engine. `verdict` is the session's live
    dict: fire_ok, hold, resume_ok, name, armed_ack, ack_after_s. "armed"
    is the engine's acknowledgment that it has taken the controller's
    armed window and applied the run airflow; the controller refuses to
    fire on a verdict that lacks it, so an engine that never
    acknowledges (armed_ack False) must produce a refused arm and no
    emission. ack_after_s withholds the acknowledgment for that long
    first, which is the real engine's case: it answers on its next
    tick, and the controller has to see the refreshed verdict to get
    past the arm."""
    t0 = time.monotonic()
    while not stop.is_set():
        v = dict(verdict)
        acked = v["armed_ack"] and time.monotonic() - t0 >= v["ack_after_s"]
        body = ('{"ts_mono":%.3f,"fire_ok":%s,"verdict":"%s","hold":%s,'
                '"resume_ok":%s,"armed":%s,"reason":"%s"}'
                % (time.clock_gettime(time.CLOCK_MONOTONIC),
                   "true" if v["fire_ok"] else "false", v["name"],
                   "true" if v["hold"] else "false",
                   "true" if v["resume_ok"] else "false",
                   "true" if acked else "false",
                   "" if v["fire_ok"] else "harness: %s" % v["name"]))
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            f.write(body)
        os.replace(tmp, path)
        stop.wait(0.5)


def wait_for_new(log, needle, n_before, timeout, sock=None):
    """Wait until needle appears in the log more times than n_before: a
    fresh occurrence, never a stale match from an earlier job."""
    end = time.time() + timeout
    while time.time() < end:
        if "".join(log).count(needle) > n_before:
            return True
        if sock is not None:
            read_avail(sock, log, 0.2)
        else:
            time.sleep(0.1)
    return False


class Session:
    """One controller process with the lifecycle overrides applied."""

    def __init__(self, name, fire_ok=True, disarm_s=2, switches=None, conf_extra="",
                 armed_ack=True, ack_after_s=0.0, dump=False, attr_log=False):
        self.name = name
        self.workdir = tempfile.mkdtemp(prefix="laser-lifecycle-")
        # The stream the controller ships, when the case reads it.
        self.dump = os.path.join(self.workdir, "stream") if dump else None
        # The sysfs writes the hardware would get, when the case reads them.
        self.attr_log = os.path.join(self.workdir, "attr.log") if attr_log else None
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
        env.pop("GFSINK_DUMP", None)
        env.pop("GFSINK_ATTR_LOG", None)
        if self.dump:
            env["GFSINK_DUMP"] = self.dump
        if self.attr_log:
            env["GFSINK_ATTR_LOG"] = self.attr_log
        self.switch_file = None
        if switches is not None:
            self.switch_file = os.path.join(self.workdir, "switches")
            self.set_switches(switches)
            env["GF_SWITCH_FILE"] = self.switch_file
        self.stop = threading.Event()
        self.verdict = {"fire_ok": fire_ok, "hold": False, "resume_ok": True, "name": "OK",
                        "armed_ack": armed_ack, "ack_after_s": ack_after_s}
        self.pub = threading.Thread(target=publish_verdicts,
                                    args=(verdict, self.stop, self.verdict),
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

    def set_verdict(self, name="OK", fire_ok=True, hold=False, resume_ok=True):
        """The engine's next verdicts: a pause tier (OVERTEMP: fire
        blocked, hold, no resume), a fail tier (AIRFLOW: the same under a
        name the client ends the job on), or clean."""
        self.verdict.update({"name": name, "fire_ok": fire_ok, "hold": hold,
                             "resume_ok": resume_ok})

    def count(self, needle):
        return "".join(self.log).count(needle)

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

    def mpos_z(self):
        self.sock.sendall(b"?")
        read_avail(self.sock, self.log, 0.3)
        m = re.findall(r"MPos:-?[\d.]+,-?[\d.]+,(-?[\d.]+)", "".join(self.log[-4:]))
        return float(m[-1]) if m else None

    def setting(self, n):
        """A $-setting's value, from a $$ dump."""
        mark = len(self.log)
        send_line(self.sock, "$$", self.log)
        m = re.search(r"\$%d=(-?[\d.]+)" % n, "".join(self.log[mark:]))
        return float(m.group(1)) if m else None

    def attr_count(self, write):
        """How many times the hardware would have been given `write`
        ("<attr> <value>") so far."""
        try:
            with open(self.attr_log) as f:
                return sum(1 for line in f if line.strip() == write)
        except OSError:
            return 0

    def shipped_x_steps(self):
        """The X steps the shipped stream moved, net: a tick byte (bit 7
        clear) with the X step bit (0x01), the X direction bit (0x02) set
        for -X."""
        try:
            with open(self.dump, "rb") as f:
                data = f.read()
        except OSError:
            return 0
        return sum((-1 if b & 0x02 else 1) for b in data if not b & 0x80 and b & 0x01)

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


def test_sender_change_holds_and_rearms():
    """Rule 3 on a machine with a button: a sender change mid-move, spindle
    on, holds the job where the cut stopped; the held job's resume from
    the new sender prompts for the press (collected from the poll, never
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
        x_held = s.mpos_x()
        if x_held is None or not 0.5 < x_held < 4.5:
            fail("[sender-change-rearm] held outside the move (MPos X %s)" % x_held)
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
        print("PASS [sender-change-rearm]: held at X=%.3f, the held job prompted on ~, the "
              "press re-armed it, and it finished at X=%.3f" % (x_held, x_end))
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
    the lid-open cancel is reported, no alarm is raised, the window never
    opens. A press with the lid still open must not arm either."""
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
    the controller reports Run. The prompt and the armed message are
    waited for as fresh occurrences: a press that lands before the
    wait has begun is not consent (rule 15), so the press must follow
    THIS job's prompt, not an earlier job's."""
    prompts, armed = s.count(PROMPT), s.count(ARMED)
    s.send_raw("M4 S100")
    s.send_raw(gcode)
    if not wait_for_new(s.log, PROMPT, prompts, 5, s.sock):
        fail("[%s] no button prompt" % tag)
    s.press_button()
    if not wait_for_new(s.log, ARMED, armed, 5, s.sock):
        fail("[%s] the button press did not arm" % tag)
    if not s.wait_state("Run", 5):
        fail("[%s] the job never reported Run" % tag)


def test_button_pause_resume():
    """Rule 7: outside the arm wait the button is the pause/resume toggle -
    a press while running is a feed hold, a press while held is a cycle
    start; the arming press itself is not a pause press. A travel job takes
    the same toggle, and its resumed move lands on its target."""
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

    # The same toggle on a travel job: no laser, so no arm wait, and the
    # move resumed from the hold lands where it was programmed to.
    s = Session("button-toggle-travel", disarm_s=60, switches=SW_CLOSED)
    try:
        x0 = s.mpos_x()
        if x0 is None:
            fail("[button-toggle-travel] no position in the status report")
        s.send_raw("M5")
        s.send_raw("G91")
        s.send_raw("G1 X40 F300")                       # 8 s
        if not s.wait_state("Run", 5):
            fail("[button-toggle-travel] the travel job never reported Run")
        if s.count(PROMPT):
            fail("[button-toggle-travel] a job with no laser asked for the arm press")
        time.sleep(1.0)
        s.press_button()
        if not s.wait_state("Hold", 3):
            fail("[button-toggle-travel] a press while running did not feed-hold (state %s)" % s.state())
        held = s.mpos_x()
        if held is None or not x0 < held < x0 + 40.0:
            fail("[button-toggle-travel] held at X %s, not part-way from %s" % (held, x0))
        time.sleep(0.5)
        s.press_button()
        if not s.wait_state("Run", 3):
            fail("[button-toggle-travel] a press while held did not resume (state %s)" % s.state())
        if not s.wait_state("Idle", 15):
            fail("[button-toggle-travel] the resumed move did not complete (state %s)" % s.state())
        x1 = s.mpos_x()
        if x1 is None or abs(x1 - x0 - 40.0) > 0.05:
            fail("[button-toggle-travel] the resumed move ended at X %s, not %.3f" % (x1, x0 + 40.0))
        print("PASS [button-toggle-travel]: a travel job held at %.3f mm and resumed to its target, "
              "%.3f mm" % (held - x0, x1 - x0))
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


def test_cancel_and_abort():
    """Rule 17: a jog cancel (0x85) stops a jog short of its target and
    leaves the controller Idle. A ^X mid-move is a controlled abort: the
    kernel is told to stop with its ramp, Alarm, the position kept, and
    no return to the job start (that move is the lid policy's alone). The
    position kept is the X that the shipped stream moved, and the lens,
    which has no reference here, is still held where it stands. $X
    recovers to Idle, the head does not move on its own, and the next jog
    ships steps."""
    s = Session("cancel-abort", disarm_s=60, dump=True, attr_log=True)
    try:
        spm = s.setting(100)
        if not spm:
            fail("[cancel-abort] no $100 in the settings dump")
        x0 = s.mpos_x()
        if x0 is None:
            fail("[cancel-abort] no position in the status report")
        s.send_raw("$J=G91X40F2400")
        if not s.wait_state("Jog", 3):
            fail("[cancel-abort] the jog never reported Jog (state %s)" % s.state())
        time.sleep(0.3)
        s.sock.sendall(b"\x85")
        if not s.wait_state("Idle", 5):
            fail("[cancel-abort] not Idle after the jog cancel (state %s)" % s.state())
        x1 = s.mpos_x()
        if x1 is None or not 0.5 < x1 - x0 < 39.0:
            fail("[cancel-abort] the jog cancel did not stop short of the target (X %s from %.3f)"
                 % (x1, x0))

        s.send_raw("M5")
        s.send_raw("G91")
        s.send_raw("G1 X120 F3000")                     # 2.4 s
        if not s.wait_state("Run", 5):
            fail("[cancel-abort] the move never reported Run (state %s)" % s.state())
        time.sleep(0.5)
        stops = s.attr_count("cnc/stop 1")
        s.sock.sendall(b"\x18")
        if not s.wait_state("Alarm", 5):
            fail("[cancel-abort] ^X mid-move did not land in Alarm (state %s)" % s.state())
        if s.attr_count("cnc/stop 1") <= stops:
            fail("[cancel-abort] ^X mid-move did not ask the kernel to stop with its ramp "
                 "(no cnc/stop write)")
        x2 = s.mpos_x()
        if x2 is None or not 0.5 < x2 - x1 < 119.5:
            fail("[cancel-abort] the abort did not keep a plausible position (X %s from %.3f)" % (x2, x1))
        moved = s.shipped_x_steps()
        if abs(x2 * spm - moved) > 0.5:
            fail("[cancel-abort] the position after the abort is not the X the stream moved "
                 "(X %.3f is %.1f steps, the stream moved %d)" % (x2, x2 * spm, moved))
        send_line(s.sock, "$X", s.log)
        if not s.wait_state("Idle", 5):
            fail("[cancel-abort] $X did not recover to Idle (state %s)" % s.state())
        read_avail(s.sock, s.log, 1.0)
        if "returned to the job start" in "".join(s.log):
            fail("[cancel-abort] a sender abort triggered the return to the job start")
        x3 = s.mpos_x()
        if x3 is None or abs(x3 - x2) > 0.05:
            fail("[cancel-abort] the head moved on its own after the abort (X %s, was %.3f)" % (x3, x2))
        # The lens has no reference in this session, so it is held where it
        # stands, and the abort keeps it held.
        mark = len(s.log)
        if send_line(s.sock, "$J=G91Z1F100", s.log, tolerate_error=True) != "error" or \
                "error:15" not in "".join(s.log[mark:]):
            fail("[cancel-abort] a Z jog after the abort was not refused: the lens is no longer held")
        shipped = os.path.getsize(s.dump) if os.path.exists(s.dump) else 0
        s.send_raw("$J=G91X-5F2400")
        if not s.wait_state("Jog", 3) or not s.wait_state("Idle", 5):
            fail("[cancel-abort] the jog after $X did not run (state %s)" % s.state())
        grew = (os.path.getsize(s.dump) if os.path.exists(s.dump) else 0) - shipped
        if grew <= 0:
            fail("[cancel-abort] the jog after $X shipped no steps")
        print("PASS [cancel-abort]: 0x85 stopped the jog at %.3f mm of 40, Idle; ^X mid-move asked "
              "for the kernel's stop and landed in Alarm at %.3f mm, the X the stream moved, with no "
              "return and the lens still held; $X recovered to Idle and the next jog shipped %d bytes"
              % (x1 - x0, x2 - x1, grew))
    finally:
        s.close()


HOME_SESSION_CMD = "sleep 47.25"


def home_sessions():
    """How many processes of the web-service homing session's command are
    running (the command is one no other process on the host uses)."""
    r = subprocess.run(["pgrep", "-f", HOME_SESSION_CMD], capture_output=True, text=True)
    return len(r.stdout.split())


def test_homing_abort_keeps_z_hold():
    """Rule 18: a ^X during the web-service homing session keeps the lens
    hold. The session owns the device while it runs, so the reset reads
    nothing back and the position stays lost; the lens, unreferenced in
    this session, must still be held where it stands once $X has cleared
    the alarm. The session's command is ended by the reset, and the stream
    owes the kernel no stop. A program Z move then hits the pinned lens's
    soft limit."""
    s = Session("home-reset", disarm_s=60, attr_log=True,
                conf_extra="homing_mode = gfcloud\ngfcloud_home_cmd = %s\n" % HOME_SESSION_CMD)
    try:
        if home_sessions():
            fail("[home-reset] a homing session's command is already running on the host")
        mark = len(s.log)
        if send_line(s.sock, "$J=G91Z1F100", s.log, tolerate_error=True) != "error" or \
                "error:15" not in "".join(s.log[mark:]):
            fail("[home-reset] the lens is not held before the session: a Z jog was not refused")
        send_line(s.sock, "", s.log)
        z0 = s.mpos_z()
        if z0 is None:
            fail("[home-reset] no position in the status report")
        stops = s.attr_count("cnc/stop 1")
        s.send_raw("$H")
        if not s.wait_state("Home", 5):
            fail("[home-reset] $H never reported Home (state %s)" % s.state())
        time.sleep(0.5)
        if not home_sessions():
            fail("[home-reset] the homing session's command is not running")
        mark = len(s.log)
        s.sock.sendall(b"\x18")
        if not s.wait_state("Alarm", 8):
            fail("[home-reset] ^X during the homing session did not land in Alarm (state %s)"
                 % s.state())
        read_avail(s.sock, s.log, 0.5)
        if "ALARM:6" not in "".join(s.log[mark:]):
            fail("[home-reset] the reset during homing did not raise ALARM:6")
        end = time.time() + 3
        while home_sessions() and time.time() < end:
            time.sleep(0.1)
        if home_sessions():
            fail("[home-reset] the homing session's command outlived the reset")
        if s.attr_count("cnc/stop 1") != stops:
            fail("[home-reset] the reset during the session wrote cnc/stop: the stream owed no stop")
        send_line(s.sock, "$X", s.log)
        if not s.wait_state("Idle", 5):
            fail("[home-reset] $X did not recover to Idle (state %s)" % s.state())
        mark = len(s.log)
        if send_line(s.sock, "$J=G91Z1F100", s.log, tolerate_error=True) != "error" or \
                "error:15" not in "".join(s.log[mark:]):
            fail("[home-reset] a Z jog after the reset was not refused: the lens hold was dropped")
        time.sleep(0.6)
        z1 = s.mpos_z()
        if z1 is None or abs(z1 - z0) > 0.001:
            fail("[home-reset] the lens moved after the reset (Z %s, was %.3f)" % (z1, z0))
        send_line(s.sock, "", s.log)
        mark = len(s.log)
        s.send_raw("G91 G1 Z1 F100")
        if read_avail(s.sock, s.log, 5.0, until=("ALARM:", "ok")) != "ALARM:" or \
                "ALARM:2" not in "".join(s.log[mark:]):
            fail("[home-reset] a program Z move did not hit the pinned lens's soft limit (ALARM:2)")
        # The soft-limit alarm asks for a reset before $X unlocks it: one
        # sent once a status report has come from the alarm's wait. A ^X
        # on the heels of the ALARM line lands while the soft limit's own
        # reset is still pending and is folded into it. The '?' polls keep
        # the controller's output flowing while it waits for the banner.
        if not s.wait_state("Alarm", 5):
            fail("[home-reset] no status report from the soft-limit alarm (state %s)" % s.state())
        banners = s.count("GrblHAL")
        s.sock.sendall(b"\x18")
        end = time.time() + 5
        while s.count("GrblHAL") <= banners and time.time() < end:
            s.state()
        if s.count("GrblHAL") <= banners:
            fail("[home-reset] no reset banner after the ^X that clears the soft limit")
        send_line(s.sock, "$X", s.log)
        if not s.wait_state("Idle", 5):
            fail("[home-reset] $X after the soft limit did not recover to Idle (state %s)" % s.state())
        print("PASS [home-reset]: ^X during the homing session ended its command, wrote no "
              "cnc/stop, landed in ALARM:6; after $X the Z jog was refused with Z at %.3f and "
              "a program Z move raised ALARM:2" % z1)
    finally:
        s.close()


def test_lid_cancel_from_hold():
    """Rule 16: a job paused on the button is not resumable past a lid
    open. The lid cancels it from the hold as it does from Run - the
    reason reported, the reset banner, no alarm, the head back at the job
    start - and a press once the lid is closed again resumes nothing."""
    s = Session("hold-lid-cancel", disarm_s=60, switches=SW_CLOSED)
    try:
        x0 = s.mpos_x()
        if x0 is None:
            fail("[hold-lid-cancel] no position in the status report")
        s.send_raw("M5")
        s.send_raw("G91")
        s.send_raw("G1 X40 F300")                       # 8 s
        if not s.wait_state("Run", 5):
            fail("[hold-lid-cancel] the job never reported Run")
        time.sleep(1.0)
        s.press_button()
        if not s.wait_state("Hold", 3):
            fail("[hold-lid-cancel] the press did not feed-hold (state %s)" % s.state())
        x_held = s.mpos_x()
        s.set_switches(SW_LID_OPEN)
        if not wait_for(s.log, "lid opened - job canceled", 5, s.sock):
            fail("[hold-lid-cancel] the lid open did not cancel the held job")
        if not wait_for(s.log, "for help]", 5, s.sock):
            fail("[hold-lid-cancel] no reset banner after the cancel")
        if not wait_for(s.log, "returned to the job start", 15, s.sock):
            fail("[hold-lid-cancel] the head did not report returning to the job start")
        if not s.wait_state("Idle", 5):
            fail("[hold-lid-cancel] not Idle after the return (state %s)" % s.state())
        if "ALARM" in "".join(s.log):
            fail("[hold-lid-cancel] an alarm was raised on the cancel (position should be kept)")
        x_end = s.mpos_x()
        if x_end is None or abs(x_end - x0) > 0.05:
            fail("[hold-lid-cancel] head not back at the job start: MPos X=%s (start %.3f, held "
                 "at %s)" % (x_end, x0, x_held))
        s.set_switches(SW_CLOSED)
        time.sleep(0.5)
        s.press_button()
        read_avail(s.sock, s.log, 1.0)
        st = s.state()
        x_after = s.mpos_x()
        if not st.startswith("Idle") or x_after is None or abs(x_after - x_end) > 0.05:
            fail("[hold-lid-cancel] a press after the cancel resumed the job (state %s, X=%s)"
                 % (st, x_after))
        print("PASS [hold-lid-cancel]: held at X=%.3f, the lid canceled the held job, reset "
              "banner, no alarm, returned to X=%.3f; a later press resumed nothing"
              % (x_held, x_end))
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


def test_sender_change_during_rearm_cancels():
    """Rule 13: a sender change while a re-arm waits for the press
    cancels the re-arm; the press resumes nothing and the job stays
    held; the new sender's ~ prompts afresh."""
    s = Session("rearm-sender-change", disarm_s=60, switches=SW_CLOSED)
    try:
        start_armed_move(s, "rearm-sender-change", gcode="G1 X30 F120")
        time.sleep(1.0)
        s.sock.close()                                     # mid-move: held, disarmed
        time.sleep(0.5)
        s.sock = s.connect()
        if s.wait_state("Hold", 5) is None:
            fail("[rearm-sender-change] the job was not held on the sender change")
        s.sock.sendall(b"~")
        if not wait_for(s.log, RESUME_PROMPT, 5, s.sock):
            fail("[rearm-sender-change] the resume did not prompt for the button")
        # A second sender change, inside the re-arm wait. The cancel is
        # reported at the disconnect, to nobody (output with no client is
        # discarded), so the evidence is what the press does next: nothing.
        s.sock.close()
        time.sleep(0.5)
        s.sock = s.connect()
        st = read_state(s, '"connected":true')
        if '"arming":false' not in st:
            fail("[rearm-sender-change] the re-arm wait survived the sender change: %r" % st)
        # A press now is the machine's resume button for the new sender:
        # it resumes nothing and arms nothing, it opens a fresh re-arm
        # prompt of its own (the displaced consent is gone), and only a
        # second press, against that prompt, re-arms the held job.
        before = s.armed_count()
        prompts = s.count(RESUME_PROMPT)
        s.press_button()
        read_avail(s.sock, s.log, 1.0)
        if s.armed_count() != before or not s.state().startswith("Hold"):
            fail("[rearm-sender-change] a press after the canceled re-arm resumed the job "
                 "(armed %d -> %d, state %s)" % (before, s.armed_count(), s.state()))
        if not wait_for_new(s.log, RESUME_PROMPT, prompts, 3, s.sock):
            fail("[rearm-sender-change] the press after the cancel did not open a fresh re-arm prompt")
        s.press_button()
        end = time.time() + 10
        while time.time() < end and s.armed_count() != before + 1:
            read_avail(s.sock, s.log, 0.2)
        if s.armed_count() != before + 1:
            fail("[rearm-sender-change] the second press did not re-arm the held job")
        s.sock.sendall(b"\x18")
        print("PASS [rearm-sender-change]: the sender change canceled the re-arm; the next press "
              "resumed nothing and prompted afresh, and the press after it re-armed")
    finally:
        s.close()


def main():
    if not os.path.isfile(BIN):
        fail("controller binary not found at %s" % BIN)
    test_sender_change_during_rearm_cancels()
    test_status_files()
    test_laser_keys_reload()
    test_sender_change_holds_and_rearms()
    test_rx_overrun_aborts()
    test_resume_after_grace()
    test_button_wait_arms()
    test_lid_open_in_wait()
    test_button_pause_resume()
    test_lid_cancels_and_returns()
    test_pause_then_lid_cancel_returns_to_the_job_start()
    test_cancel_and_abort()
    test_homing_abort_keeps_z_hold()
    test_lid_cancel_from_hold()
    test_lid_policy_hold()
    test_sigterm_mid_job()
    print("PASS: the armed-window lifecycle holds")


if __name__ == "__main__":
    main()
