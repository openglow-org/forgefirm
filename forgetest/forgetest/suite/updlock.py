# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""An update job holds the machine, on the machine.

Its own module, so that no other test's fingerprint moves. The job is the
download of the published release: it fetches the firmware archive and
checks its signature, and applies nothing. The archive it leaves is
removed when it was not there before.
"""

import os

from ..catalog import test
from .motion import _job_post, _words

DL_FW = "/data/forgefirm/download.fw"


@test("update.job-locks", title="An update job holds the machine: the settings are locked while it runs",
      subsystem="update", kind="auto", est_min=3,
      covers=[("forgectrl", "src/update.*"), ("forgectrl", "src/lease.*")],
      requires=["update.release-check"],
      description="With a release published on the releases API, POST /update/download starts the download "
                  "job (202), which fetches the release's firmware archive and checks its signature and applies "
                  "nothing. While it runs, /status names the job (update:download, of kind system) as the "
                  "machine lease's holder, and a settings write and a posted job are each refused in its name "
                  "(409, the settings locked). When it ends, the lease is free and the same settings write is "
                  "taken. The downloaded archive is removed when it was not there before. That a log export "
                  "does not lock the settings while every other holder does is lease_test's.")
def job_locks(ctx):
    fc = ctx.forgectrl
    ev = ctx.evidence
    ctx.check(fc.wait_idle(timeout=30, abort=ctx.aborted), "machine not idle")
    st, rel = fc.get("/update/release")
    ev["release"] = {k: (rel or {}).get(k) for k in ("available", "version", "bytes")} if isinstance(rel, dict) else rel
    ctx.check(st == 200 and isinstance(rel, dict) and rel.get("available"), "no release is published to download: %s", rel)
    had = os.path.exists(DL_FW)
    units = fc.settings().get("ui_units") or "metric"

    def holder():
        return ((fc.status().get("lease") or {}).get("holder") or {})

    def job():
        st_, j = fc.get("/update/status")
        return j if isinstance(j, dict) else {}

    try:
        st, body = fc.post("/update/download")
        ctx.check(st == 202, "POST /update/download -> %s %s", st, _words(body)[:200])
        ctx.wait_for(lambda: holder().get("owner") == "update:download" or None, 20, poll=0.2)
        held = holder()
        ev["holder"] = held
        ctx.log("the lease while the download runs: %s", held)
        ctx.check(held.get("owner") == "update:download" and held.get("kind") == "system",
                  "the download job does not hold the machine: %s", held)
        refused = {}
        st, body = fc.post("/settings", params={"ui_units": units})
        refused["a settings write"] = [st, _words(body)[:200]]
        st2, body2 = _job_post(fc, "G21\n", name="beside-an-update")
        refused["a posted job"] = [st2, _words(body2)[:200]]
        ev["refused"] = refused
        ctx.log("beside the download: %s", refused)
        ctx.check(job().get("running") is True, "the download ended before the refusals were read: %s", job())
        ctx.check(st == 409 and "settings are locked" in _words(body), "a settings write beside the download -> %s %s",
                  st, _words(body)[:200])
        ctx.check(st2 == 409 and "holds the machine" in _words(body2), "a job beside the download -> %s %s",
                  st2, _words(body2)[:200])
        ctx.wait_for(lambda: job().get("running") is False or None, 600, poll=2.0)
        done = job()
        ev["job"] = done
        ctx.log("the download ended: %s", done)
        ctx.check(done and (done.get("result") or {}).get("ok") is True, "the download did not end well: %s", done)
        ctx.check(not holder(), "the lease was not given back: %s", holder())
        st, body = fc.post("/settings", params={"ui_units": units})
        ctx.check(st == 200, "the same settings write after the job -> %s %s", st, _words(body)[:200])
    finally:
        # The job runs on after a check that failed: its archive lands when it ends.
        ctx.wait_for(lambda: job().get("running") is False or None, 600, poll=2.0)
        if not had and os.path.exists(DL_FW):
            os.remove(DL_FW)
            ctx.log("removed the downloaded archive")
