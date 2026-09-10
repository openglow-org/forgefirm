"""The runner: executes one acceptance test or one bench tool at a time.

Acceptance tests run in a worker thread with a Context: log lines, an
operator prompt channel (the page shows the question, the answer comes
back through the API), an abort flag, an evidence dict, hardware helpers,
and the takeover wrapper for tests that need forgectrl out of the way.
Results are appended to the log with the fingerprint the test ran under;
the campaign rules (campaign.py) do the rest.

Bench tools are subprocesses (bench.py registry): same single slot, same
log pane, no campaign effect.

A queue (BATCH_GROUPS) runs what a campaign still owes, one test at a
time through that same single slot: the unattended tests for an empty
room, the attended ones for an operator at the machine. It runs them in
prerequisite order and stops on the first result that is not a PASS,
because a FAIL closes the campaign and going on would quietly open a
second one.

The operator's part of a test is asked for in one of three ways. A
`ready` prompt pre-announces a timed step and waits for the click that
starts it. A `notice` is a standing instruction with no button: the test
shows it and watches the machine for the result. An `act` is a machine
action by name (lid, interlock, button) - a notice for the operator
today, and the seam a bench actuator plugs into - that returns when the
machine shows the action done. A `confirm` stays what it was: a yes/no
the evidence cannot answer.

Safety, in code rather than convention: a live test starts only with the
operator's acknowledgment in the request; the runner never touches the
laser latch; a takeover always ends with forgectrl started again, and a
marker file makes a crash mid-takeover recoverable at the next start.

Runner-level events (a queue opening or stopping, a takeover recovered,
the leftovers a baseline pass found) go to the journal: the daemon's
stderr (daemon.log under the data directory), syslog when there is one,
and the log of the run in progress. The page shows the run, never the
journal.
"""
import logging
import logging.handlers
import os
import random
import subprocess
import threading
import time
import traceback

from . import artifact as _artifact
from . import baseline as _baseline
from . import campaign as _campaign
from . import fixture as _fixture
from . import catalog as _catalog
from . import hw
from .log import now_ts, data_dir

MAX_LINES = 4000

# The two queues the page offers. A campaign is mostly waiting: the
# unattended tests need nobody in the room, the attended ones need the
# operator at the machine, and sorting them that way lets one set run
# while the operator is elsewhere. Each queue takes everything of its
# kinds the campaign does not already count as satisfied.
BATCH_GROUPS = {
    "unattended": ("auto",),
    "attended": ("operator", "live"),
}

# A bench actuator (fixture.py) moves an operator test whose actions it
# covers into the unattended queue. The probe that decides is one GET;
# it is repeated at most this often, and before every run.
FIXTURE_PROBE_S = 30.0


class Aborted(Exception):
    pass


class Failed(Exception):
    pass


journal = logging.getLogger("forgetest")
journal.setLevel(logging.INFO)


def configure_journal(syslog_path="/dev/log"):
    """The daemon's journal: stderr (the init script keeps it in
    daemon.log) and, where the socket exists, syslog under the
    `forgetest` name (the unified log tree files it under system/)."""
    if journal.handlers:
        return journal
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("%(asctime)s forgetest: %(message)s", "%Y-%m-%dT%H:%M:%S"))
    journal.addHandler(h)
    if os.path.exists(syslog_path):
        try:
            sh = logging.handlers.SysLogHandler(address=syslog_path)
            sh.setFormatter(logging.Formatter("forgetest[%(process)d]: %(message)s"))
            sh.ident = ""
            journal.addHandler(sh)
        except OSError:
            pass
    return journal


# The wording of each machine action, for the operator who performs it
# while the bench has no actuator. The text names the action and nothing
# else; a test adds its own context.
ACTION_TEXT = {
    ("lid", "open"): "Open the lid.",
    ("lid", "close"): "Close the lid.",
    ("interlock", "open"): "Open the remote-interlock loop: unplug the Pro's interlock plug, or "
                           "pull the jumper at J8 on a Basic/Plus.",
    ("interlock", "close"): "Restore the remote-interlock loop (plug or jumper back in).",
    ("button", "press"): "Press the button once.",
}
ACT_TIMEOUT_S = 180
# How long a live test waits for the operator to prove they are at the
# machine by pressing its button. Generous: they may be setting up the
# scrap, and nothing fires until it lands.
PRESENCE_TIMEOUT_S = 600


