# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""An M-code a package answers, on the machine.

Its own module, for the reason extcore.py gives. The package is the
reference package's id and key with a service of its own that answers
M160 on its call socket, so exthost's put-back takes it away like the
others. The jobs are dark: they move and never command the laser.
"""

import json
import os
import subprocess

from ..catalog import test
from .exthost import (EXT_ROOT, FWUP, REF_ID, REF_KEY, _as_found, _forgeext, _put_back, _svc, _tree, _until, _write)
from .motion import _job, _job_post, _job_wait, _words, kernel_start, kernel_xy_mm, machine_idle
from .setup import SAFETY_PHRASE, read_file, record_path, request

# The service: it answers M160 on the call socket the host hands it (fd 4), and keeps every call it was
# asked, with the monotonic time it was asked, in its data directory. P1 takes 2 s and is done; any
# other P is refused.
MC_SERVICE = r'''
import json, os, socket, time
data = os.environ["FFX_DATA"]
lst = socket.socket(fileno=int(os.environ["FFX_CALL_FD"]))
calls = []
while True:
    c, _ = lst.accept()
    try:
        buf = b""
        while b"\r\n\r\n" not in buf:
            k = c.recv(4096)
            if not k:
                break
            buf += k
        head, _, body = buf.partition(b"\r\n\r\n")
        n = 0
        for line in head.split(b"\r\n")[1:]:
            k, _, v = line.partition(b":")
            if k.strip().lower() == b"content-length":
                n = int(v)
        while len(body) < n:
            k = c.recv(4096)
            if not k:
                break
            body += k
        req = json.loads(body[:n] or b"{}")
        path = head.split(b" ")[1].decode()
        t0 = time.monotonic()
        words = req.get("words", {})
        if path == "/mcode" and words.get("P") == 1:
            time.sleep(2.0)
            status, ans = 200, {"message": "forgetest P1"}
        elif path == "/mcode":
            status, ans = 409, {"error": "the stand-in refused"}
        else:
            status, ans = 404, {"error": "no such call"}
        calls.append({"path": path, "req": req, "t": t0, "status": status})
        with open(os.path.join(data, "calls.json.new"), "w") as f:
            json.dump(calls, f)
        os.rename(os.path.join(data, "calls.json.new"), os.path.join(data, "calls.json"))
        out = json.dumps(ans).encode()
        c.sendall(b"HTTP/1.1 %d X\r\nContent-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n"
                  % (status, len(out)) + out)
    except Exception:
        pass
    finally:
        c.close()
'''


def _pack_mcode(work):
    import io
    import tarfile
    manifest = {"manifest": 1, "id": REF_ID, "name": "forgetest M-code", "version": "1.0.0",
                "author": "forgetest", "license": "MIT", "api": "0.1", "runtime": "python",
                "service": {"exec": "bin/mcode.py"}, "capabilities": ["mcode:160", "job_time.run"]}
    payload = os.path.join(work, "payload.tar.gz")
    with tarfile.open(payload, "w:gz") as t:
        for name, text, mode in (("manifest.json", json.dumps(manifest), 0o644), ("bin/mcode.py", MC_SERVICE, 0o755)):
            info = tarfile.TarInfo(name)
            data = text.encode()
            info.size, info.mode = len(data), mode
            t.addfile(info, io.BytesIO(data))
    conf = os.path.join(work, "fwup.conf")
    _write(conf, 'meta-product = "ForgeFIRM extension"\nmeta-description = "%s"\nmeta-version = "1.0.0"\n'
                 'meta-platform = "forgefirm-ext"\nfile-resource payload.tar.gz {\n    host-path = "%s"\n}\n'
           % (REF_ID, payload))
    key = os.path.join(work, REF_KEY)
    raw, signed = os.path.join(work, "raw.ffx"), os.path.join(work, "mcode.ffx")
    for cmd in ([FWUP, "-g", "-o", key], [FWUP, "-c", "-f", conf, "-o", raw],
                [FWUP, "-S", "-s", key + ".priv", "-i", raw, "-o", signed]):
        subprocess.run(cmd, check=True, capture_output=True, timeout=60, cwd=work)
    return signed, key + ".pub"


def _port_state(fc):
    st, body = fc.get("/motion/state")
    return body if st == 200 and isinstance(body, dict) else {}


@test("exthost.mcode", title="A job waits at an M-code a package answers, dark and still",
      subsystem="exthost", kind="auto", mode="grbl", est_min=4,
      covers=[("grblhal-glowforge", "src/glowforge_mcode.*"), ("grblhal-glowforge", "src/ctlport.*"),
              ("forgectrl", "src/mcode.*"), ("forgectrl", "src/extpkg.*"), ("forgectrl", "src/grblport.*"),
              ("forgeext", "src/main.c"), ("forgeext", "src/run.*"), ("forgeext", "src/caps.*"),
              ("forgeext", "src/manifest.*"), ("forgeext", "src/call.*")],
      requires=["exthost.service", "motion.job"],
      steps=["Bed clear; the head needs 10 mm of free travel toward +X. Nobody touches the gantry."],
      description="A package that asks for mcode:160 and job_time.run, the one granted, is installed and "
                  "turned on; its service answers POST /mcode on its call socket. The host's status names it "
                  "as the one that answers M160. A dark job (out 5 mm, M160 P1, back) plays through POST "
                  "/job: at the M-code the controller's port state names M160 with P 1, the head stands "
                  "still across the wait (the kernel's counters and the port's position unchanged over "
                  "1 s), and the service answers after 2 s; the job then goes on and ends done, every line "
                  "acknowledged, the head back where it began, the service asked once with the code and "
                  "its words, no discharge. A job that names M161, which nothing answers, fails at that "
                  "line with nothing moved. A job at M160 P2, which the service refuses, is held: the "
                  "port's state goes to Hold with the head still, and the job is aborted from there. "
                  "Everything is put back as exthost.service puts it back; the refusals the controller "
                  "makes of a table or an answer out of form, the timeout, the reset, and the barrier's "
                  "darkness under an open armed window are the driver's mcode_test harness's.")
def mcode(ctx):
    import tempfile
    fc = ctx.forgectrl
    ev = ctx.evidence
    machine_idle(ctx)
    prior = fc.settings().get("ext_enabled") or ""
    raw = read_file(record_path())
    found_tree = _tree(EXT_ROOT)
    dir_mode = os.stat(os.path.dirname(EXT_ROOT)).st_mode & 0o7777
    st, body, hdrs = request(fc.base, "GET", "/advisories/extensions", headers={"Host": fc.host_header()})
    etag = hdrs.get("etag")
    work = tempfile.mkdtemp(prefix="forgetest-ffx.")
    owner_key = os.path.join(EXT_ROOT, "keys", REF_KEY + ".pub")
    calls_file = os.path.join(EXT_ROOT, "data", REF_ID, "calls.json")

    def calls():
        try:
            return json.loads(read_file(calls_file) or "[]")
        except ValueError:
            return []

    try:
        archive, pub = _pack_mcode(work)
        import shutil
        shutil.copy(pub, owner_key)
        os.chmod(owner_key, 0o644)
        r = _forgeext("install", archive, "--consent-community", "--grant", "job_time.run")
        ctx.check(r.get("ok") is True, "the install with job_time.run granted -> %s", r.get("error"))
        st, reply = fc.post("/settings", data={"ext_enabled": "1", "advisory": etag, "phrase": SAFETY_PHRASE})
        ctx.check(st == 200, "ext_enabled=1 -> %s %r", st, reply)
        x = _until(ctx, lambda: _svc(REF_ID) if _svc(REF_ID).get("state") == "running" else None, 90, poll=0.5)
        ctx.check(x, "the service is not running: %s", _svc(REF_ID))

        def listed():
            try:
                doc = json.loads(read_file("/run/forgefirm/ext/status.json") or "{}")
            except ValueError:
                return None
            return doc.get("mcodes") if doc.get("mcodes") == [{"code": 160, "id": REF_ID}] else None
        ev["status_mcodes"] = _until(ctx, listed, 30, poll=0.5)
        ctx.check(ev["status_mcodes"], "the host's status does not name the package as the one that answers M160")
        ctx.sleep(3.0)                  # the relay tells the controller within its 2 s

        # A dark job that waits at M160 P1 for 2 s.
        x0, _y0 = kernel_start(ctx)
        st, body = _job_post(fc, "(forgetest exthost.mcode)\nG21\nG91\nG1 X5 F1200\nM160 P1\nG1 X-5 F1200\nG90\n")
        ctx.check(st == 200 and isinstance(body, dict) and body.get("state") == "running",
                  "POST /job -> %s %s", st, _words(body)[:200])
        waiting = _until(ctx, lambda: _port_state(fc) if isinstance(_port_state(fc).get("mcode"), dict) else None,
                         20, poll=0.1)
        ctx.check(waiting and waiting["mcode"].get("code") == 160 and waiting["mcode"].get("words") == {"P": 1},
                  "the port's state does not name M160 P1 while the job waits: %s", waiting)
        k1 = kernel_xy_mm(ctx)
        ctx.sleep(1.0)
        k2 = kernel_xy_mm(ctx)
        still = _port_state(fc)
        ev["wait"] = {"port": waiting, "kernel": [k1, k2], "port_after_1s": still}
        ctx.log("the job waits at M160: port %s, kernel %s -> %s", waiting, k1, k2)
        ctx.check(abs(k2[0] - k1[0]) < 0.001 and abs(k2[1] - k1[1]) < 0.001,
                  "the head moved while the job waited: %s -> %s", k1, k2)
        ctx.check(still.get("mpos") == waiting.get("mpos") and still.get("state") == "Idle",
                  "the port's position or state moved while the job waited: %s -> %s", waiting, still)
        ctx.check(abs(k1[0] - x0 - 5.0) < 0.05, "the job did not wait at the M-code 5 mm out: %.3f", k1[0] - x0)
        rec, far = _job_wait(ctx, fc, 40, x0)
        x1 = kernel_xy_mm(ctx)[0]
        got = calls()
        ev["job"] = {"record": rec, "farthest_mm": round(far, 3), "end_mm": round(x1 - x0, 3), "calls": got}
        ctx.log("the job at M160 P1: %s", ev["job"])
        ctx.check(rec["state"] == "done" and rec["reason"] == "", "the job did not end well: %s", rec)
        ctx.check(rec["sent"] == rec["acked"] == rec["lines"] + 1, "lines %s, sent %s, acked %s",
                  rec["lines"], rec["sent"], rec["acked"])
        ctx.check(rec["lit"] is False and rec["emission"]["laser_on_samples"] == 0, "a dark job's witnesses: %s",
                  rec["emission"])
        ctx.check(abs(x1 - x0) < 0.1, "the job did not end where it began: %.3f mm off", x1 - x0)
        ctx.check(len(got) == 1 and got[0]["path"] == "/mcode" and got[0]["req"] == {"code": 160, "words": {"P": 1}}
                  and got[0]["status"] == 200, "the service was not asked M160 P1 once: %s", got)

        # M161: nothing answers it, and the job stops where it is parsed.
        x0 = kernel_xy_mm(ctx)[0]
        st, body = _job_post(fc, "G21\nG91\nM161\nG1 X5 F1200\nG1 X-5 F1200\nG90\n")
        rec, far = _job_wait(ctx, fc, 20, x0)
        ev["unanswered"] = {"record": rec, "farthest_mm": round(far, 3)}
        ctx.log("the job at M161: %s", ev["unanswered"])
        ctx.check(rec["state"] == "failed" and "answered error:20" in (rec.get("reason") or ""),
                  "a job at M161 did not fail with error:20: %s", rec)
        ctx.check(far < 0.01, "a job at an M-code nothing answers moved the head %.3f mm", far)

        # M160 P2: the service refuses it, and the job is held there.
        x0 = kernel_xy_mm(ctx)[0]
        st, body = _job_post(fc, "G21\nG91\nM160 P2\nG1 X5 F1200\nG1 X-5 F1200\nG90\n")
        held = _until(ctx, lambda: _port_state(fc) if _port_state(fc).get("state") == "Hold" else None, 30, poll=0.2)
        ev["refused"] = {"port": held, "calls": calls()}
        ctx.log("the job at M160 P2: %s", ev["refused"])
        ctx.check(held and held.get("mcode") is None, "a job at a refused M160 is not held: %s", _port_state(fc))
        ctx.sleep(1.0)
        ctx.check(abs(kernel_xy_mm(ctx)[0] - x0) < 0.01, "the held job moved the head")
        st, body = fc.post("/job/abort")
        ctx.check(st == 200, "POST /job/abort -> %s %s", st, _words(body)[:160])
        rec, _far = _job_wait(ctx, fc, 20, x0)
        ctx.check(rec["state"] == "failed", "the held job did not end aborted: %s", rec)
        if _port_state(fc).get("state") == "Alarm":
            st, body = _job_post(fc, "G21\n", unlock="1")
            _job_wait(ctx, fc, 20, x0)
        ctx.check(_port_state(fc).get("state") == "Idle", "the controller is not idle after the abort: %s",
                  _port_state(fc))
    finally:
        # A check that failed mid-job leaves the job running; it holds the settings until it ends.
        if _job(fc).get("state") == "running":
            fc.post("/job/abort")
            _job_wait(ctx, fc, 20)
        _put_back(ctx, fc, work, prior, etag, raw, dir_mode)
    _as_found(ctx, fc, prior, raw, dir_mode, found_tree)
