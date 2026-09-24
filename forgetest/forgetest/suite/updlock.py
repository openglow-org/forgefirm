# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""An update job holds the machine, on the machine.

Its own module, so that no other test's fingerprint moves. The job is a
probe download of the published release: the download job itself, run on
the release's acceptance record instead of its firmware file, because
GitHub counts every request of the firmware file as an install. The
verification refuses the record and the probe keeps nothing; a download
the machine had staged is left as it was.
"""

import os

from ..catalog import test
from .motion import _job_post, _words

DL_FW = "/data/forgefirm/download.fw"
DL_PROBE = "/data/forgefirm/download-probe"
HELD = "an update job (download) holds the machine"


def _as_found(path):
    try:
        st = os.stat(path)
        return [st.st_size, st.st_mtime_ns]
    except OSError:
        return None


@test("update.job-locks", title="An update job holds the machine: the settings are locked while it runs",
      subsystem="update", kind="auto", est_min=2,
      covers=[("forgectrl", "src/update.*"), ("forgectrl", "src/relcheck.*"), ("forgectrl", "src/lease.*")],
      requires=["update.release-check"],
      description="With a release published on the releases API, POST /update/download?probe=1 starts the "
                  "download job (202) on the release's acceptance record in place of its firmware file, so "
                  "the test adds nothing to the firmware file's download count. The machine lease is taken "
                  "before the 202: a settings write and a posted job right after it are each refused in the "
                  "job's name (409, an update job (download) holds the machine, the settings locked), and "
                  "/status names the job (update:download, of kind system) as the holder while it runs. The "
                  "job fetches the record through the download's own path and ends refused by the signature "
                  "check with the record discarded. When it ends, the lease is free and the same settings "
                  "write is taken. A download the machine had staged is untouched and the probe leaves no "
                  "file. That a log export does not lock the settings while every other holder does is "
                  "lease_test's; that a probe never names the firmware file is relcheck_test's.")
def job_locks(ctx):
    fc = ctx.forgectrl
    ev = ctx.evidence
    ctx.check(fc.wait_idle(timeout=30, abort=ctx.aborted), "machine not idle")
    st, rel = fc.get("/update/release")
    if st == 200 and isinstance(rel, dict) and not rel.get("checked"):
        # the kept answer does not outlive a restart of the daemon: ask now
        st, rel = fc.post("/update/check")
        ctx.log("the release was not checked since the daemon started: POST /update/check -> %s", st)
    ev["release"] = {k: (rel or {}).get(k) for k in ("available", "version")} if isinstance(rel, dict) else rel
    ctx.check(st == 200 and isinstance(rel, dict) and rel.get("available"), "no release is published to probe: %s", rel)
    staged = _as_found(DL_FW)
    ev["staged_download"] = staged
    ctx.check(_as_found(DL_PROBE) is None, "a probe's file is left over from before: %s", DL_PROBE)
    units = fc.settings().get("ui_units") or "metric"

    def holder():
        return ((fc.status().get("lease") or {}).get("holder") or {})

    def job():
        st_, j = fc.get("/update/status")
        return j if isinstance(j, dict) else {}

    try:
        # The record is small, so the job can end within a second. The
        # lease is taken before the 202, so the settings write goes first
        # and at once: it is the same value, so a probe that ended before
        # it only costs another probe. The posted job goes only while the
        # settings are refused in the job's name.
        refused = None
        for attempt in (1, 2, 3):
            st, body = fc.post("/update/download", params={"probe": "1"})
            ctx.check(st == 202, "POST /update/download probe=1 -> %s %s", st, _words(body)[:200])
            st, body = fc.post("/settings", params={"ui_units": units})
            if st == 200:
                ctx.wait_for(lambda: job().get("running") is False or None, 120, poll=0.5)
                result = job().get("result") or {}
                ctx.log("probe %d ended before the settings write was read (%s); again", attempt, result)
                ctx.check("signature verification failed" in str(result.get("error")),
                          "the probe ended, and not refused by the signature check: %s", result)
                continue
            st2, body2 = _job_post(fc, "G21\n", name="beside-an-update")
            held = holder()
            refused = {"a settings write": [st, _words(body)[:200]], "a posted job": [st2, _words(body2)[:200]]}
            break
        ctx.check(refused is not None, "three probes each ended before a settings write could be refused")
        ev["refused"] = refused
        ev["holder"] = held
        ctx.log("beside the probe: %s; the lease: %s", refused, held)
        ctx.check(st == 409 and HELD in _words(body) and "settings are locked" in _words(body),
                  "a settings write beside the probe -> %s %s", st, _words(body)[:200])
        ctx.check(st2 == 409 and HELD in _words(body2), "a job beside the probe -> %s %s", st2, _words(body2)[:200])
        if held:
            ctx.check(held.get("owner") == "update:download" and held.get("kind") == "system",
                      "the probe does not hold the machine as the download job: %s", held)
        else:
            ctx.log("the probe had ended before /status was read; its refusals name it")
        ctx.wait_for(lambda: job().get("running") is False or None, 600, poll=1.0)
        done = job()
        ev["job"] = done
        ctx.log("the probe ended: %s", done)
        result = done.get("result") or {}
        ctx.check(result.get("ok") is False and "signature verification failed" in str(result.get("error")),
                  "the probe did not end refused by the signature check (the record fetched and discarded): %s",
                  result)
        ctx.check(not holder(), "the lease was not given back: %s", holder())
        st, body = fc.post("/settings", params={"ui_units": units})
        ctx.check(st == 200, "the same settings write after the job -> %s %s", st, _words(body)[:200])
    finally:
        # The job runs on after a check that failed: its end is waited for.
        ctx.wait_for(lambda: job().get("running") is False or None, 600, poll=2.0)
    ctx.check(_as_found(DL_FW) == staged, "the staged download moved: %s, and %s before", _as_found(DL_FW), staged)
    ctx.check(_as_found(DL_PROBE) is None, "the probe left its file: %s", DL_PROBE)