class Run:
    def __init__(self, kind, id, title):
        self.kind = kind          # 'test' | 'bench'
        self.id = id
        self.title = title
        self.started = time.time()
        self.started_ts = now_ts()
        self.lines = []
        self.dropped = 0
        self.prompt = None        # {"id","question","options"}
        self.notice = None        # {"id","text"}: a standing instruction, no button
        self.answers = []
        self.unattended = False   # a fixture-run test: no prompt can be answered
        # The operator proved they are at the machine by pressing its
        # button, so the bench actuator performs every press from there
        # on. Set by Ctx.ready() when an actuator is up to take over.
        self.fixture_takeover = False
        self.evidence = {}
        self.baseline_captured = None   # preserved state the post pass hands back
        self.aborted = threading.Event()
        self.finished = None      # {"result","message","duration_s"}
        self.proc = None
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._answer = None
        self._prompt_seq = 0

    def log(self, msg):
        line = "%s %s" % (time.strftime("%H:%M:%S"), msg)
        with self._lock:
            if len(self.lines) >= MAX_LINES:
                self.lines.pop(0)
                self.dropped += 1
            self.lines.append(line)

    def snapshot(self, tail=200):
        with self._lock:
            lines = self.lines[-tail:]
            prompt = dict(self.prompt) if self.prompt else None
            notice = dict(self.notice) if self.notice else None
        return {
            "kind": self.kind, "id": self.id, "title": self.title,
            # A finished run's clock stops: the page shows the last run
            # until the next one starts, and a live figure there counts
            # the time since, not the time it took.
            "started": self.started_ts,
            "elapsed_s": (self.finished["duration_s"] if self.finished
                          else int(time.time() - self.started)),
            "log": lines, "dropped": self.dropped, "prompt": prompt, "notice": notice,
            "finished": self.finished, "aborting": self.aborted.is_set(),
        }

    # -- prompt channel -----------------------------------------------
    def ask(self, question, options):
        if self.unattended:
            raise Failed("the test asked a person (%r) while running unattended with the fixture: "
                         "declare the step in hands=... so the test stays in the attended queue" % question)
        with self._cv:
            self._prompt_seq += 1
            pid = "p%d" % self._prompt_seq
            self.prompt = {"id": pid, "question": question, "options": list(options)}
            self._answer = None
            while self._answer is None and not self.aborted.is_set():
                self._cv.wait(0.5)
            self.prompt = None
            if self.aborted.is_set() and self._answer is None:
                raise Aborted("aborted at prompt")
            ans = self._answer
            self._answer = None
        self.answers.append({"ts": now_ts(), "question": question, "answer": ans})
        return ans

    def set_notice(self, text):
        with self._lock:
            if text is None:
                self.notice = None
            else:
                self._prompt_seq += 1
                self.notice = {"id": "n%d" % self._prompt_seq, "text": text}

    def answer(self, prompt_id, value):
        with self._cv:
            if not self.prompt or self.prompt["id"] != prompt_id:
                return False
            if value not in self.prompt["options"]:
                return False
            self._answer = value
            self._cv.notify_all()
            return True

    def abort(self):
        self.aborted.set()
        with self._cv:
            self._cv.notify_all()
        p = self.proc
        if p is not None and p.poll() is None:
            try:
                p.terminate()
            except OSError:
                pass


