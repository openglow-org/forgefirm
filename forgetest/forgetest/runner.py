# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""The runner: executes one acceptance test or one bench tool at a time.

Acceptance tests run in a worker thread with a Context: log lines, an
operator prompt channel (the page shows the question, the answer comes
back through the API), an abort flag, an evidence dict, hardware helpers,
and the takeover wrapper for tests that need forgectrl out of the way.
Results are appended to the log with the fingerprint the test ran under
and the provenance verdict (provenance.py: whether the machine runs its
pins as built); the campaign rules (campaign.py) do the rest.

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
operator's acknowledgment in the request; the runner never unlocks the
laser latch, and locks it wherever it hands the machine back; a takeover
always ends with forgectrl started again, and a marker file, which holds
what the takeover saved, makes a crash mid-takeover recoverable at the
next start.

A run is judged on the machine as the pass before it found it. A leftover
on the laser's safety state found before a run stops that run, and every
leftover found before a run is charged to the test that ran before it
(a `leftover` record, which holds that test's fingerprint until the
operator answers it). A hand-back that cannot be judged, or a result that
cannot be recorded, is never a PASS.

Runner-level events (a queue opening or stopping, a takeover recovered,
the leftovers a baseline pass found) go to the journal: the daemon's
stderr (daemon.log under the data directory), syslog when there is one,
and the log of the run in progress. The page shows the run, never the
journal.
"""
import json
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
from . import provenance as _provenance
from .log import boot_id, now_ts, data_dir

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
# covers into the unattended queue. The probe that decides is network I/O
# (an mDNS lookup and a GET, seconds when a configured box does not
# answer); it is repeated at most this often, off the page's polls, and
# before every run and queue.
FIXTURE_PROBE_S = 30.0


class Aborted(Exception):
    pass


class Failed(Exception):
    pass


class NotRun(Exception):
    """The run did not start: the machine was not one a test can be judged
    on (a leftover on the laser's safety state, or a state the pre pass
    could not capture). Recorded as an ERROR that closes the campaign and
    asks for a rerun only: the test judged nothing."""


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
ARM_LIT_WAIT_S = 1800                # an arm can wait minutes (a coolant settle) before it lights
ARM_PRESS_WAIT_S = 3600              # the controller's own button timeout, at most


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
        self.released = False     # the fixture's release after the run has run
        self.start_seq = None     # the place of the run's "start" record in the log
        self.fingerprint = None   # a test run's fingerprint and campaign, as its start record has them
        self.campaign_id = None
        self.safety_charged = []  # the tests the safety rule charged in the pass under way
        self.safety_pending = []  # safety records the log refused, carried on the run's own result
        self.body_safety = []     # what a takeover's reads before its stop found during the run
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
            # The press is read as an edge from the switches' events, opened
            # before the notice goes up: a press of any length counts, and
            # nobody holds the button for a poll to land on it.
            edges = hw.ButtonEdges()
            self.notice(text + " Then press the button on the machine to start.")
            self.log("READY: waiting for the operator's press on the machine "
                     "(the bench then does every press in this test)")
            try:
                if edges.available:
                    pressed = edges.wait(1, PRESENCE_TIMEOUT_S, self.checkpoint)
                    # The release, so this press is never read as the arm
                    # press or a pause toggle; a short press has queued it.
                    if pressed is not None:
                        edges.wait(0, 10, self.checkpoint)
                else:
                    pressed = self.wait_for(lambda: self.switch("button") is True, PRESENCE_TIMEOUT_S,
                                            poll=0.05)
                    if pressed is not None:
                        self.wait_for(lambda: self.switch("button") is False, 10, poll=0.05)
            finally:
                edges.close()
                self.clear_notice()
            if pressed is None:
                raise Failed("no press on the machine within %d s: %s"
                             % (PRESENCE_TIMEOUT_S, text))
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

    def act(self, channel, state, until=None, timeout=ACT_TIMEOUT_S, text="", fail=True, ms=None):
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
                fixture.act(channel, state, ms)
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
        who is looking at the same prompt. A press the fixture reports
        failed while the button went out was taken all the same, and no
        notice goes up for it. Returns True when the fixture pressed."""
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
            self._clear_when_taken()
            return False
        try:
            fixture.act("button", "press")
        except _fixture.FixtureError as e:
            rec["fixture_error"] = str(e)
            if self._press_taken(rec, e):
                return rec["by"] == "fixture"
            self.log("ARM press: fixture failed (%s) - asking the operator", e)
            self.notice(text)
            self._clear_when_taken()
            return False
        rec["by"] = "fixture"
        self.log("ARM press by the fixture, at the machine's own press prompt")
        return True

    def _press_taken(self, rec, err):
        """After the box reported a failed press of a lit button: True when
        the button is out, so the controller took a press. No notice goes
        up then, because it would ask for a second press, and a press during
        the job pauses it. The press is recorded as the box's only when its
        request went out and the failure came after it (err.sent: the box
        pressed and its reply was lost). After a refusal, a failed lookup
        or a failed connect the box made no press, and the record stays as
        it was: the press was a person's. A button still lit, or
        unreadable, is False: the operator is asked."""
        if hw.button_asks() is not False:
            return False
        if getattr(err, "sent", False):
            rec["by"] = "fixture"
            self.log("ARM press: the fixture reported a failure (%s), and the button is out: the press "
                     "was taken; no notice", err)
        else:
            self.log("ARM press: the fixture made no press (%s), and the button is out: a press was "
                     "taken all the same; no notice", err)
        return True

    def _clear_when_taken(self, lit_timeout=ARM_LIT_WAIT_S):
        """Take the arm notice down the moment the press is taken: the
        controller lights the button white for its arm wait and puts it out
        at the press. Without this the notice stood until the test's next
        step, and an operator who still saw it pressed again, which pauses
        the job. A notice replaced meanwhile is left alone. The watch lives
        only as long as the notice it guards and the run: a notice cleared
        or replaced, a run that finished (passed, failed, or errored before
        the press) or an abort ends it, so no thread is left polling the
        LEDs behind a test."""
        run = self.run
        with run._lock:
            nid = (run.notice or {}).get("id")
        if nid is None:
            return

        def standing():
            with run._lock:
                return run.finished is None and (run.notice or {}).get("id") == nid

        def watch():
            t0 = time.time()
            while hw.button_asks() is not True:
                if not standing() or time.time() - t0 > lit_timeout:
                    return
                if run.aborted.wait(0.05):
                    return
            while hw.button_asks() is not False:
                if not standing() or time.time() - t0 > lit_timeout + ARM_PRESS_WAIT_S:
                    return
                if run.aborted.wait(0.05):
                    return
            with run._lock:
                same = (run.notice or {}).get("id") == nid
                if same:
                    run.notice = None
            if same:
                self.log("ARM: the button went out at the press; the notice is down")

        threading.Thread(target=watch, daemon=True, name="forgetest-arm-notice").start()

    def arm_press(self, text="The button lights white: press it to arm. The machine fires after your press.",
                  lit_timeout=60):
        """The arm cue of a live test. A person's press by default: a
        standing notice until the caller clears it. The fixture presses
        only where the bench opted in (arm_press in its config) and its
        button channel is enabled: a thread waits up to `lit_timeout`
        seconds for the button to light (the job may still be on its way
        to the arm wait; a card that settles the coolant first takes
        minutes) and presses once, recorded as the fixture's; if the
        button never lights, or the press fails with the button still lit,
        the notice goes up for a person. Returns True when the fixture has
        been asked."""
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
            self._clear_when_taken()
            return False
        self.log("ARM: the fixture presses when the button lights (the bench's arm_press opt-in)")

        def press():
            lit = self.wait_for(hw.button_asks, lit_timeout)
            if lit is None:
                self.log("ARM: the button never lit within %d s - asking the operator", lit_timeout)
                self.notice(text)
                self._clear_when_taken()
                return
            try:
                fixture.act("button", "press")
            except _fixture.FixtureError as e:
                rec["fixture_error"] = str(e)
                if self._press_taken(rec, e):
                    rec["took_s"] = round(lit, 2)
                    return
                self.log("ARM press: fixture failed (%s) - asking the operator", e)
                self.notice(text)
                self._clear_when_taken()
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
        # what the takeover's reads before its stop find is the run's own
        return Takeover(self.run.log, self.test.id, run=self.run,
                        on_safety=lambda found: self.runner._safety_found(self.run, "body", found))

    def counters_rezeroed(self):
        """Tell the baseline the kernel position counters were re-zeroed
        at the head's starting position during this run (cloud mode's
        connect clears them): counters at (0,0,0) afterward mean the head
        is back where the run found it."""
        cap = self.run.baseline_captured
        if cap and cap.get("position") is not None:
            cap["position"] = [0, 0, 0]
            cap["rezero_declared"] = True   # the test vouches for the new frame
            # ... and it is the frame now in force, so a takeover later in
            # the run can still judge where the head stands against it
            cap["frame"] = _baseline.counter_frame()
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
    # captured on enter, kept in the marker, and written back on exit, or
    # by recover() after a forgetest that died mid-takeover, before
    # forgectrl starts, so the machine forgectrl starts on is the one the
    # takeover found. forgectrl's liveness probe writes the ones its move
    # needs itself (motor_lock, x_mode, x_decay, the X current, step_freq,
    # streaming), and the controller writes its whole configuration at its
    # start; the rest (ramp_rate, the Y axis) stay as written here.
    PRESERVE = ("cnc/motor_lock", "cnc/step_freq", "cnc/ramp_rate", "cnc/streaming",
                "cnc/x_mode", "cnc/y_mode", "cnc/x_decay", "cnc/y_decay",
                "pic/x_step_current", "pic/y_step_current")

    def __init__(self, log, who, run=None, on_safety=None):
        self.log = log            # callable(str)
        self.who = who
        self.marker = marker_path()
        self.saved = {}
        self.run = run            # the test's run, whose baseline the head is judged against
        self.cloud = False
        self.offline = False      # the cloud client found is the offline service
        self.frame = None         # the counters' frame, read before the controller is stopped
        self.found = None         # (counters, microstep mode) once the controller is gone
        self.on_safety = on_safety  # the runner's safety rule, for what the reads before the stop find

    def wait_settled(self):
        return _baseline.Baseline(self.log).wait_settled()

    def read_before_stop(self, stops=True):
        """The latch and the cooling engine's window, read before the
        takeover's controller stop, which (forgectrl's exit) locks the latch
        and ends the window when forgectrl stops a controller: a clear bit 3
        is locked, and either finding goes to the runner's safety rule
        (on_safety), a window labeled by what comes next (stops: whether the
        refusal makes that stop). Returns (findings, unread): unread says
        what the cooling engine answered when it did not answer with its
        report, which says nothing of a window (a host run, with no
        forgectrl, skips it as the pass does)."""
        found = []
        ilk = hw.sysfs_int("cnc/interlock_circuit")
        if ilk is not None and not (ilk & _baseline.LATCH_BIT):
            found.append(_baseline.Leftover("laser_latch", "unlocked before the takeover (interlock 0x%x)" % ilk,
                                            "locked", self.lock()))
        try:
            st, c = hw.Forgectrl().get("/cool/status")
        except hw.HwError as e:
            st, c = "no answer (%s)" % e, None
        unread = None
        if (st != 200 or not isinstance(c, dict)) and not _baseline.host_run():
            unread = "/cool/status -> %s" % st
            self.log("takeover: the cooling engine did not answer before the takeover (%s): "
                     "its window is not judged" % unread)
        elif isinstance(c, dict) and c.get("armed"):
            found.append(_baseline.Leftover("cool.armed", "%s/armed=True/hold=%s before the takeover"
                                            % (c.get("phase"), c.get("hold")), "unarmed",
                                            "ending it: the takeover's controller stop" if stops
                                            else "left to the hand-back: no controller to stop"))
        if found and self.on_safety is not None:
            try:
                self.on_safety(found)
            except Exception as e:  # noqa: BLE001 - the takeover is refused either way
                self.log("takeover: the safety finding could not be recorded: %s: %s" % (type(e).__name__, e))
        return found, unread

    STOP_IDLE_S = 5.0         # a controlled stop ramps down at ramp_rate, well inside this

    def restore_attrs(self):
        """Relock the latch, acknowledge an underrun, and write the captured
        kernel attributes back: the machine forgectrl's start finds. The lock
        comes first. The underrun is acknowledged here so that forgectrl
        starts on an idle kernel, and the takeover's exit records it as the
        run's leftover: a drill that ends in underrun has left the kernel
        faulted. A program still playing (a drill that raised inside its
        run) gets a controlled stop first, and the attributes go back only
        once the kernel is idle: written under a program, they would unmask
        an axis mid-move. Returns None for an idle kernel, else the state
        found and what became of it: ("underrun", "restored (acknowledged)"),
        ("running", "restored (stopped)") or ("running", "failed: ...")."""
        self.lock()
        state = hw.sysfs_read("cnc/state")
        found = None
        if state == "underrun":
            try:
                hw.sysfs_write("cnc/stop", "1")         # the underrun's acknowledgment
                self.log("takeover: the kernel was left in underrun: acknowledged before forgectrl starts")
            except OSError as e:
                self.log("takeover: WARNING could not acknowledge the underrun: %s" % e)
            found = ("underrun", "restored (acknowledged)")
        elif state == "running":
            try:
                hw.sysfs_write("cnc/stop", "1")         # a controlled stop: no step lost
            except OSError as e:
                self.log("takeover: WARNING could not stop the program: %s" % e)
            deadline = time.time() + self.STOP_IDLE_S
            while hw.sysfs_read("cnc/state") == "running" and time.time() < deadline:
                time.sleep(0.1)
            if hw.sysfs_read("cnc/state") == "running":
                self.log("takeover: a program still plays after the stop: the saved attributes are left "
                         "for forgectrl's probe, which halts it")
                return ("running", "failed: still running after the stop; attributes left")
            self.log("takeover: a program was still playing: stopped before the attributes go back")
            found = ("running", "restored (stopped)")
        for attr, val in self.saved.items():
            try:
                hw.sysfs_write(attr, val)
            except OSError as e:
                self.log("takeover: WARNING could not restore %s=%s: %s" % (attr, val, e))
        return found

    def lock(self):
        """The latch locked; returns what became of it, as a leftover's action."""
        try:
            hw.sysfs_write("cnc/laser_latch", "1")
            return "restored"
        except OSError as e:
            self.log("takeover: WARNING could not relock the latch: %s" % e)
            return "failed: %s" % e

    @staticmethod
    def holders():
        """Every holder of the pulse device: the daemon, the GRBL controller,
        the cloud client (a script, so found by its command line), and any
        other process with the device open (a takeover drill)."""
        found = hw.pidof("forgectrl") + hw.pidof("grblHAL_glowfor") + hw.pgrep_f("gfcloud.py")
        return sorted(set(found + hw.pids_holding(PULSE_DEVICE)))

    def write_marker(self):
        """The marker: who holds the machine, and what recover() writes back
        if this process dies before its exit."""
        with open(self.marker, "w") as f:
            json.dump({"ts": now_ts(), "who": self.who, "saved": self.saved}, f, sort_keys=True)
            f.write("\n")

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
        running = None            # what that read says of a controller; None: no answer
        try:
            st, mode = hw.Forgectrl().get("/mode")
            self.cloud = st == 200 and isinstance(mode, dict) and mode.get("mode") == "cloud"
            if st == 200 and isinstance(mode, dict):
                running = mode.get("controller") == "running"
            self.offline = self.cloud and hw.listens_on(mode.get("pid"), _baseline.OFFLINE_SOCKET)
        except hw.HwError:
            pass
        if self.offline:
            log("takeover: the cloud client is the offline service (pid %s); forgectrl's start "
                "brings it back offline" % mode.get("pid"))
        # The frame is read while the controller runs: its exit takes the
        # anchor with it (forgectrl unlinks it), though not the counters.
        self.frame = _baseline.counter_frame()
        # The stop locks the latch and ends a window when forgectrl stops a
        # controller: what they read now is read first. A finding (the
        # runner's safety rule has recorded it), or a window read the
        # cooling engine did not answer, refuses the takeover after the
        # stop, and the refusal says what the stop answered; nothing else is
        # taken over, and the pass after it starts the controller the stop
        # left in standby. A refusal makes the stop only when the /mode read
        # above found a controller running or went unanswered: with none
        # running there is no session to end, and forgectrl keeps a stop's
        # standby through its setup gate, with nothing after a refusal to
        # start it again once the gate opens.
        refused_stop = running is not False
        found, unread = self.read_before_stop(stops=refused_stop)
        if (found or unread) and not refused_stop:
            stop = "no controller stop (/mode: no controller running)"
        else:
            log("takeover: stopping the controller through forgectrl")
            try:
                st, body = hw.Forgectrl().post("/controller/stop")
                stop = "POST /controller/stop -> %s" % st
            except hw.HwError as e:
                stop = "POST /controller/stop -> no answer (%s)" % e
        log("takeover: %s" % stop)
        if found or unread:
            raise Failed("takeover: %s; %s, and nothing else was taken over"
                         % ("the laser's safety state was off before the takeover: %s"
                            % "; ".join(str(x) for x in found) if found
                            else "the cooling engine's window was not judged (%s)" % unread, stop))
        self.write_marker()
        rc, out = hw.initd("forgectrl", "stop")
        log("takeover: forgectrl stop -> rc %s" % rc)
        deadline = time.time() + 15
        while time.time() < deadline and self.holders():
            time.sleep(0.5)
        left = self.holders()
        if left:
            self.__exit__(None, None, None)
            raise Failed("takeover: processes still alive after stop: %s" % left)
        log("takeover: pulse device free")
        try:
            self.position_at_start()
        except Exception as e:  # noqa: BLE001 - a reading never keeps forgectrl down
            log("takeover: WARNING the head's position could not be judged: %s: %s" % (type(e).__name__, e))
        return self

    # A controller start re-zeroes the step counters where the head then
    # stands, and forgectrl's start after a takeover is one, so the
    # baseline's check after the run cannot see a head left out before it.
    # The takeover judges it at the two moments it can: as the controller
    # goes away (against where the run expects the head) and before it
    # comes back (against where the takeover found it). A miss is recorded
    # for the baseline's post pass, which fails the run; nothing is moved,
    # because a jog sized from these counters is only as good as the frame
    # they are in. Cloud mode is not judged: its counters are the cloud
    # client's, as in the baseline.

    def _leftover(self, item, found, expected, action):
        """A leftover for the baseline's post pass, which fails the run."""
        cap = self.run.baseline_captured if self.run is not None else None
        if cap is not None:
            cap.setdefault("restart_leftovers", []).append(
                {"item": item, "found": found, "expected": expected, "action": action})
        self.log("takeover: %s: %s (expected %s); recorded as a leftover" % (item, found, expected))

    def _record(self, where, found, expected):
        cap = self.run.baseline_captured if self.run is not None else None
        if cap is not None:
            cap.setdefault("restart_positions", []).append(
                {"where": where, "found": found, "expected": expected})
        self.log("takeover: the head is not where it belongs at the %s: counters %s, expected %s; "
                 "recorded as a leftover, nothing moved" % (where, found, expected))

    def position_at_start(self):
        cap = self.run.baseline_captured if self.run is not None else None
        if not cap or self.cloud:
            return
        now = _baseline.read_position()
        self.found = (now, hw.sysfs_read("cnc/x_mode"))
        was = cap.get("position")
        if now is None or was is None:
            return
        if self.frame is None or cap.get("frame") != self.frame:
            self.log("takeover: the counters are not in the frame the run began in (a controller "
                     "start or a home the test did not declare), so the head is not judged here")
            return
        if now != was and not _baseline.position_quantized(was, now, _baseline.counter_steps_per_mm()):
            self._record("takeover start", now, was)

    def position_at_end(self):
        if self.found is None:
            return
        was, mode = self.found
        now = _baseline.read_position()
        if was is None or now is None:
            return
        if hw.sysfs_read("cnc/x_mode") != mode:
            self.log("takeover: the microstep mode changed under the takeover, so the counters "
                     "before and after it are not one scale; the head is not judged here")
            return
        if now != was and not _baseline.position_quantized(was, now, _baseline.counter_steps_per_mm()):
            self._record("takeover end", now, was)

    # In cloud mode forgectrl's start at the end starts a cloud client, and
    # what that client is comes only from the one-start markers gfcloud
    # reads and takes down as it starts. Started bare, it is the online
    # client with the service's connect-time hunt, and the moves the
    # service sends after the hunt are still running as the run ends and
    # the next one begins, whatever the takeover found. The start is made
    # under the no-hunt marker, as every client start the runner makes is,
    # and under the offline marker as well when the takeover found the
    # offline service; that client is then proven to be the offline
    # service, and a miss is recorded for the baseline's post pass, which
    # fails the run.
    OFFLINE_BACK_S = 60       # the start to the offline service's listener

    def client_markers(self):
        """(path, name) of the markers forgectrl's start is made under."""
        if not self.cloud:
            return []
        marks = [(_baseline.NOHUNT_MARKER, "no-hunt")]
        if self.offline:
            marks.append((_baseline.OFFLINE_MARKER, "offline"))
        return marks

    def markers_on(self):
        for path, name in self.client_markers():
            try:
                with open(path, "w") as f:
                    f.write("forgetest takeover %s\n" % self.who)
            except OSError as e:
                self.log("takeover: WARNING the %s marker could not be written: %s" % (name, e))

    def wait_offline(self):
        """The cloud client's pid once it listens on the offline socket, or
        None after OFFLINE_BACK_S."""
        deadline = time.time() + self.OFFLINE_BACK_S
        while True:
            try:
                st, m = hw.Forgectrl().get("/mode")
            except hw.HwError:
                st, m = None, None
            pid = m.get("pid") if st == 200 and isinstance(m, dict) else None
            if hw.listens_on(pid, _baseline.OFFLINE_SOCKET):
                return pid
            if time.time() >= deadline:
                return None
            time.sleep(0.5)

    def client_back(self, mode):
        """After forgectrl's start: a marker no client read is taken down
        (no client started - the gate, standby, a fault - and a later
        start must not come up under it), and the offline service found is
        judged against the client that came up."""
        if not self.cloud:
            return
        mode = mode if isinstance(mode, dict) else {}
        running = mode.get("mode") == "cloud" and mode.get("controller") == "running"
        pid = self.wait_offline() if self.offline and running else None
        for path, name in self.client_markers():
            if (not running or name == "offline") and os.path.exists(path):
                try:
                    os.remove(path)
                    self.log("takeover: the %s marker was never read - taken down" % name)
                except OSError:
                    pass
        if not self.offline:
            return
        if pid:
            self.log("takeover: the cloud client is back as the offline service (pid %s)" % pid)
            return
        found = ("a client that is not the offline service (pid %s)" % mode.get("pid") if running
                 else "no cloud client running (mode %s, controller %s)"
                 % (mode.get("mode"), mode.get("controller")))
        cap = self.run.baseline_captured if self.run is not None else None
        if cap is not None:
            cap.setdefault("restart_clients", []).append(
                {"where": "takeover end", "found": found, "expected": "the offline service"})
        self.log("takeover: the offline service did not come back: %s; recorded as a leftover" % found)

    def __exit__(self, exc_type, exc, tb):
        try:
            self.position_at_end()
        except Exception as e:  # noqa: BLE001 - a reading never keeps forgectrl down
            self.log("takeover: WARNING the head's position could not be judged: %s: %s"
                     % (type(e).__name__, e))
        found = self.restore_attrs()
        if found:
            self._leftover("kernel state at the takeover end", found[0], "idle", found[1])
        self.markers_on()
        rc, out = hw.initd("forgectrl", "start")
        self.log("takeover: forgectrl start -> rc %s" % rc)
        try:
            os.remove(self.marker)
        except OSError:
            pass
        # leave the machine settled for whatever runs next: the probe
        # move done, the controller back (or the ladder's verdict logged)
        mode = self.wait_settled()
        # The takeover hands back a running forgectrl: a start that failed,
        # or a daemon that never answered after it, is the run's leftover,
        # whatever forgectrl does later.
        if rc != 0 or mode is None:
            self._leftover("forgectrl at the takeover end",
                           "start rc %s, %s" % (rc, "answering" if mode is not None else "not answering"),
                           "started and answering", "failed")
        try:
            self.client_back(mode)
        except Exception as e:  # noqa: BLE001 - the judge never breaks the exit path
            self.log("takeover: WARNING the cloud client could not be judged: %s: %s"
                     % (type(e).__name__, e))
        return False


