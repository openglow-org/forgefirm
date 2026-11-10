# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""The hardware button latch across a cloud print.

The safing chain carries a latch of its own behind the button: the
kernel's laser lock sets it, and only a press taken with the lock released
clears it (docs.forgefirm.org, technical/machine/safing-chain). While it is
set nothing fires, whatever the lock says. A cloud print can therefore fire
only if the press that arms it is taken after the unlock. The kernel does
not model the latch, so nothing but the machine can show it: this test
reads it (cnc/button_latch: 1 set, 0 cleared) through a dark print.

Its own module: a suite module's text is in every one of its tests'
fingerprints. The cloud suite's helpers are reached through the module, so
whatever stands in for one of them stands in here too."""

import time

from ..catalog import test
from .. import hw
from . import cloud

# The latch is the kernel's lock and the board's own circuit: the module
# that writes the lock and reads the latch back is part of what this proves.
_COVERS = cloud._MACHINE_RUN + [("kernel-module-glowforge", "src/cnc*")]

# How often the latch is read while the kernel plays the job, and how many
# readings a 30 s job has to give.
SAMPLE_S = 0.25
SAMPLES_MIN = 20


def button_latch():
    return hw.sysfs_int("cnc/button_latch")


def kernel_running():
    return (hw.sysfs_read("cnc/state") or "").startswith("running")


@test("cloud.button-latch", title="A cloud print runs with the hardware button latch cleared, on the "
      "one press that arms it",
      subsystem="cloud", kind="live", est_min=5,
      covers=_COVERS,
      requires=["cloud.lid-interlock-abort"], actions=["button"],
      steps=[cloud.OFFLINE_STEP,
             "Bed clear (the job is dark: a square the laser is never commanded on). Press the "
             "button once when it lights white, and not again: a second press would pause the "
             "print."],
      description="The hardware's own button latch is set by the kernel's laser lock and cleared "
                  "only by a press taken with the lock released; while it is set nothing can fire. "
                  "The cloud client waits out the cooling verdict with the lock held, releases the "
                  "lock, and only then lights the button and takes the press. This reads the latch "
                  "through a dark print under the offline service: set, with the lock, before the "
                  "print; the lock released and the latch still set when the button is asked for; "
                  "cleared in every sample taken while the kernel plays the job, on one press and no "
                  "other; and set again, with the lock, once the job has ended. The job commands no "
                  "laser, so nothing fires: the latch is the chain's own state, and it reads the "
                  "same with the beam never asked for.")
def button_latch_print(ctx):
    ev = ctx.evidence
    offset = cloud.enter_offline(ctx)
    job = cloud.offline_job(ctx, "latch.puls", seconds=30)
    off = cloud.Offline().__enter__()
    try:
        button_latch_body(ctx, ev, off, job, offset)
    finally:
        off.__exit__(None, None, None)
        cloud.offline_cleanup(ctx)


def button_latch_body(ctx, ev, off, job, offset):
    ev["before"] = {"button_latch": button_latch(), "lock": cloud.latch_locked()}
    ctx.check(ev["before"]["button_latch"] is not None, "cnc/button_latch cannot be read")
    ctx.check(ev["before"]["lock"] and ev["before"]["button_latch"] == 1,
              "before the print the lock is %s and the button latch %s; both are set at idle",
              ev["before"]["lock"], ev["before"]["button_latch"])
    off.print_ready(9011, job)
    cloud.wait_button_wait(ctx, offset, 120)
    # The button is asked for: the lock is released already, and the latch
    # it set still stands until the press.
    ev["at_button"] = {"button_latch": button_latch(), "lock": cloud.latch_locked()}
    ctx.check(ev["at_button"]["lock"] is False,
              "the lock is still held while the button is asked for: a press now would not count")
    ctx.check(ev["at_button"]["button_latch"] == 1,
              "the button latch reads %s before any press", ev["at_button"]["button_latch"])
    ctx.act("button", "press", text="The button is lit: press it once. The print runs dark and "
            "finishes. Do not press again.",
            until=lambda: cloud.wait_print_running(ctx, offset, 0.1) is not None,
            timeout=cloud.PRESS_TIMEOUT_S)
    run = cloud.wait_print_running(ctx, offset, 60)
    ctx.check(run is not None, "the print did not start after the press")
    # While the kernel plays the job: the latch cleared and the lock
    # released in every sample, and nobody at the button.
    samples = []
    t0 = time.time()
    while time.time() - t0 < 120:
        ctx.checkpoint()
        if kernel_running():
            samples.append((round(time.time() - t0, 2), button_latch(), cloud.latch_locked()))
        elif samples:
            break
        time.sleep(SAMPLE_S)
    ev["samples_in_run"] = len(samples)
    ev["latch_set_in_run"] = [s for s in samples if s[1] != 0][:8]
    ev["lock_held_in_run"] = [s for s in samples if s[2]][:8]
    ctx.check(len(samples) >= SAMPLES_MIN, "only %d samples were taken while the kernel ran", len(samples))
    ctx.check(not ev["latch_set_in_run"],
              "the hardware button latch read set while the kernel ran (first at %s): the print "
              "ran with fire blocked by the chain", ev["latch_set_in_run"][:1])
    ctx.check(not ev["lock_held_in_run"], "the lock was held while the kernel ran (first at %s)",
              ev["lock_held_in_run"][:1])
    fin = cloud.wait_action_finished(ctx, offset, "print", 180)
    ev["print finished"] = cloud.message(fin)
    ctx.check(fin and cloud.COMPLETED in fin, "the print did not complete: %s",
              cloud.message(fin) or "no finish line")
    presses = [ln for ln in cloud.log_lines_since(cloud.GFCLOUD_LOG, offset) if "button pushed" in ln]
    ev["presses"] = len(presses)
    ctx.check(len(presses) == 1, "%d presses were seen; the test stands on exactly one", len(presses))
    ctx.sleep(1.0)
    ev["after"] = {"button_latch": button_latch(), "lock": cloud.latch_locked()}
    ctx.check(ev["after"]["lock"] and ev["after"]["button_latch"] == 1,
              "after the print the lock is %s and the button latch %s; both are set again",
              ev["after"]["lock"], ev["after"]["button_latch"])
    cloud.settle_cloud(ctx, offset)
    ctx.log("PASS: latch set and lock released at the button, cleared by the one press, cleared in "
            "%d samples while the kernel ran, set again after the job", len(samples))