class Context:
    """What a test function gets."""

    def __init__(self, run, runner, test):
        self.run = run
        self.runner = runner
        self.test = test
        self.evidence = run.evidence
        self._forgectrl = None

    # -- reporting -----------------------------------------------------
    def log(self, msg, *args):
        self.run.log(msg % args if args else msg)

    def check(self, cond, msg, *args):
        if not cond:
            raise Failed(msg % args if args else msg)

    def fail(self, msg, *args):
        raise Failed(msg % args if args else msg)

    def aborted(self):
        return self.run.aborted.is_set()

    def checkpoint(self):
        if self.aborted():
            raise Aborted("aborted")

    def sleep(self, seconds):
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.checkpoint()
            time.sleep(min(0.25, max(0.0, deadline - time.time())))

    # -- operator ------------------------------------------------------
    def prompt(self, question, options=("Yes", "No")):
        self.log("PROMPT: %s", question)
        ans = self.run.ask(question, options)
        self.log("ANSWER: %s", ans)
        return ans

    def confirm(self, question):
        """Yes/No question; a No is a test failure."""
        ans = self.prompt(question, ("Yes", "No"))
        if ans != "Yes":
            raise Failed("operator answered No: %s" % question)
        return True

    def instruct(self, text):
        """A step for the operator; continues on Done, fails on Cannot."""
        ans = self.prompt(text, ("Done", "Cannot"))
        if ans != "Done":
            raise Failed("operator could not: %s" % text)

    def ready(self, text):
        """Pre-announce a timed step and wait for the operator to say the
        machine is set up.

        Where an actuator is up and wired to the button, the operator
        proves they are at the machine by pressing the machine's own
        button, and the actuator then performs every press in the test.
        A person doing timing-critical presses in the middle of a live
        cut is how a press count becomes unknowable: a press that lands
        late, or twice, cannot be told apart afterwards from the machine
        misbehaving. The button does nothing at Idle, so this press is
        only a presence check.

        With no actuator the operator does the presses and answers on the
        page, as before. With the run unattended there is nobody to
        announce it to and the gate passes at once, logged."""
        if self.run.unattended:
            self.log("READY (fixture performs the step): %s", text)
            return

        # Presence is proved once per test, not once per gate. A test with
        # two armed halves (the kill drill stops the controller and does it
        # again) reaches this twice, and asking a second time is friction
        # with nothing behind it: the operator proved presence a moment ago
        # and the actuator is already making the presses. The setup line
        # still goes up, because the second half may want the scrap moved.
        if self.run.fixture_takeover:
            self.notice(text)
            self.log("READY: presence already proved this test; the bench has the presses")
            return

        fixture = getattr(self.runner, "fixture", None) if self.runner is not None else None
        if fixture is not None and fixture.covers("button"):
            self.notice(text + " Then press the button on the machine to start.")
            self.log("READY: waiting for the operator's press on the machine "
                     "(the bench then does every press in this test)")
            pressed = self.wait_for(lambda: self.switch("button") is True, PRESENCE_TIMEOUT_S)
            self.clear_notice()
            if pressed is None:
                raise Failed("no press on the machine within %d s: %s"
                             % (PRESENCE_TIMEOUT_S, text))
            # Let the button come back up, so this press is never read as
            # the arm press or a pause toggle.
            self.wait_for(lambda: self.switch("button") is False, 10)
            self.run.fixture_takeover = True
            self.evidence.setdefault("actions", []).append(
                {"channel": "button", "state": "presence", "by": "operator",
                 "took_s": round(pressed, 2), "ts": now_ts()})
            self.log("READY: operator present (press after %.1f s); the bench takes the presses", pressed)
            return

        ans = self.prompt(text, ("Ready", "Cannot"))
        if ans != "Ready":
            raise Failed("operator could not: %s" % text)

    def notice(self, text):
        """A standing instruction with no button: the page shows it
        until clear_notice(), while the test watches the machine for the
        result. Logged like a prompt."""
        self.log("NOTICE: %s", text)
        self.run.set_notice(text)

    def clear_notice(self):
        self.run.set_notice(None)

    def wait_for(self, cond, timeout, poll=0.25):
        """Poll `cond()` until true; the seconds it took, or None on
        timeout. Abort-aware. An error in cond counts as not yet."""
        t0 = time.time()
        while time.time() - t0 < timeout:
            self.checkpoint()
            try:
                if cond():
                    return time.time() - t0
            except Exception:  # noqa: BLE001 - a transient read error is "not yet"
                pass
            time.sleep(poll)
        return None

    def switch(self, name):
        """One of forgectrl's switch readings (lid, interlock_ok, ...)."""
        return (self.forgectrl.status().get("switches") or {}).get(name)

    def act(self, channel, state, until=None, timeout=ACT_TIMEOUT_S, text="", fail=True):
        """A machine action by name: ("lid", "open"|"close"),
        ("interlock", "open"|"close"), ("button", "press"). The bench's
        actuator performs it when one covers the channel (the runner's
        `fixture`; none exists yet); otherwise the operator does, told by
        a standing notice, and the test watches the machine for the
        result - the switch reaching the state, or `until()` true
        (required for the button, whose press is proven by what it does).
        `text` adds the test's own context after the action's wording.
        Returns the seconds the action took; with fail=False a timeout
        returns None instead of failing the test."""
        key = (channel, state)
        if key not in ACTION_TEXT:
            raise ValueError("unknown action %r" % (key,))
        if until is None:
            if channel == "button":
                raise ValueError("a button press needs `until`: what the press is expected to do")
            want_open = state == "open"
            if channel == "lid":
                until = lambda: self.switch("lid") is (not want_open)  # noqa: E731
            else:
                until = lambda: self.switch("interlock_ok") is (not want_open)  # noqa: E731
        wording = ACTION_TEXT[key] + ((" " + text) if text else "")
        fixture = getattr(self.runner, "fixture", None) if self.runner is not None else None
        rec = {"channel": channel, "state": state, "by": "operator", "ts": now_ts()}
        self.evidence.setdefault("actions", []).append(rec)
        # The test began with the bench taking the presses. If it is gone
        # now, say so loudly: falling back to the operator silently is
        # what turns one dropped actuator into a press nobody can account
        # for afterwards.
        if self.run.fixture_takeover and not (fixture is not None and fixture.covers(channel)):
            rec["fixture_lost"] = True
            self.log("ACT %s %s: WARNING the bench actuator took this test's presses and is now "
                     "gone - asking the operator instead; a press from here on is a person's",
                     channel, state)
        if fixture is not None and fixture.covers(channel):
            self.log("ACT %s %s (fixture)", channel, state)
            rec["by"] = "fixture"
            try:
                fixture.act(channel, state)
            except _fixture.FixtureError as e:
                rec["fixture_error"] = str(e)
                if self.run.unattended:
                    # nobody is in the room to do it instead: the run ends
                    # here, as the harness's failure, not the machine's
                    self.log("ACT %s %s: fixture failed (%s) - unattended, nobody to ask", channel, state, e)
                    raise
                # the box did not do it: the operator is asked instead,
                # and the record says so
                self.log("ACT %s %s: fixture failed (%s) - asking the operator", channel, state, e)
                rec["by"] = "operator"
                self.notice(wording)
        else:
            self.notice(wording)
        try:
            dt = self.wait_for(until, timeout)
        finally:
            if rec["by"] == "operator":
                self.clear_notice()
        rec["took_s"] = round(dt, 2) if dt is not None else None
        if dt is None:
            self.log("ACT %s %s: not seen within %d s", channel, state, timeout)
            if fail:
                raise Failed("%s %s was not seen on the machine within %d s" % (channel, state, timeout))
            return None
        self.log("ACT %s %s: done after %.1f s", channel, state, dt)
        return dt

    def press_now(self, text="The button is lit white. Press it now: the laser fires after your press."):
        """The arm press, at the moment the machine says it is waiting.

        A check that runs a job opens a `press` prompt of its own when
        it sees the button lit and the tube still dark, which is the
        machine's own word that the press it wants is this one. Pressing
        on that is exact; pressing on a button LED read from here is
        not, because the button is lit through parts of a check that are
        not the arm (the lens reference, the program on its way to the
        controller), and a press then lands before the job waits for it
        and is lost.

        The fixture presses where the bench took this test's presses (or
        opted in standing); otherwise the notice goes up for a person,
        who is looking at the same prompt. Returns True when the fixture
        pressed."""
        fixture = getattr(self.runner, "fixture", None) if self.runner is not None else None
        rec = {"channel": "button", "state": "arm", "by": "operator", "ts": now_ts()}
        self.evidence.setdefault("actions", []).append(rec)
        opted_in = fixture is not None and fixture.covers("button") and \
            (fixture.arm_press or self.run.fixture_takeover)
        if not opted_in:
            if self.run.fixture_takeover:
                rec["fixture_lost"] = True
                self.log("ARM: WARNING the bench actuator took this test's presses and is now "
                         "gone - asking the operator for the arm press")
            self.notice(text)
            return False
        try:
            fixture.act("button", "press")
        except _fixture.FixtureError as e:
            rec["fixture_error"] = str(e)
            self.log("ARM press: fixture failed (%s) - asking the operator", e)
            self.notice(text)
            return False
        rec["by"] = "fixture"
        self.log("ARM press by the fixture, at the machine's own press prompt")
        return True

    def arm_press(self, text="The button lights white: press it to arm. The machine fires after your press.",
                  lit_timeout=60):
        """The arm cue of a live test. A person's press by default: a
        standing notice until the caller clears it. The fixture presses
        only where the bench opted in (arm_press in its config) and its
        button channel is enabled: a thread waits up to `lit_timeout`
        seconds for the button to light (the job may still be on its way
        to the arm wait; a card that settles the coolant first takes
        minutes) and presses once, recorded as the fixture's; if the
        button never lights or the press fails, the notice goes up for a
        person. Returns True when the fixture has been asked."""
        fixture = getattr(self.runner, "fixture", None) if self.runner is not None else None
        rec = {"channel": "button", "state": "arm", "by": "operator", "ts": now_ts()}
        self.evidence.setdefault("actions", []).append(rec)
        # A presence press at the ready gate hands this test's presses to
        # the bench, whatever the config's standing opt-in says: the
        # operator has already proved they are at the machine, which is
        # the thing the opt-in exists to establish.
        opted_in = fixture is not None and fixture.covers("button") and \
            (fixture.arm_press or self.run.fixture_takeover)
        if not opted_in:
            if self.run.fixture_takeover:
                rec["fixture_lost"] = True
                self.log("ARM: WARNING the bench actuator took this test's presses and is now "
                         "gone - asking the operator for the arm press")
            self.notice(text)
            return False
        self.log("ARM: the fixture presses when the button lights (the bench's arm_press opt-in)")

        def press():
            lit = self.wait_for(hw.button_lit, lit_timeout)
            if lit is None:
                self.log("ARM: the button never lit within %d s - asking the operator", lit_timeout)
                self.notice(text)
                return
            try:
                fixture.act("button", "press")
            except _fixture.FixtureError as e:
                self.log("ARM press: fixture failed (%s) - asking the operator", e)
                rec["fixture_error"] = str(e)
                self.notice(text)
                return
            rec["by"] = "fixture"
            rec["took_s"] = round(lit, 2)
            self.log("ARM press by the fixture, button lit after %.1f s", lit)

        threading.Thread(target=press, daemon=True, name="forgetest-arm-press").start()
        return True

    # -- hardware ------------------------------------------------------
    @property
    def forgectrl(self):
        if self._forgectrl is None:
            self._forgectrl = hw.Forgectrl()
        return self._forgectrl

    def sysfs(self, attr, default=None):
        return hw.sysfs_read(attr, default)

    def sysfs_int(self, attr, default=None):
        return hw.sysfs_int(attr, default)

    def grbl(self):
        return hw.Grbl()

    def takeover(self):
        return Takeover(self.run.log, self.test.id)

    def counters_rezeroed(self):
        """Tell the baseline the kernel position counters were re-zeroed
        at the head's starting position during this run (cloud mode's
        connect clears them): counters at (0,0,0) afterward mean the head
        is back where the run found it."""
        cap = self.run.baseline_captured
        if cap and cap.get("position") is not None:
            cap["position"] = [0, 0, 0]
            self.log("position counters re-zeroed at the starting position; the baseline "
                     "expects (0,0,0) at the end")

    def mode_changed(self, mode):
        """Declare a deliberate controller-mode change for the operator:
        the run leaves the machine in `mode` and the baseline keeps it
        there instead of switching back to the mode the run found (the
        cloud tests enter cloud mode once and stay)."""
        cap = self.run.baseline_captured
        if cap is not None and cap.get("mode") != mode:
            self.log("controller mode changed to %s for the operator; the baseline keeps it", mode)
            cap["mode"] = mode