def marker_path():
    return os.environ.get("FORGETEST_MARKER") or "/run/forgetest.active"


PULSE_DEVICE = "/dev/glowforge"


def forgectrl_answers(timeout=2.0):
    """forgectrl answers /mode (a short ask: a down daemon refuses at once)."""
    try:
        st, body = hw.Forgectrl(timeout=timeout).get("/mode")
    except hw.HwError:
        return False
    return st == 200 and isinstance(body, dict)


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
        self._fixture_probed = None   # time.monotonic() of the last probe's end
        self._fixture_lock = threading.Lock()
        self._fixture_said = None
        self.boot_ref = None
        # safety records nothing in the log took (the leftover, the run's
        # result and its bare record all refused): tried again before each
        # start and export, which wait while any stands (_retry_refused)
        self.refused_charges = []
        self.provenance = _provenance.Provenance(manifest)
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
    def _fixture_fresh(self):
        return self._fixture_probed is not None and time.monotonic() - self._fixture_probed < FIXTURE_PROBE_S

    def probe_fixture(self, force=False):
        """The fixture, up and answering, or None; re-probed at most every
        FIXTURE_PROBE_S unless forced (before a run). A probe that finds
        it gone, or a config that appeared, changes the queues' routing
        from then on. One probe at a time, and the probe counts from its
        completion: a caller that arrives while another's probe is in
        flight (a queue start right after the daemon came up) waits for
        its answer instead of reading the stale fixture, which would route
        the fixture's tests to nobody. A page poll never calls this on its
        own thread (cached_fixture)."""
        with self._fixture_lock:
            if not force and self._fixture_fresh():
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
            self._fixture_probed = time.monotonic()
            return fx

    def cached_fixture(self):
        """The fixture as the last probe found it, never a probe on the
        caller's thread: the state a page polls, and a start computes under
        the runner's lock, must not wait out a box that does not answer.
        An answer older than FIXTURE_PROBE_S starts one probe in the
        background, and a later poll reads what it found."""
        if not self._fixture_fresh() and not self._fixture_lock.locked():
            threading.Thread(target=self.probe_fixture, daemon=True, name="forgetest-fixture").start()
        return self.fixture

    def fixture_channels(self):
        """The channels the fixture covers as the last probe found it (the
        button only with its enable jumper in)."""
        fx = self.cached_fixture()
        if fx is None:
            return ()
        return tuple(c for c in fx.channels if fx.covers(c))

    def fixture_release(self, run, left=None):
        """After a run, once the post pass has locked the latch: any channel
        the fixture still holds energized is released and recorded. A
        release that fails, for any reason, is said, recorded, and a
        leftover in `left`: a channel may still be held. Never raises, so
        the hand-back always goes on."""
        run.released = True
        fx = self.fixture
        if fx is None:
            return
        try:
            held = fx.energized(fx.status())
            if held:
                fx.release()
                run.log("fixture: released %s left energized" % ", ".join(held))
            run.evidence.setdefault("fixture", {})["released"] = held
        except Exception as e:  # noqa: BLE001 - the hand-back goes on whatever the box answered
            why = "%s: %s" % (type(e).__name__, e)
            run.log("fixture: release check failed: %s" % why)
            run.evidence.setdefault("fixture", {})["release_error"] = why
            if left is not None:
                left.append(_baseline.Leftover("fixture", "release check failed (%s)" % why,
                                               "nothing held", "failed: a channel may still be held"))

    def fixture_release_locked(self, run):
        """The release on a path where the post pass broke before its hook:
        the latch is locked first here, since that pass may not have locked
        it (a lock is never refused and fires nothing)."""
        try:
            hw.sysfs_write("cnc/laser_latch", "1")
        except OSError as e:
            run.log("baseline: the lock before the fixture's release failed: %s" % e)
        self.fixture_release(run)

    # -- startup recovery ------------------------------------------------
    def recover(self):
        """A takeover this process (or an earlier one) never ended: the
        latch is locked and, on a machine nothing else drives, the machine
        is handed back as the takeover's exit would have (an underrun
        acknowledged, the saved attributes written back from the marker),
        before forgectrl starts on it."""
        m = marker_path()
        if not os.path.exists(m):
            return
        try:
            with open(m) as f:
                text = f.read()
        except OSError:
            text = ""
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if not isinstance(data, dict):
            data = {"who": text.strip() or "?"}
        saved = data.get("saved") if isinstance(data.get("saved"), dict) else {}
        t = Takeover(self._note, data.get("who") or "?")
        t.saved = {k: v for k, v in saved.items() if k in Takeover.PRESERVE and isinstance(v, str)}
        # The attributes go back only on a machine nothing else drives: a
        # drill that outlived the forgetest that ran it may still be playing
        # (a motor_lock written under it would unmask an axis mid-run), and a
        # forgectrl already up owns them. Then the latch is locked and the
        # rest left.
        busy = []
        holders = Takeover.holders()
        if holders:
            busy.append("the pulse device held by pid %s" % ", ".join(str(p) for p in holders))
        state = hw.sysfs_read("cnc/state")
        if state == "running":
            busy.append("the kernel running")
        if forgectrl_answers():
            busy.append("forgectrl up")
        if busy:
            t.lock()
            self._note("recovered a takeover left by '%s' with %s: the latch is locked, the saved attributes "
                       "(%s) are left as they are" % (t.who, " and ".join(busy),
                                                      ", ".join("%s=%s" % kv for kv in t.saved.items()) or "none"))
        else:
            self._note("recovered a takeover left by '%s': handing the machine back (%s) and starting forgectrl"
                       % (t.who, ", ".join("%s=%s" % kv for kv in t.saved.items()) or "no attributes saved"))
            t.restore_attrs()
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

    def running_start(self):
        """The place of the test run in progress's start record, or None."""
        r = self.current
        return r.start_seq if r and r.kind == "test" and not r.finished else None

    def state(self):
        # a run the log saw start and never saw end is the ERROR it is
        records = _campaign.as_judged(self.log.read(), running=self.running_start())
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
        # what the machine runs as the campaign opens: the component
        # programs' hashes and whether they are the build's
        rec = self.log.append({"t": "campaign", "id": cid, "manifest_sha": self.manifest.content_sha,
                               "catalog_hash": self.catalog_hash, "image": self.manifest.version,
                               "provenance": self.provenance.check()})
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

    def dispose(self, test_id, reason):
        """The operator's answer to the FAIL, ERROR or leftover that blocks a
        test's current fingerprint (campaign.py, reason "disposition"):
        recorded with its reason, it lets a PASS on that fingerprint count
        again, except one older than a leftover on the laser's safety state,
        which still needs a PASS newer than it. The artifact carries the
        disposition beside the record it answers."""
        reason = (reason or "").strip()
        if not reason:
            return False, "a reason is required"
        t = _catalog.get(test_id, self.registry)
        if t is None:
            return False, "unknown test"
        with self._lock:
            if self.busy():
                return False, "a run is in progress"
            state, _ = self.state()
            ts = state["tests"][t.id]
            if ts["reason"] != "disposition":
                return False, "nothing to dispose: no FAIL, ERROR or leftover on this fingerprint waits for one"
            self.log.append({"t": "disposition", "test": t.id, "fingerprint": ts["fingerprint"],
                             "reason": reason})
            unsafe = (ts.get("leftover") or {}).get("safety")
        return True, ("disposition recorded; after a leftover on the laser's safety state the test needs a new PASS"
                      if unsafe else "disposition recorded; a PASS on this fingerprint counts again")

    def export(self):
        """(True, the signed artifact), or (False, why). A run in progress
        would change what the artifact claims, so an export waits for it."""
        with self._lock:
            if self.busy():
                return False, "a run is in progress"
            held = self._retry_refused()
            if held:
                return False, held
            # A run the log saw start and never saw end (the result and its
            # bare record both refused, or forgetest gone mid-run) refuses
            # every export until a campaign reset after it, across restarts.
            raw = self.log.read()
            pos = _campaign.positions(raw)
            reset_at = max([pos[id(r)] for r in raw if r.get("t") == "reset"] or [0])
            lost = [r for r in _campaign.lost_runs(raw) if pos[id(r)] > reset_at]
            if lost:
                return False, ("a run of %s started and left no result in the log: reset the campaign before "
                               "an export" % ", ".join(sorted(set(r.get("test") for r in lost))))
            state, records = self.state()
        art = _artifact.build(state, self.tests(), self.manifest, records, self.catalog_hash)
        art = _artifact.sign(art, _artifact.bench_key())
        self.log.append({"t": "export", "artifact_sha256": art["sha256"], "authorized": art["authorized"],
                         "campaign": (state["campaign"] or {}).get("id")})
        return True, art

    def _retry_refused(self):
        """The safety records nothing in the log took, tried again; None when
        none stands, else why a start or an export waits. They live in this
        process only: a forgetest restart loses them, as a process death loses
        a finding not yet written."""
        for rec in list(self.refused_charges):
            try:
                self.log.append(rec)
                self.refused_charges.remove(rec)
            except Exception:  # noqa: BLE001 - it stands, and so does the refusal
                pass
        if not self.refused_charges:
            return None
        return ("a safety record the log refused is still held (charged to %s): free the log's disk, then try again"
                % ", ".join(sorted(set(str(r.get("test")) for r in self.refused_charges))))

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
            held = self._retry_refused()
            if held:
                return False, held, None
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
            # The log learns of the run before its first pass, so a run
            # that never ends (forgetest gone mid-run) is still in it
            # (campaign.with_lost_runs); a run it cannot learn of does not start.
            try:
                start = self.log.append({"t": "start", "campaign": campaign["id"], "test": t.id,
                                         "fingerprint": t.fingerprint(self.manifest)})
            except Exception as e:  # noqa: BLE001 - whatever stopped the log stops the run
                return False, "the run's start could not be recorded: %s: %s" % (type(e).__name__, e), None
            run = Run("test", t.id, t.title)
            run.start_seq = start["seq"]
            run.fingerprint, run.campaign_id = start["fingerprint"], campaign["id"]
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
        # The queue's routing on an answer no older than FIXTURE_PROBE_S,
        # probed here, outside the runner's lock, when the last is older.
        self.probe_fixture()
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
        run.safety_charged = []
        bl = _baseline.Baseline(run.log, abort=run.aborted.is_set,
                                on_safety=lambda found: self._safety_found(run, "pre", found))
        left = bl.enforce("pre", captured=None)
        run.evidence["baseline"] = {"pre": [x.as_dict() for x in left]}
        # The safety findings are charged already, as they were read (the
        # safety rule). What the pass could not judge (forgectrl silent, the
        # kernel or a readback unreadable) stops the run below and is charged
        # to nobody: it is no state the run before left.
        others = [x for x in left if x.judged() and x.item not in _campaign.SAFETY_ITEMS]
        self._retry_safety(run)
        if run.aborted.is_set():
            # An Abort cuts the pass's waits short, so what they settle is
            # not judged: nothing more is charged, and the run ends.
            run.evidence["baseline"]["charged"] = (run.safety_charged or [None])[0]
            raise Aborted("aborted during the pass before the run")
        if left:
            charged = self._charge_previous(run, others) if others else None
            charged = (run.safety_charged or [charged])[0]
            who = ("%s charged with what it left%s" % (charged, " (on this run's own result: the log refused "
                                                                "the record)" if run.safety_pending else "")
                   if charged
                   else "charged to nobody: the pass could not judge it" if not any(x.judged() for x in left)
                   else "charged to nobody: no run of this boot to charge with the laser's safety state, "
                        "and no PASS of the open campaign just before it")
            journal.info("leftovers before %s (%s): %s" % (run.id, who, "; ".join(str(x) for x in left)))
            run.evidence["baseline"]["charged"] = charged
            # The laser's safety state off before the run: the pass has just
            # locked it again, and a test that ran now would read the lock
            # it put there, not the state the machine was found in.
            safety = [x for x in left if x.item in _campaign.SAFETY_ITEMS]
            if safety:
                raise NotRun("the machine was found with its laser safety state changed before the run (%s): "
                             "%s; the latch is locked again and the run did not start"
                             % (who, "; ".join(str(x) for x in safety)))
            blind = [x for x in left if x.item in ("forgectrl", "kernel sysfs")]
            if blind:
                raise NotRun("the machine's state could not be captured before the run: %s"
                             % "; ".join(str(x) for x in blind))
        if mode:
            found = bl.mode
            ok, detail = bl.switch_mode(mode)
            run.log("mode: test needs %s - %s" % (mode, detail))
            run.evidence["mode"] = {"needs": mode, "found": found, "switched": found != mode,
                                    "ok": ok, "detail": detail}
            # the latch read before the switch, after the pass: the safety
            # rule has charged it, and the run does not start
            unlocked = [x for x in bl.switch_found if x.item in _campaign.SAFETY_ITEMS]
            if unlocked:
                self._retry_safety(run)
                run.evidence["baseline"]["switch"] = [x.as_dict() for x in unlocked]
                raise NotRun("the machine was found with its laser safety state changed before the mode switch: "
                             "%s; the latch is locked again and the run did not start"
                             % "; ".join(str(x) for x in unlocked))
            if not ok:
                raise Failed("the test needs %s mode and the machine could not be brought "
                             "there: %s" % (mode, detail))
        run.baseline_captured = bl.capture()
        if bl.missed and not _baseline.host_run():
            raise NotRun("the machine's state could not be captured before the run: forgectrl did not "
                         "answer %s" % ", ".join(bl.missed))
        return run.baseline_captured

    def _safety_found(self, run, phase, found):
        """The safety rule. A leftover on the laser's safety state (a
        campaign.SAFETY_ITEMS member, judged) goes into the log the moment
        the pass reads it, before the pass waits on anything after that read,
        as a `leftover` record marked safety that holds a fingerprint until a
        PASS newer than it (and a disposition). Found in a test's own pass
        after its run, or by the reads before a takeover in its body
        (phase "body"), it is that run's: on its fingerprint, and the run
        fails (_judge_and_record). Found before a run, it is charged to the
        newest run of this boot that finished, a result in the log whatever
        its campaign and whatever its result, looking back past runs that
        never ended and bench tools; a bench tool's own passes and its
        takeover's reads, which hold no fingerprint, charge that run the same
        way. The charge follows time, not cause: a client that arms during a
        later test's body is charged to that later test. A record the log
        refuses is tried again at the end of the pass before a run, then
        carried on the run's own result (campaign.with_carried_charges); one
        nothing took stays on the runner (refused_charges), which tries it
        again before each start and export and refuses both while it stands."""
        items = "; ".join(str(x) for x in found)
        when = {"post": "after", "body": "during"}.get(phase, "before")
        if phase == "body":
            run.body_safety.extend(found)       # a test's own: the run fails (_judge_and_record)
        if phase in ("post", "body") and run.kind == "test":
            target = {"campaign": run.campaign_id, "test": run.id, "fingerprint": run.fingerprint}
        else:
            target = self._safety_target(run)
            if target is None:
                journal.info("the laser's safety state off %s %s, with no run of this boot to charge: %s"
                             % (when, run.id, items))
                return
        message = "the laser's safety state found off %s %s %s: %s" % (when, run.kind, run.id, items)
        rec = {"t": "leftover", "campaign": target.get("campaign"), "test": target.get("test"),
               "fingerprint": target.get("fingerprint"), "leftovers": [x.as_dict() for x in found],
               "needs_disposition": True, "safety": True, "message": message,
               "found_" + when: "%s %s" % (run.kind, run.id)}
        try:
            self.log.append(rec)
        except Exception as e:  # noqa: BLE001 - the charge stands on the run's own result instead
            run.safety_pending.append(rec)
            run.log("baseline: the log refused the safety record (%s: %s); the run's own result carries it"
                    % (type(e).__name__, e))
        run.safety_charged.append(target.get("test"))
        journal.info("%s (charged to %s)" % (message, target.get("test")))

    def _retry_safety(self, run):
        """The safety records the log refused, tried once more."""
        for rec in list(run.safety_pending):
            try:
                self.log.append(rec)
                run.safety_pending.remove(rec)
            except Exception:  # noqa: BLE001 - carried on the result instead
                pass

    def _safety_target(self, run):
        """The newest result in the log before this run's own start that was
        recorded in this boot, or None."""
        records = self.log.read()
        pos = _campaign.positions(records)
        mine = run.start_seq if run.start_seq is not None else float("inf")
        return next((r for r in reversed(records) if r.get("t") == "result" and pos[id(r)] < mine
                     and r.get("boot") == boot_id()), None)

    def _charge_previous(self, run, left):
        """A leftover a pre pass finds that is not on the laser's safety state
        (those follow the safety rule, _safety_found) is charged to the test
        that ran last: a `leftover` record for its fingerprint, which holds it
        like a FAIL until the operator records a disposition with a reason
        (the operator may have touched the machine between the runs). The test
        is read from the log, not from memory, which a restart of forgetest
        empties: the newest run start or result before this run's own start,
        when it is a PASS of the open campaign recorded in this boot. Nothing
        is charged when that is a start: a bench tool's (its own post pass
        judged what it left) or a test run's that never ended (a result the
        log could not take, a forgetest gone mid-run). Nor across a boot.
        Returns the test charged, or None."""
        records = self.log.read()
        pos = _campaign.positions(records)
        mine = run.start_seq if run.start_seq is not None else float("inf")
        prev = next((r for r in reversed(records)
                     if r.get("t") in ("result", "start") and pos[id(r)] < mine), None)
        if prev is None or prev.get("t") != "result" or prev.get("result") != _campaign.PASS \
                or prev.get("boot") != boot_id():
            return None
        campaign = _campaign.open_campaign(_campaign.as_judged(records, running=run.start_seq),
                                           self.manifest.content_sha, self.catalog_hash)[0]
        if campaign is None or prev.get("campaign") != campaign.get("id"):
            return None
        found = "; ".join(str(x) for x in left)
        self.log.append({"t": "leftover", "campaign": prev.get("campaign"), "test": prev.get("test"),
                         "fingerprint": prev.get("fingerprint"), "found_before": "%s %s" % (run.kind, run.id),
                         "leftovers": [x.as_dict() for x in left], "needs_disposition": True,
                         "message": "left behind and found before %s %s: %s" % (run.kind, run.id, found)})
        return prev.get("test")

    def _baseline_post(self, run, captured):
        # The fixture lets go of what it holds only after the laser side has
        # locked the latch: a loop it held open closes the chain. A safety
        # finding is the run's own, recorded as it is read.
        run.safety_charged = []
        bl = _baseline.Baseline(run.log, on_safety=lambda found: self._safety_found(run, "post", found))
        left = bl.enforce("post", captured=run.baseline_captured or captured,
                          after_laser=lambda found: self.fixture_release(run, found))
        run.evidence.setdefault("baseline", {})["post"] = [x.as_dict() for x in left]
        if left:
            journal.info("leftovers after %s: %s" % (run.id, "; ".join(str(x) for x in left)))
        return left

    def _exec_test(self, t, run, campaign):
        """Run one test and record its result. The run is finished on every
        path: a result the log could not take is said on the run as an
        ERROR, never left running, which would refuse every start until the
        daemon restarted."""
        try:
            self._judge_and_record(t, run, campaign)
        except Exception as e:  # noqa: BLE001 - the run ends whatever broke
            message = "the result was not recorded: %s: %s" % (type(e).__name__, e)
            run.log(traceback.format_exc().rstrip())
            run.log("result %s: %s" % (_campaign.ERROR, message))
            journal.info("%s: %s" % (run.id, message))
            if not run.released:
                self.fixture_release_locked(run)
            # The log still learns of the run: a bare ERROR that closes the
            # campaign and holds the fingerprint, so an earlier PASS of the
            # test does not stand for a run whose result was lost. A log
            # that takes not even that keeps the run's start with no result
            # after it, which is the same ERROR (campaign.with_lost_runs)
            # and refuses every export until a reset.
            try:
                bare = {"t": "result", "campaign": campaign["id"], "test": t.id,
                        "result": _campaign.ERROR, "fingerprint": run.fingerprint, "message": message,
                        "start": run.start_seq, "needs_disposition": True, "not_recorded": True}
                if run.safety_pending:
                    bare["charges"] = list(run.safety_pending)
                self.log.append(bare)
            except Exception as again:  # noqa: BLE001
                journal.info("%s: not even its bare record went in (%s): its start stands as an ERROR "
                             "and every export is refused until the campaign is reset" % (run.id, again))
                self.refused_charges.extend(run.safety_pending)
            run.finished = {"result": _campaign.ERROR, "message": message,
                            "duration_s": int(time.time() - run.started)}

    def _judge_and_record(self, t, run, campaign):
        ctx = Context(run, self, t)
        fp = t.fingerprint(self.manifest)
        before = self.provenance.check()
        result, message = _campaign.PASS, ""
        fixture_error = False
        not_run = False
        captured = None
        try:
            captured = self._baseline_pre(run, mode=t.mode)
            t.fn(ctx)
            if run.aborted.is_set():
                result, message = _campaign.ABORTED, "aborted"
        except NotRun as e:
            result, message = _campaign.ERROR, str(e)
            not_run = True
        except Aborted as e:
            result, message = _campaign.ABORTED, str(e) or "aborted"
        except Failed as e:
            result, message = _campaign.FAIL, str(e)
        except _fixture.FixtureError as e:
            # the bench actuator could not perform a step: the test was
            # not judged, and the machine is not the one at fault
            result, message = _campaign.ERROR, "the fixture could not perform a step: %s" % e
            fixture_error = True
        except Exception as e:  # noqa: BLE001 - an erroring test is a failed test
            result, message = _campaign.ERROR, "%s: %s" % (type(e).__name__, e)
            run.log(traceback.format_exc().rstrip())
        left = []
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
        except Exception as e:  # noqa: BLE001 - the result is recorded whatever the hand-back did
            run.log("baseline: post pass errored: %s: %s" % (type(e).__name__, e))
            run.log(traceback.format_exc().rstrip())
            # A hand-back that did not finish judged nothing after the point
            # it broke: the run is not a PASS.
            if result == _campaign.PASS:
                result, message = _campaign.ERROR, ("the hand-back could not be judged: %s: %s"
                                                    % (type(e).__name__, e))
            if not run.released:
                self.fixture_release_locked(run)
        # The safety rule: the laser's safety state found off after the run
        # fails it, whatever the run was (an Abort included: the pass reads
        # the latch before any wait an Abort could cut short).
        if run.safety_charged or run.body_safety or any(x.judged() and x.item in _campaign.SAFETY_ITEMS
                                                        for x in left):
            if result != _campaign.FAIL:
                message = "the laser's safety state was found off after the run%s" % (
                    " (the run was %s: %s)" % (result, message) if message else "")
                result = _campaign.FAIL
            fixture_error = not_run = False
        duration = int(time.time() - run.started)
        run.log("result %s%s" % (result, (": " + message) if message else ""))
        # whether the run was earned on the pins as built, judged before it
        # and after it: a result that was not is never inherited
        rec = {"t": "result", "campaign": campaign["id"], "test": t.id, "result": result,
               "start": run.start_seq, "fingerprint": fp, "manifest_sha": self.manifest.content_sha,
               "image": self.manifest.version, "duration_s": duration, "message": message,
               "provenance": _provenance.merge(before, self.provenance.check()),
               "evidence": run.evidence, "answers": run.answers, "log": list(run.lines)}
        if fixture_error and result == _campaign.ERROR:
            rec["fixture_error"] = True
        elif not_run and result == _campaign.ERROR:
            rec["not_run"] = True
        elif result in _campaign.CLOSING:
            # A FAIL, or an ERROR the test raised, blocks this fingerprint
            # until the operator records a disposition (campaign.py): a
            # rerun that passes is not an answer to it. An ERROR the bench
            # fixture raised, or one before the test ran, judged nothing,
            # and asks for a rerun only.
            rec["needs_disposition"] = True
        if run.safety_pending:
            # safety records the log refused: they stand on this result
            # (campaign.with_carried_charges)
            rec["charges"] = list(run.safety_pending)
        self.log.append(rec)            # a log that cannot take it ends the run in _exec_test
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
            held = self._retry_refused()
            if held:
                return False, held
            # A bench tool's start is in the log too: a leftover found after
            # it is not charged to the test before it, whatever restarts
            # come between (_charge_previous).
            try:
                start = self.log.append({"t": "start", "bench": tool["id"]})
            except Exception as e:  # noqa: BLE001 - whatever stopped the log stops the run
                return False, "the run's start could not be recorded: %s: %s" % (type(e).__name__, e)
            run = Run("bench", tool["id"], tool["title"])
            run.start_seq = start["seq"]
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
            # a finding of the takeover's reads before its stop is charged as
            # in the tool's pass before it, and the tool does not start
            takeover = (Takeover(run.log, "bench:" + tool["id"],
                                 on_safety=lambda found: self._safety_found(run, "body", found))
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
        # a bench tool has no result to carry a refused safety record: the
        # runner keeps it (_retry_refused)
        self._retry_safety(run)
        self.refused_charges.extend(run.safety_pending)
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