class Takeover:
    """Hardware takeover: the controller is stopped through the supervisor,
    forgectrl is stopped, the marker records the ownership, and forgectrl
    is started again on every exit path. Used by takeover tests (through
    Context.takeover()) and by takeover bench tools."""

    # Controller-owned kernel attributes a takeover drill may change:
    # captured on enter, written back on exit before forgectrl starts, so
    # the supervisor's liveness probe runs on the machine it expects (a
    # leftover motor_lock=15 masks the probe's steps: no motion by
    # construction, a false driver-wedge verdict, the rail-off ladder).
    PRESERVE = ("cnc/motor_lock", "cnc/step_freq", "cnc/ramp_rate", "cnc/streaming",
                "cnc/x_mode", "cnc/y_mode", "cnc/x_decay", "cnc/y_decay",
                "pic/x_step_current", "pic/y_step_current")

    def __init__(self, log, who):
        self.log = log            # callable(str)
        self.who = who
        self.marker = marker_path()
        self.saved = {}

    def wait_settled(self):
        return _baseline.Baseline(self.log).wait_settled()

    def restore_attrs(self):
        """Write the captured kernel attributes back and relock the latch."""
        for attr, val in self.saved.items():
            try:
                hw.sysfs_write(attr, val)
            except OSError as e:
                self.log("takeover: WARNING could not restore %s=%s: %s" % (attr, val, e))
        try:
            hw.sysfs_write("cnc/laser_latch", "1")
        except OSError as e:
            self.log("takeover: WARNING could not relock the latch: %s" % e)

    def __enter__(self):
        log = self.log
        log("takeover: waiting for forgectrl to be settled")
        self.wait_settled()
        for attr in self.PRESERVE:
            v = hw.sysfs_read(attr)
            if v is not None:
                self.saved[attr] = v
        if self.saved:
            log("takeover: preserving %s" % ", ".join("%s=%s" % kv for kv in self.saved.items()))
        log("takeover: stopping the controller through forgectrl")
        try:
            st, body = hw.Forgectrl().post("/controller/stop")
            log("takeover: POST /controller/stop -> %s" % st)
        except hw.HwError as e:
            log("takeover: forgectrl unreachable (%s)" % e)
        with open(self.marker, "w") as f:
            f.write("%s %s\n" % (now_ts(), self.who))
        rc, out = hw.initd("forgectrl", "stop")
        log("takeover: forgectrl stop -> rc %s" % rc)
        deadline = time.time() + 15
        # Every holder of the pulse device: the daemon, the GRBL controller,
        # and the cloud client (a script, so found by its command line).
        def holders():
            return hw.pidof("forgectrl") + hw.pidof("grblHAL_glowfor") + hw.pgrep_f("gfcloud.py")
        while time.time() < deadline and holders():
            time.sleep(0.5)
        left = holders()
        if left:
            self.__exit__(None, None, None)
            raise Failed("takeover: processes still alive after stop: %s" % left)
        log("takeover: pulse device free")
        return self

    def __exit__(self, exc_type, exc, tb):
        self.restore_attrs()
        rc, out = hw.initd("forgectrl", "start")
        self.log("takeover: forgectrl start -> rc %s" % rc)
        try:
            os.remove(self.marker)
        except OSError:
            pass
        # leave the machine settled for whatever runs next: the probe
        # move done, the controller back (or the ladder's verdict logged)
        self.wait_settled()
        return False


def marker_path():
    return os.environ.get("FORGETEST_MARKER") or "/run/forgetest.active"


class Runner:
    def __init__(self, log, manifest, registry, bench=None):
        self.log = log
        self.manifest = manifest
        self.registry = registry
        self.bench = bench
        self.catalog_hash = _catalog.catalog_hash(registry)
        self._lock = threading.Lock()
        self.current = None
        self.last = None
        self.batch = None
        self.fixture = None       # the bench actuator, when one is up (fixture.py)
        self._fixture_probed = 0.0
        self._fixture_lock = threading.Lock()
        self._fixture_said = None
        self.boot_ref = None
        self.recover()
        threading.Thread(target=self._take_boot_reference, daemon=True,
                         name="forgetest-bootref").start()

    def _take_boot_reference(self):
        try:
            self.boot_ref = _baseline.boot_reference(self._note, data_dir())
        except Exception as e:  # noqa: BLE001
            self._note("baseline: boot reference failed: %s: %s" % (type(e).__name__, e))

    def _note(self, msg):
        """A runner-level line: to the journal, and to the run in progress."""
        journal.info(msg)
        r = self.current
        if r is not None and not r.finished:
            r.log(msg)

    # -- the bench actuator ------------------------------------------------
    def probe_fixture(self, force=False):
        """The fixture, up and answering, or None; re-probed at most every
        FIXTURE_PROBE_S unless forced (before a run). A probe that finds
        it gone, or a config that appeared, changes the queues' routing
        from then on. One probe at a time, and the probe counts from its
        completion: a caller that arrives while another's probe is in
        flight (a page poll and a queue start right after the daemon
        came up) waits for its answer instead of reading the stale
        fixture, which routed the fixture's tests to nobody."""
        with self._fixture_lock:
            if not force and time.time() - self._fixture_probed < FIXTURE_PROBE_S:
                return self.fixture
            had = self.fixture
            # a box that is not there is said once, not every probe
            said = []
            fx = _fixture.probe(said.append)
            for m in said:
                if m != self._fixture_said:
                    self._note(m)
                    self._fixture_said = m
            if fx is not None:
                self._fixture_said = None
            if fx is None and had is not None:
                self._note("fixture: %s no longer answers - running without it" % had.hostname)
            self.fixture = fx
            self._fixture_probed = time.time()
            return fx

    def fixture_channels(self):
        """The channels the fixture covers right now (the button only
        with its enable jumper in)."""
        fx = self.probe_fixture()
        if fx is None:
            return ()
        return tuple(c for c in fx.channels if fx.covers(c))

    def fixture_release(self, run):
        """After a run: any channel the fixture still holds energized is
        released and recorded, like any other leftover."""
        fx = self.fixture
        if fx is None:
            return
        try:
            held = fx.energized(fx.status())
            if held:
                fx.release()
                run.log("fixture: released %s left energized" % ", ".join(held))
            run.evidence.setdefault("fixture", {})["released"] = held
        except _fixture.FixtureError as e:
            run.log("fixture: release check failed: %s" % e)
            run.evidence.setdefault("fixture", {})["release_error"] = str(e)

    # -- startup recovery ------------------------------------------------
    def recover(self):
        m = marker_path()
        if os.path.exists(m):
            try:
                with open(m) as f:
                    who = f.read().strip()
            except OSError:
                who = "?"
            self._note("recovered a takeover left by '%s': starting forgectrl" % who)
            hw.initd("forgectrl", "start")
            try:
                os.remove(m)
            except OSError:
                pass

    # -- state -----------------------------------------------------------
    def tests(self):
        return _catalog.all_tests(self.registry)

    def running_id(self):
        r = self.current
        return r.id if r and r.kind == "test" and not r.finished else None

    def state(self):
        records = self.log.read()
        st = _campaign.compute(records, self.tests(), self.manifest, self.catalog_hash,
                               running=self.running_id())
        st["catalog_hash"] = self.catalog_hash
        st["manifest"] = {"sha": self.manifest.content_sha, "identity": self.manifest.identity_sha(),
                          "image": self.manifest.image_name, "version": self.manifest.version}
        st["log_corrupt"] = self.log.corrupt
        r = self.current
        st["running"] = r.snapshot() if r and not r.finished else None
        last = r if (r and r.finished) else self.last
        st["last_run"] = last.snapshot() if last else None
        st["batch"] = self.batch_snapshot()
        # what each queue would run if started now, so the page can label
        # its buttons with the work rather than a bare verb
        st["batch_available"] = {g: self.batch_selection(g, st) for g in BATCH_GROUPS}
        fx = self.fixture
        st["fixture"] = fx.summary() if fx is not None else None
        return st, records

    def busy(self):
        r = self.current
        return bool(r and not r.finished)

    # -- campaign actions -------------------------------------------------
    def _open_campaign_if_needed(self, state):
        if state["campaign"]:
            return state["campaign"]
        cid = "c-%s-%04x" % (time.strftime("%Y%m%d%H%M%S", time.gmtime()), random.randrange(1 << 16))
        rec = self.log.append({"t": "campaign", "id": cid, "manifest_sha": self.manifest.content_sha,
                               "catalog_hash": self.catalog_hash, "image": self.manifest.version})
        return rec

    def invalidate(self, reason):
        reason = (reason or "").strip()
        if not reason:
            return False, "a reason is required"
        if self.busy():
            return False, "a run is in progress"
        self.log.append({"t": "invalidate", "reason": reason})
        return True, "all results invalidated; a full campaign is required"

    def reset(self, reason=""):
        if self.busy():
            return False, "a run is in progress"
        self.log.append({"t": "reset", "reason": (reason or "").strip()})
        return True, "campaign reset"

    def export(self):
        state, records = self.state()
        art = _artifact.build(state, self.tests(), self.manifest, records, self.catalog_hash)
        self.log.append({"t": "export", "artifact_sha256": art["sha256"], "authorized": art["authorized"],
                         "campaign": (state["campaign"] or {}).get("id")})
        return art

    # -- starting -----------------------------------------------------------
    def start_test(self, test_id, ack_live=False, ignore_requires=False):
        """Start a test. `requires` gates the start unless the operator
        set ignore_requires: the test then runs alone, and the run's
        evidence records which prerequisites were unmet (the release gate
        needs every test satisfied anyway, so nothing is hidden - the
        record just says the order was the operator's)."""
        if self.batch_active():
            return False, "a queue is running"
        ok, msg, _ = self._start_test(test_id, ack_live, ignore_requires)
        return ok, msg

    def _start_test(self, test_id, ack_live=False, ignore_requires=False, batch=None):
        """As start_test, and hands back the Run so the queue driver can
        follow it without racing another start for `current`."""
        t = _catalog.get(test_id, self.registry)
        if t is None:
            return False, "unknown test", None
        with self._lock:
            if self.busy():
                return False, "a run is in progress", None
            state, _ = self.state()
            ts = state["tests"][t.id]
            missing = list(ts["missing_requires"])
            if missing and not ignore_requires:
                return False, "prerequisites not satisfied: %s" % ", ".join(missing), None
            if t.kind == "live" and not ack_live:
                return False, "live test: acknowledge eye protection, fire watch, and exhaust first", None
            reason = t.cannot_start()
            if reason:
                return False, "cannot start: %s" % reason, None
            campaign = self._open_campaign_if_needed(state)
            run = Run("test", t.id, t.title)
            self.last = self.current
            self.current = run
        run.log("start %s (%s, %s) in campaign %s" % (t.id, t.kind, t.hardware, campaign["id"]))
        fx = self.probe_fixture(force=True)
        if fx is not None:
            run.log("fixture: %s at %s covers %s" % (fx.hostname, fx._ip, ", ".join(self.fixture_channels())))
            run.evidence["fixture"] = {"hostname": fx.hostname, "ip": fx._ip,
                                       "channels": list(self.fixture_channels())}
            # nobody is expected in the room for a test the fixture runs
            # in the unattended queue: a prompt there is a defect, not a wait
            if batch is not None and batch.get("group") == "unattended" and t.kind != "auto":
                run.unattended = True
                run.log("fixture: running unattended (no prompt can be answered)")
        if missing:
            run.log("prerequisites overridden by the operator - not satisfied: %s" % ", ".join(missing))
            run.evidence["prerequisites"] = {"overridden": True, "missing": missing, "ts": now_ts()}
        if t.kind == "live":
            run.evidence["operator"] = {"ack_live": True, "ts": now_ts()}
        if batch:
            run.log("queued by the %s queue opened %s" % (batch["group"], batch["ts"]))
            run.evidence["batch"] = {"group": batch["group"], "ts": batch["ts"]}
        th = threading.Thread(target=self._exec_test, args=(t, run, campaign), daemon=True,
                              name="forgetest-run")
        th.start()
        return True, "started", run

    # -- the queue ----------------------------------------------------------
    def batch_active(self):
        b = self.batch
        return bool(b and not b["finished"])

    def batch_selection(self, group, state=None):
        """The ids the given queue would run, in prerequisite order: every
        test of those kinds that the campaign does not already count as
        satisfied, which is exactly what is left to do."""
        kinds = BATCH_GROUPS.get(group)
        if kinds is None:
            return None
        if state is None:
            state, _ = self.state()
        tests = self.tests()
        # an operator test the fixture can run alone goes to the
        # unattended queue and leaves the attended one
        channels = self.fixture_channels()
        routed = set(t.id for t in tests if channels and t.fixture_runnable(channels))
        want = [t.id for t in tests
                if ((t.kind in kinds and t.id not in routed) or (group == "unattended" and t.id in routed))
                and not state["tests"][t.id]["satisfied"]]
        return _catalog.order_by_requires(tests, want)

    def start_batch(self, group, ack_live=False, ignore_requires=False):
        if group not in BATCH_GROUPS:
            return False, "unknown queue", None
        if self.batch_active():
            return False, "a queue is already running", None
        if self.busy():
            return False, "a run is in progress", None
        order = self.batch_selection(group)
        if not order:
            return False, "nothing to run: every %s test is already satisfied" % group, None
        live = [tid for tid in order
                if _catalog.get(tid, self.registry).kind == "live"]
        if live and not ack_live:
            return False, ("this queue fires the laser (%s): acknowledge eye protection, "
                           "fire watch, and exhaust first" % ", ".join(live)), None
        with self._lock:
            if self.batch_active() or self.busy():
                return False, "a run is in progress", None
            self.batch = {"group": group, "ts": now_ts(), "order": list(order), "done": [],
                          "skipped": [], "current": None, "stop": False, "stopped": None,
                          "finished": None, "ack_live": bool(ack_live),
                          "ignore_requires": bool(ignore_requires)}
            b = self.batch
        self._note("queue %s: %d test(s) to run: %s" % (group, len(order), ", ".join(order)))
        threading.Thread(target=self._drive_batch, args=(b,), daemon=True,
                         name="forgetest-queue").start()
        return True, "queue started: %d test(s)" % len(order), list(order)

    def stop_batch(self):
        """Cancel what is still queued. The run in progress finishes and is
        recorded; Abort is the lever that stops that one."""
        b = self.batch
        if not self.batch_active():
            return False, "no queue is running"
        left = len(self.batch_snapshot()["pending"])
        b["stop"] = True
        b["stopped"] = b["stopped"] or "stopped by the operator"
        self._note("queue %s: stop requested, %d test(s) will not run" % (b["group"], left))
        return True, "queue stopped; the run in progress finishes"

    def _drive_batch(self, b):
        """One test at a time, in order, until the queue empties or a run
        comes back anything other than PASS. A FAIL closes the campaign, so
        carrying on would only open a second one behind the operator's
        back; an ABORTED means they asked it to stop."""
        try:
            for tid in b["order"]:
                if b["stop"]:
                    break
                b["current"] = tid
                ok, msg, run = self._start_test(tid, ack_live=b["ack_live"],
                                                ignore_requires=b["ignore_requires"], batch=b)
                if not ok:
                    b["skipped"].append({"test": tid, "reason": msg})
                    self._note("queue %s: skipped %s: %s" % (b["group"], tid, msg))
                    continue
                # stop_batch lets the run in progress finish; Abort is what
                # ends this one, and lands here as a non-PASS result.
                while run.finished is None:
                    time.sleep(0.2)
                result = run.finished["result"]
                b["done"].append({"test": tid, "result": result})
                if result != _campaign.PASS:
                    b["stop"] = True
                    b["stopped"] = "%s on %s" % (result, tid)
                    self._note("queue %s: stopped on %s (%s)" % (b["group"], tid, result))
                    break
        except Exception as e:  # noqa: BLE001 - a broken queue must not wedge the runner
            b["stopped"] = "%s: %s" % (type(e).__name__, e)
            self._note("queue %s: driver errored: %s" % (b["group"], b["stopped"]))
        finally:
            b["current"] = None
            b["finished"] = now_ts()

    def batch_snapshot(self):
        b = self.batch
        if b is None:
            return None
        done_ids = set(x["test"] for x in b["done"]) | set(x["test"] for x in b["skipped"])
        return {"group": b["group"], "ts": b["ts"], "order": list(b["order"]),
                "done": list(b["done"]), "skipped": list(b["skipped"]),
                "pending": [t for t in b["order"] if t not in done_ids and t != b["current"]],
                "current": b["current"], "stopping": bool(b["stop"]) and not b["finished"],
                "stopped": b["stopped"], "finished": b["finished"]}

    # -- baseline around every run -----------------------------------------
    def _baseline_pre(self, run, mode=None):
        """Bring the machine to the fresh-boot idle state before a run and
        record what the previous run left behind; then, for a test that
        declares a controller mode, put the machine in that mode. The
        preserved state is captured after the switch, so the post pass
        keeps the mode the test asked for: a queue that crosses from the
        cloud tests to a motion test switches once, there, and stays.
        Returns the captured preserved state for the post pass."""
        bl = _baseline.Baseline(run.log, abort=run.aborted.is_set)
        left = bl.enforce("pre", captured=None)
        if left:
            who = self.last.id if self.last is not None else "an earlier run"
            journal.info("leftovers before %s (left by %s): %s"
                         % (run.id, who, "; ".join(str(x) for x in left)))
        run.evidence["baseline"] = {"pre": [x.as_dict() for x in left]}
        if mode:
            found = bl.mode
            ok, detail = bl.switch_mode(mode)
            run.log("mode: test needs %s - %s" % (mode, detail))
            run.evidence["mode"] = {"needs": mode, "found": found, "switched": found != mode,
                                    "ok": ok, "detail": detail}
            if not ok:
                raise Failed("the test needs %s mode and the machine could not be brought "
                             "there: %s" % (mode, detail))
        run.baseline_captured = bl.capture()
        return run.baseline_captured

    def _baseline_post(self, run, captured):
        bl = _baseline.Baseline(run.log)
        left = bl.enforce("post", captured=run.baseline_captured or captured)
        run.evidence.setdefault("baseline", {})["post"] = [x.as_dict() for x in left]
        if left:
            journal.info("leftovers after %s: %s" % (run.id, "; ".join(str(x) for x in left)))
        return left

    def _exec_test(self, t, run, campaign):
        ctx = Context(run, self, t)
        fp = t.fingerprint(self.manifest)
        result, message = _campaign.PASS, ""
        captured = None
        try:
            captured = self._baseline_pre(run, mode=t.mode)
            t.fn(ctx)
            if run.aborted.is_set():
                result, message = _campaign.ABORTED, "aborted"
        except Aborted as e:
            result, message = _campaign.ABORTED, str(e) or "aborted"
        except Failed as e:
            result, message = _campaign.FAIL, str(e)
        except _fixture.FixtureError as e:
            # the bench actuator could not perform a step: the test was
            # not judged, and the machine is not the one at fault
            result, message = _campaign.ERROR, "the fixture could not perform a step: %s" % e
        except Exception as e:  # noqa: BLE001 - an erroring test is a failed test
            result, message = _campaign.ERROR, "%s: %s" % (type(e).__name__, e)
            run.log(traceback.format_exc().rstrip())
        self.fixture_release(run)
        try:
            left = self._baseline_post(run, captured)
            # A test hands the machine back as it found it. Anything left
            # behind was recorded here and nothing more, so a check that
            # measured correctly and walked away with the machine in a
            # state nobody chose still passed: that is how the airflow
            # check came to leave the purge fan off, and an operator met
            # it at their first fire instead of the bench meeting it here.
            # The pass is now conditional on the machine being whole. A
            # leftover the baseline put back still fails: the restore is
            # the bench cleaning up after a defect, not the defect's
            # absence.
            if left and result == _campaign.PASS:
                result = _campaign.FAIL
                message = ("the machine was not handed back as found: %s"
                           % "; ".join(str(x) for x in left))
        except Exception as e:  # noqa: BLE001 - never lose the result over the cleanup
            run.log("baseline: post pass errored: %s: %s" % (type(e).__name__, e))
        duration = int(time.time() - run.started)
        run.log("result %s%s" % (result, (": " + message) if message else ""))
        rec = {"t": "result", "campaign": campaign["id"], "test": t.id, "result": result,
               "fingerprint": fp, "manifest_sha": self.manifest.content_sha,
               "image": self.manifest.version, "duration_s": duration, "message": message,
               "evidence": run.evidence, "answers": run.answers, "log": list(run.lines)}
        self.log.append(rec)
        run.finished = {"result": result, "message": message, "duration_s": duration}

    # -- bench tools ---------------------------------------------------------
    def start_bench(self, tool_id, args=None, ack_live=False):
        if self.batch_active():
            return False, "a queue is running"
        if self.bench is None:
            return False, "no bench registry"
        tool = self.bench.get(tool_id)
        if tool is None:
            return False, "unknown tool"
        if not tool.get("ported"):
            return False, "tool not yet ported to the bench page"
        ok, argv, err = self.bench.command(tool, args or {})
        if not ok:
            return False, err
        if tool.get("safety") == "live" and not ack_live:
            return False, "live tool: acknowledge eye protection, fire watch, and exhaust first"
        with self._lock:
            if self.busy():
                return False, "a run is in progress"
            run = Run("bench", tool["id"], tool["title"])
            self.last = self.current
            self.current = run
        run.log("bench %s: %s" % (tool["id"], " ".join(argv)))
        th = threading.Thread(target=self._exec_bench, args=(tool, run, argv, args or {}),
                              daemon=True, name="forgetest-bench")
        th.start()
        return True, "started"

    def _exec_bench(self, tool, run, argv, args):
        rc = None
        message = ""
        captured = None
        try:
            captured = self._baseline_pre(run)
            env = dict(os.environ)
            env.setdefault("PYTHONUNBUFFERED", "1")
            # The tools that also run from a LAN host (gfbench.py) run on
            # the board here: the machine is local, the panel token is at
            # hand, and their data files go under <data>/bench/.
            env.setdefault("GF_HOST", "127.0.0.1")
            env.setdefault("FORGETEST_BENCH_DATA", os.path.join(data_dir(), "bench"))
            if not env.get("GF_TOKEN"):
                tok = hw.Forgectrl().token
                if tok:
                    env["GF_TOKEN"] = tok
            takeover = (Takeover(run.log, "bench:" + tool["id"])
                        if tool.get("safety") in ("takeover", "scope") else None)
            if takeover is not None:
                takeover.__enter__()
            try:
                run.proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                            env=env, cwd=self.bench.tool_dir())
                for raw in iter(run.proc.stdout.readline, b""):
                    run.log(raw.decode("utf-8", "replace").rstrip())
                run.proc.stdout.close()
                rc = run.proc.wait()
            finally:
                if takeover is not None:
                    takeover.__exit__(None, None, None)
            if run.aborted.is_set():
                message = "aborted"
        except Exception as e:  # noqa: BLE001
            message = "%s: %s" % (type(e).__name__, e)
            run.log(message)
        try:
            self._baseline_post(run, captured)
        except Exception as e:  # noqa: BLE001
            run.log("baseline: post pass errored: %s: %s" % (type(e).__name__, e))
        duration = int(time.time() - run.started)
        result = "ABORTED" if run.aborted.is_set() else ("OK" if rc == 0 else "EXIT %s" % rc)
        run.log("bench %s finished: %s" % (tool["id"], result))
        run.finished = {"result": result, "message": message, "duration_s": duration, "rc": rc}
        self.bench.record(tool, args, run)

    # -- control --------------------------------------------------------------
    def answer(self, prompt_id, value):
        r = self.current
        if not r or r.finished:
            return False, "nothing is running"
        if r.answer(prompt_id, value):
            return True, "answered"
        return False, "no such prompt (or the answer is not one of the options)"

    def abort(self):
        r = self.current
        if not r or r.finished:
            return False, "nothing is running"
        r.abort()
        return True, "abort requested"
