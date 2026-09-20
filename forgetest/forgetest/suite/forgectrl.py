# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""forgectrl.* - the machine-services daemon's API, access control, and panel."""
import json
import os
import socket
import time
import urllib.parse

from ..catalog import test
from .. import hw

# The write guard reads the account and the session store (a machine with
# an account needs a session from the LAN; this host and the dev image
# write with the token), so both are the guard's domain.
_COVERS_AUTH = [("forgectrl", "src/auth.*"), ("forgectrl", "src/peer.*"), ("forgectrl", "src/main.c"),
                ("forgectrl", "src/session.*"), ("forgectrl", "src/users.*"),
                ("forgectrl", "src/ui/login.js"), ("forgectrl", "src/ui/wizard.js")]


def lan_ip():
    """The board's own non-loopback IPv4 (the address a LAN client would
    use), or None."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 9))
        ip = s.getsockname()[0]
    except OSError:
        ip = None
    finally:
        s.close()
    if ip and not ip.startswith("127."):
        return ip
    return None


@test("forgectrl.auth", title="API access control", subsystem="forgectrl", kind="auto", est_min=1,
      covers=_COVERS_AUTH,
      description="Every state-changing endpoint refuses an unauthenticated write (the factory "
                  "return, the SSH switch and the wizard's own routes included); a non-literal "
                  "Host, a non-literal Origin and a cross-site Sec-Fetch-Site are refused, while "
                  "the machine's own hostname passes and that name with a domain on it does not; "
                  "the cooling report channel accepts the loopback peer and refuses a non-loopback "
                  "one (over HTTP the write is sent to HTTPS first, 302; over HTTPS the route "
                  "answers 403 loopback only); the fuse view is two-factor "
                  "(token and the physical button) and refused without either; "
                  "the flash and factory-restore chain is refused unauthenticated; a page "
                  "asked for without a session is sent to the login carrying its path.")
def auth(ctx):
    fc = ctx.forgectrl
    ev = ctx.evidence

    st, body = fc.get("/status")
    ctx.log("GET /status -> %s", st)
    ctx.check(st == 200, "GET /status -> %s", st)

    # unauthenticated writes: every one must be refused before it acts
    for path, params in (("/controller/stop", None), ("/controller/start", None),
                         ("/mode", {"controller": "grbl"}), ("/settings", {"ui_units": "mm"}),
                         ("/diag/flow-verify", None), ("/diag/abort", None),
                         ("/update/apply", None), ("/boot", {"target": "a"}),
                         ("/update/check", None), ("/update/dismiss", {"version": "v0.0.0"}),
                         ("/system/reboot", None), ("/restore/factory", None),
                         ("/restore/factory-return", {"confirm": "1"}), ("/system/ssh", {"enable": "1"}),
                         ("/wiz/advisories/accept", None), ("/wiz/account", None),
                         ("/wiz/complete", None)):
        st, body = fc.post(path, params=params, auth=False)
        ctx.log("POST %s (no token) -> %s %s", path, st, body if isinstance(body, dict) else "")
        ev["noauth " + path] = st
        ctx.check(st == 403, "POST %s without a token -> %s, expected 403", path, st)
        ctx.check(isinstance(body, dict) and body.get("error") == "authentication required",
                  "POST %s without a token: unexpected body %r", path, body)

    # the upload sink refuses during body parse; only the status is asserted
    st, body = fc.post("/update/upload", data=b"not a firmware archive", auth=False,
                       headers={"Content-Type": "application/octet-stream"})
    ev["noauth /update/upload"] = st
    ctx.log("POST /update/upload (no token) -> %s", st)
    ctx.check(st in (400, 403), "POST /update/upload without a token -> %s", st)

    # An oversized body from an unauthenticated client must not be
    # accumulated in memory before the token check. The framework buffers
    # every POST body ahead of the callback, and its default is no limit,
    # so without a ceiling this is a way to take the daemon and any job
    # with it from the network, unauthenticated. The body here is far over
    # the cap; what matters is that the daemon answers and is still
    # serving afterwards, having refused the write.
    big = b"ui_units=mm&pad=" + (b"x" * (4 * 1024 * 1024))
    st, body = fc.post("/settings", data=big, auth=False,
                       headers={"Content-Type": "application/x-www-form-urlencoded"})
    ev["oversize_body_status"] = st
    ctx.log("POST /settings (no token, %d MiB body) -> %s", len(big) // (1024 * 1024), st)
    ctx.check(st == 403, "an oversized unauthenticated body -> %s, expected 403", st)
    st, body = fc.get("/status")
    ev["alive_after_oversize"] = st
    ctx.log("GET /status after the oversized body -> %s", st)
    ctx.check(st == 200, "the daemon did not survive an oversized body (%s)", st)

    # origin checks (read endpoint, so only the origin layer decides)
    st, body = fc.get("/status", headers={"Host": "evil.example.net"})
    ev["host_name"] = st
    ctx.log("GET /status Host=evil.example.net -> %s", st)
    ctx.check(st == 403, "a DNS-name Host was accepted (%s)", st)
    # the machine's own name passes; the same name with a domain on it
    # does not, because anyone can register one
    own = socket.gethostname()
    st, body = fc.get("/status", headers={"Host": own})
    ev["host_own_name"] = st
    ctx.log("GET /status Host=%s -> %s", own, st)
    ctx.check(st == 200, "the machine's own hostname was refused as a Host (%s)", st)
    st, body = fc.get("/status", headers={"Host": own + ".example.net"})
    ev["host_own_name_domain"] = st
    ctx.log("GET /status Host=%s.example.net -> %s", own, st)
    ctx.check(st == 403, "a domain name built on the machine's name was accepted (%s)", st)
    st, body = fc.get("/status", headers={"Origin": "http://evil.example.net"})
    ev["origin_name"] = st
    ctx.log("GET /status Origin=http://evil.example.net -> %s", st)
    ctx.check(st == 403, "a DNS-name Origin was accepted (%s)", st)
    st, body = fc.get("/status", headers={"Sec-Fetch-Site": "cross-site"})
    ev["sfs_cross"] = st
    ctx.log("GET /status Sec-Fetch-Site=cross-site -> %s", st)
    ctx.check(st == 403, "a cross-site fetch was accepted (%s)", st)
    st, body = fc.get("/status", headers={"Sec-Fetch-Site": "same-origin", "Origin": "http://127.0.0.1"})
    ctx.check(st == 200, "same-origin literal Origin refused (%s)", st)

    # the fuse view is two-factor: the token AND the physical button held.
    # Without the token: authentication refused; with the token and nobody
    # at the button: refused with the button message. The identity itself is
    # never fetched (it would land in this log).
    st, body = fc.get("/fuse-identity", auth=False)
    ev["fuse_noauth"] = st
    ctx.log("GET /fuse-identity (no token) -> %s %s", st, body if isinstance(body, dict) else "")
    ctx.check(st == 403 and isinstance(body, dict) and body.get("error") == "authentication required",
              "GET /fuse-identity without token -> %s %r", st, body)
    st, body = fc.get("/fuse-identity")
    ev["fuse_token_no_button"] = st
    ctx.log("GET /fuse-identity (token, button not held) -> %s %s", st, body if isinstance(body, dict) else "")
    msg = body.get("error", "") if isinstance(body, dict) else str(body)
    ctx.check(st == 403 and "button" in msg,
              "GET /fuse-identity with the token but no button -> %s %r (expected the two-factor refusal)",
              st, body)

    # the cooling report channel: the loopback peer is accepted. An idle
    # report is what the controller sends every period; the engine is idle
    # here, so it changes nothing. A dual-stack listener reports this peer
    # as ::ffff:127.0.0.1, which the check must recognize in full.
    st, body = fc.post("/cool/state", params={"mode": "idle", "armed": "0"})
    ev["cool_state_from_loopback"] = st
    ctx.log("POST /cool/state from loopback -> %s %s", st, body if isinstance(body, dict) else "")
    ctx.check(st == 200, "/cool/state refused the loopback peer (%s %r): the controller's "
              "reports never reach the engine", st, body)

    # ...and a non-loopback peer is refused, even with a token. Over HTTP
    # the write is sent to HTTPS first (302, the listener's rule); over
    # HTTPS the route itself refuses the peer (403 loopback only). Neither
    # request follows the redirect, and the self-signed certificate is
    # not verified.
    from .setup import request, decode
    ip = lan_ip()
    ev["lan_ip"] = ip
    ctx.check(ip, "cannot determine the board's LAN address")
    token = {"X-ForgeFIRM-Token": fc.token}
    report = {"mode": "idle", "armed": "0"}
    st, body, hdrs = request("http://%s" % ip, "POST", "/cool/state", data=report, headers=token)
    loc = hdrs.get("location", "")
    ev["cool_state_from_lan_http"] = {"status": st, "location": loc}
    ctx.log("POST http://%s/cool/state -> %s %s", ip, st, loc)
    ctx.check(st == 302 and loc.startswith("https://"),
              "a LAN write over HTTP -> %s %r, expected a 302 to HTTPS", st, loc)
    st, body, hdrs = request("https://%s" % ip, "POST", "/cool/state", data=report, headers=token)
    b = decode(body)
    ev["cool_state_from_lan"] = st
    ctx.log("POST https://%s/cool/state -> %s %s", ip, st, b if isinstance(b, dict) else "")
    ctx.check(st == 403 and isinstance(b, dict) and b.get("error") == "loopback only",
              "/cool/state accepted a non-loopback peer (%s %r)", st, b)

    # the login return path: a page asked for from the LAN without a
    # session is sent to the login carrying the path it asked for (with
    # its query), so the login can come back to it. Before the setup has
    # made the account the page is served instead (200).
    st, body, hdrs = request("https://%s" % ip, "GET", "/setup?step=laser.focus")
    loc = hdrs.get("location", "")
    ev["setup_from_lan"] = {"status": st, "location": loc}
    ctx.log("GET https://%s/setup?step=laser.focus (no session) -> %s %s", ip, st, loc)
    ctx.check(st in (200, 302), "the setup page from the LAN -> %s", st)
    if st == 302:
        ctx.check(loc == "/login?next=/setup%3Fstep%3Dlaser.focus",
                  "the login redirect does not carry the path: %r", loc)


@test("forgectrl.settings-bounds", title="Settings validation and restore", subsystem="forgectrl",
      kind="auto", est_min=1,
      covers=[("forgectrl", "src/settings.*"), ("forgectrl", "src/main.c"), ("forgectrl", "src/cam.c")],
      description="An over-length value and an out-of-range value are refused (400) and leave the "
                  "settings byte-identical; an in-range value is accepted (200). The lid lamp "
                  "idles at lid_lamp_idle (unset = 236), an out-of-range level is refused, a new "
                  "level applies to the lamp at once, and clearing it returns the default.")
def settings_bounds(ctx):
    fc = ctx.forgectrl
    ev = ctx.evidence
    before = fc.settings()
    ev["keys"] = len(before)

    st, body = fc.post("/settings", data={"gf_serial": "X" * 300})
    ev["overlong"] = st
    ctx.log("POST /settings gf_serial=<300 chars> -> %s %s", st, body if isinstance(body, dict) else "")
    ctx.check(st == 400, "over-length value -> %s, expected 400", st)

    st, body = fc.post("/settings", data={"laser_disarm_s": "99999"})
    ev["out_of_range"] = st
    ctx.log("POST /settings laser_disarm_s=99999 -> %s", st)
    ctx.check(st == 400, "out-of-range value -> %s, expected 400", st)

    # The cloud download guard is bytes, so its range is far wider than the
    # other numeric keys: check the far end is still a wall.
    st, body = fc.post("/settings", data={"pulse_reject_threshold_bytes": "2000000000"})
    ev["pulse_bytes_out_of_range"] = st
    ctx.log("POST /settings pulse_reject_threshold_bytes=2000000000 -> %s", st)
    ctx.check(st == 400, "out-of-range byte limit -> %s, expected 400", st)

    st, body = fc.post("/settings", data={"no_such_key_forgetest": "1"})
    ev["unknown_key"] = st
    ctx.log("POST /settings no_such_key_forgetest=1 -> %s", st)
    ctx.check(st in (400, 404), "unknown key -> %s, expected 400", st)

    after = fc.settings()
    ctx.check(json.dumps(after, sort_keys=True) == json.dumps(before, sort_keys=True),
              "settings changed after refused writes")
    ctx.log("settings unchanged after the refused writes")

    # an accepted in-range write: rewrite a present key with its own value
    key = None
    for k in ("ui_units", "laser_disarm_s", "cool_flow_rise", "rail_settle_s"):
        v = before.get(k)
        if isinstance(v, str) and v != "":
            key = k
            break
    if key is None:
        key, val = "ui_units", "metric"
        ctx.log("no settable key is present; writing %s=%s (recorded in evidence)", key, val)
    else:
        val = before[key]
    st, body = fc.post("/settings", data={key: val})
    ev["accepted"] = {"key": key, "value": val, "status": st}
    ctx.log("POST /settings %s=%s -> %s", key, val, st)
    ctx.check(st == 200, "in-range write -> %s, expected 200", st)
    final = fc.settings()
    others_before = {k: v for k, v in before.items() if k != key}
    others_after = {k: v for k, v in final.items() if k != key}
    ctx.check(others_before == others_after, "other settings changed by the write")
    ctx.check(final.get(key) == val, "%s reads back %r, wrote %r", key, final.get(key), val)

    # the lid lamp's idle level: resting at the setting, bounded, applied live
    lamp_was = (before.get("lid_lamp_idle") or "").strip()
    want = lamp_was or "236"
    got = ctx.sysfs("pic/lid_led")
    ev["lid_lamp"] = {"setting": lamp_was, "resting": got}
    ctx.log("lid lamp: setting %r, pic/lid_led=%s (expected %s)", lamp_was, got, want)
    ctx.check(got == want, "lid lamp rests at %s, lid_lamp_idle is %s", got, want)
    for bad in ("256", "-1", "bright"):
        st, body = fc.post("/settings", data={"lid_lamp_idle": bad})
        ctx.check(st == 400, "lid_lamp_idle=%s -> %s, expected 400", bad, st)
    ctx.log("lid_lamp_idle 256 / -1 / bright refused")
    try_level = "100" if want != "100" else "120"
    st, body = fc.post("/settings", data={"lid_lamp_idle": try_level})
    ctx.check(st == 200, "lid_lamp_idle=%s -> %s, expected 200", try_level, st)
    applied = None
    t0 = time.time()
    while time.time() - t0 < 5:
        applied = ctx.sysfs("pic/lid_led")
        if applied == try_level:
            break
        ctx.sleep(0.2)
    ctx.log("lid_lamp_idle=%s -> pic/lid_led=%s after %.1f s", try_level, applied, time.time() - t0)
    # an empty value clears the key: the query-string form carries it
    st, body = (fc.post("/settings", params={"lid_lamp_idle": ""}) if not lamp_was
                else fc.post("/settings", data={"lid_lamp_idle": lamp_was}))
    ctx.check(st == 200, "restoring lid_lamp_idle=%r -> %s", lamp_was, st)
    t0 = time.time()
    back = None
    while time.time() - t0 < 5:
        back = ctx.sysfs("pic/lid_led")
        if back == want:
            break
        ctx.sleep(0.2)
    ev["lid_lamp"].update({"applied": applied, "restored": back})
    ctx.check(applied == try_level, "lamp did not follow lid_lamp_idle=%s (reads %s)", try_level, applied)
    ctx.check(back == want, "lamp did not return to %s after the restore (reads %s)", want, back)
    ctx.log("lid lamp follows the setting live and returns to %s", want)


@test("forgectrl.panel-serves", title="Control panel and status endpoints", subsystem="forgectrl",
      kind="auto", est_min=1,
      covers=[("forgectrl", "src/ui.*"), ("forgectrl", "src/ui/**"), ("forgectrl", "src/status.*"),
              ("forgectrl", "src/cam.c"), ("forgectrl", "src/main.c"), ("forgectrl", "src/super.c"),
              ("grblhal-glowforge", "src/glowforge_status.c"), ("grblhal-glowforge", "src/serial.c"),
              ("forgectrl", "src/curverec.*")],
      description="The panel page is served, /status carries the machine telemetry the panel and "
                  "the acceptance tool read (including the sys block: CPU busy percent over the "
                  "interval since the previous read, memory used percent; and homed_axes, "
                  "the axes that carry a reference, Z alone once the lens has taken its "
                  "own at the controller's start), and /cam/status "
                  "answers. In GRBL mode with a live controller, /status also echoes the "
                  "controller's published state file as the grbl block (fresh age, machine "
                  "state, sender session, laser window and dose model, modal report), "
                  "GET /grbl/settings serves the published $$ view, and the controller's "
                  "settings store is /data/forgefirm/EEPROM-glowforge.DAT with nothing of "
                  "it at the top of /data.")
def panel_serves(ctx):
    fc = ctx.forgectrl
    ev = ctx.evidence
    st, body = fc.get("/", raw=True)
    ev["panel_status"] = st
    ctx.log("GET / -> %s (%d bytes)", st, len(body) if body else 0)
    ctx.check(st == 200, "GET / -> %s", st)
    text = body.decode("utf-8", "replace")
    ctx.check("<html" in text.lower() and "ForgeFIRM" in text, "the panel does not look like the panel")
    ctx.check(fc.token and fc.token in text, "the panel does not embed the bearer token")
    ctx.check("<link " not in text and "<script src=" not in text,
              "the panel references an external asset (the build did not bundle src/ui/)")
    # The daemon stores the page gzipped and inflates it once at first
    # request; what it serves is the plain page with the theme attribute
    # the head script sets and the one save bar every settings tab shares.
    ctx.check("data-bs-theme" in text, "the panel lacks the theme attribute (inflate failed?)")
    ctx.check('id="savebar"' in text, "the panel lacks the save bar")

    s = fc.status()
    for key in ("state", "switches", "coolant", "fans"):
        ctx.check(key in s, "/status lacks %r", key)
    ev["state"] = s.get("state")
    ev["switches"] = s.get("switches")
    ctx.log("/status state=%s switches=%s", s.get("state"), s.get("switches"))
    for key in ("lid", "button", "interlock_ok", "head", "hv_enable"):
        ctx.check(key in (s.get("switches") or {}), "/status switches lacks %r", key)

    # SoC utilization rides /status next to the temperatures. The CPU
    # number is a delta over the interval since the previous read, so
    # the read above primes it; after a beat both percents must be
    # numbers in range.
    ctx.sleep(1)
    sys_ = ctx.forgectrl.status().get("sys") or {}
    ev["sys"] = sys_
    ctx.log("/status sys=%s", sys_)
    ctx.check(isinstance(sys_.get("cpu_pct"), (int, float)) and 0.0 <= sys_["cpu_pct"] <= 100.0,
              "/status sys.cpu_pct is not a percent: %s", sys_)
    ctx.check(isinstance(sys_.get("mem_pct"), (int, float)) and 0.0 < sys_["mem_pct"] < 100.0,
              "/status sys.mem_pct is not a percent: %s", sys_)

    # The lens takes its own reference on the hall edge at every
    # controller start, so with a controller running Z is referenced on
    # its own while X and Y wait for a home: homed_axes names the axes
    # that carry one, and Z reads inside the lens reach rather than the
    # zero an unreferenced axis would show.
    axes = s.get("homed_axes")
    ev["homed_axes"] = axes
    ctx.check(isinstance(axes, int) and 0 <= axes <= 7,
              "/status homed_axes is not an axis mask: %s", axes)
    ctx.check(bool(s.get("homed")) == (axes == 7),
              "/status homed (%s) disagrees with homed_axes (%s)",
              s.get("homed"), axes)
    st, mode0 = fc.get("/mode")
    if isinstance(mode0, dict) and mode0.get("controller") == "running":
        lens = s.get("lens") or {}
        pos = s.get("pos") or {}
        ctx.log("/status homed_axes=%s pos.z=%s reach=%s..%s", axes,
                pos.get("z"), lens.get("reach_min"), lens.get("reach_max"))
        ctx.check(axes is not None and axes & 4,
                  "Z is not referenced with a controller running: %s", axes)
        ctx.check(isinstance(pos.get("z"), (int, float))
                  and lens.get("reach_min") is not None
                  and lens["reach_min"] <= pos["z"] <= lens["reach_max"],
                  "Z %s is outside the lens reach %s..%s", pos.get("z"),
                  lens.get("reach_min"), lens.get("reach_max"))

    st, cam = fc.get("/cam/status")
    ev["cam_status"] = st
    ctx.log("GET /cam/status -> %s %s", st, cam)
    ctx.check(st == 200 and isinstance(cam, dict) and "running" in cam, "GET /cam/status -> %s", st)

    # The controller's published state, echoed only while a live GRBL
    # controller runs (glowforge_status.c -> /run/forgefirm -> /status).
    st, mode = fc.get("/mode")
    if isinstance(mode, dict) and mode.get("mode") == "grbl" and mode.get("controller") == "running":
        g = ctx.forgectrl.status().get("grbl") or {}
        ev["grbl"] = g
        ctx.log("/status grbl=%s", g)
        ctx.check(isinstance(g.get("age_s"), (int, float)) and g["age_s"] < 30,
                  "/status grbl block missing or stale: %s", g)
        rep = g.get("report") or {}
        for key in ("state", "sender", "laser", "modals"):
            ctx.check(key in rep, "/status grbl.report lacks %r", key)
        ctx.check((rep.get("laser") or {}).get("model") in ("density", "analog"),
                  "grbl.report.laser carries no model: %s", rep.get("laser"))
        ctx.check((rep.get("laser") or {}).get("curve"),
                  "grbl.report.laser carries no dose curve: %s", rep.get("laser"))
        st, text = fc.get("/grbl/settings", raw=True)
        ev["grbl_settings_status"] = st
        ctx.check(st == 200 and b"$35=" in (text or b""),
                  "GET /grbl/settings -> %s without the $$ view", st)
        # The $-settings persist in the data directory, never loose in /data.
        nvs = "/data/forgefirm/EEPROM-glowforge.DAT"
        ev["grbl_nvs"] = os.path.isfile(nvs)
        ctx.check(os.path.isfile(nvs), "the controller's settings store is not at %s", nvs)
        ctx.check(not os.path.exists("/data/EEPROM-glowforge.DAT"),
                  "a settings store remains at the top of /data")
    else:
        ctx.log("no live GRBL controller (%s); grbl block checks skipped", mode)

    # The dose-curve recorder's read surface answers in any mode.
    st, cs = fc.get("/curve/status")
    ev["curve_status"] = cs
    ctx.check(st == 200 and isinstance(cs, dict) and cs.get("state") in
              ("idle", "waiting", "recording", "done", "failed"),
              "GET /curve/status -> %s %s", st, cs)
    st, text = fc.get("/curve/ladder.gcode", raw=True)
    ctx.check(st == 200 and b"S1000" in (text or b"") and b"M5" in (text or b""),
              "GET /curve/ladder.gcode -> %s without the ladder", st)


class _EventStream:
    """GET /events over a raw socket bound to a chosen loopback source
    address. Every address in 127.0.0.0/8 is this host, so the test can be
    several peers at once, and the daemon counts streams per peer address."""

    def __init__(self, base, source, timeout=5.0):
        u = urllib.parse.urlsplit(base)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind((source, 0))
        self.sock.settimeout(timeout)
        self.sock.connect((u.hostname or "127.0.0.1", u.port or 80))
        self.sock.sendall(b"GET /events HTTP/1.1\r\nHost: %s\r\nAccept: text/event-stream\r\n"
                          b"Connection: close\r\n\r\n" % (u.netloc or "127.0.0.1").encode())
        self.buf = b""
        self.eof = False        # the response is over: the last chunk, or the socket closed
        head = self._until(b"\r\n\r\n")
        self.status = int(head.split(b" ", 2)[1]) if head.startswith(b"HTTP/") else 0
        self.head = head.decode("latin-1", "replace")

    def _fill(self):
        try:
            chunk = self.sock.recv(4096)
        except socket.timeout:
            return False
        if not chunk:
            self.eof = True
            return False
        self.buf += chunk
        return True

    def _until(self, mark):
        while mark not in self.buf:
            if not self._fill():
                break
        i = self.buf.find(mark)
        if i < 0:
            out, self.buf = self.buf, b""
            return out
        out, self.buf = self.buf[:i], self.buf[i + len(mark):]
        return out

    def text(self, seconds):
        """Everything the stream says in the next `seconds` (chunk framing
        and all: the checks look for event names inside it)."""
        end = time.time() + seconds
        self.sock.settimeout(0.2)       # the window is the caller's, not one long read
        while time.time() < end and not self.eof:
            self._fill()
            if self.buf.endswith(b"\r\n0\r\n\r\n"):
                self.eof = True
        out, self.buf = self.buf, b""
        return out.decode("utf-8", "replace")

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


@test("events.stream", title="The event stream: edges arrive, and the cap holds",
      subsystem="forgectrl", kind="auto", mode="grbl", est_min=2,
      covers=[("forgectrl", "src/events.*"), ("forgectrl", "src/main.c"), ("forgectrl", "src/status.*"),
              ("forgectrl", "src/grblport.*")],
      description="GET /events from three loopback source addresses: each gets 200, "
                  "text/event-stream, and the hello event. A fourth address gets 503 with the "
                  "reason, and GET /settings still answers while the three are held. A second "
                  "stream from the first address is served, and the older one gets bye and the "
                  "end of its response. An edge arrives on an open stream: POST /motion/release "
                  "and /motion/energize show up as motors.released and motors.energized, with "
                  "ids that count up (X and Y lose their reference, as after any release). With "
                  "every stream closed, the places come back within two keep-alive intervals.")
def events_stream(ctx):
    ev = ctx.evidence
    fc = hw.Forgectrl()
    streams = []
    try:
        # A stream an earlier test closed keeps its place until the daemon's
        # next write to it (its keep-alive), so the three places may not be
        # free yet: that is the daemon as documented, and the last part of
        # this test measures it. The three are opened once they can be.
        deadline = time.time() + 25
        while True:
            for src in ("127.0.0.2", "127.0.0.3", "127.0.0.4"):
                streams.append(_EventStream(fc.base, src))
            if all(s.status == 200 for s in streams) or time.time() > deadline:
                break
            ctx.log("an earlier stream still holds a place (%s): waiting for it to be given back",
                    [s.status for s in streams])
            for s in streams:
                s.close()
            streams = []
            ctx.sleep(3)
        for s, src in zip(streams, ("127.0.0.2", "127.0.0.3", "127.0.0.4")):
            hello = s.text(1.0)
            ctx.check(s.status == 200 and "text/event-stream" in s.head and "event: hello" in hello,
                      "GET /events from %s -> %s, %r", src, s.status, hello[:120])
        fourth = _EventStream(fc.base, "127.0.0.5")
        body = fourth.text(1.0)
        fourth.close()
        ev["fourth"] = [fourth.status, body.strip()[-120:]]
        ctx.log("the fourth stream -> %s %s", fourth.status, ev["fourth"][1])
        ctx.check(fourth.status == 503 and "every event stream is taken" in body,
                  "the fourth stream -> %s %r, expected 503 and the reason", fourth.status, body[:160])
        st, _body = fc.get("/settings")
        ctx.check(st == 200, "GET /settings -> %s with three event streams held", st)

        # One per address, by replacement.
        newer = _EventStream(fc.base, "127.0.0.2")
        hello = newer.text(1.0)
        ctx.check(newer.status == 200 and "event: hello" in hello,
                  "a second stream from one address -> %s %r", newer.status, hello[:120])
        bye = streams[0].text(2.0)
        ev["replaced"] = {"bye": "event: bye" in bye, "ended": streams[0].eof}
        ctx.check("event: bye" in bye and "replaced" in bye, "the older stream was not told: %r", bye[-160:])
        ctx.check(streams[0].eof, "the older stream was not ended")
        streams[0].close()
        streams[0] = newer

        # An edge, on a stream that has been open all along.
        st, body = fc.post("/motion/release")
        ctx.check(st == 200, "POST /motion/release -> %s %s", st, body)
        ctx.sleep(1.0)
        st, body = fc.post("/motion/energize")
        ctx.check(st == 200, "POST /motion/energize -> %s %s", st, body)
        text = streams[1].text(2.0)
        ev["edges"] = [l for l in text.splitlines() if l.startswith(("id:", "event:"))]
        ctx.log("the stream carried: %s", ev["edges"])
        i_rel, i_en = text.find("event: motors.released"), text.find("event: motors.energized")
        ctx.check(0 <= i_rel < i_en, "the release and the energize did not arrive in order: %r", text[-300:])
        ids = [int(l.split(":")[1]) for l in text.splitlines() if l.startswith("id:")]
        ctx.check(ids and ids == sorted(ids) and len(set(ids)) == len(ids), "the ids do not count up: %s", ids)
    finally:
        for s in streams:
            s.close()
        if fc.status().get("motors_released"):
            fc.post("/motion/energize")

    # A closed client is noticed at the daemon's next write to it.
    def reopened():
        s = _EventStream(fc.base, "127.0.0.5")
        try:
            return s.status == 200
        finally:
            s.close()
    took = ctx.wait_for(reopened, 25, poll=1.0)
    ev["place_back_s"] = took
    ctx.check(took is not None, "no stream could be opened 25 s after every stream was closed")
    ctx.log("PASS: three streams, the fourth refused in words with the settings route answering, a "
            "replacement told and ended, two edges in order, and a place back after %.0f s", took)


def _lease(fc):
    return (fc.status().get("lease") or {})


def _text(body):
    return body if isinstance(body, str) else json.dumps(body)


@test("forgectrl.lease", title="The machine lease: a holder refuses everything else, by name",
      subsystem="forgectrl", kind="auto", est_min=2,
      covers=[("forgectrl", "src/lease.*"), ("forgectrl", "src/main.c"), ("forgectrl", "src/status.*"),
              ("forgectrl", "src/wizdark.*"), ("forgectrl", "src/diag.*"), ("forgectrl", "src/curverec.*"),
              ("forgectrl", "src/update.*"), ("forgectrl", "src/logs.*"), ("forgectrl", "src/super.*"),
              ("forgectrl", "src/events.*")],
      description="The switches check is started and left waiting at its first prompt: it moves "
                  "nothing, and it holds the machine lease for as long as it waits. /status must "
                  "name it as the holder (wizard:switches, kind hardware), the event stream must "
                  "report lease.changed, and everything that asks the lease must be refused with "
                  "409 and the holder's name: a diagnostic, the dose-curve recorder, a log export, a "
                  "mode switch to the mode already in force, POST /controller/start, and POST "
                  "/settings (with 'settings are locked'). Only requests that would do no harm if "
                  "the lease failed are made: no reboot, no boot-slot change, no update job. The "
                  "check is then aborted, and the lease must read free, lease.changed must say so, "
                  "and POST /settings must be accepted again. Whatever a failed refusal started is "
                  "stopped on the way out.")
def lease(ctx):
    from .setup_dark import dark

    ev = ctx.evidence
    fc = hw.Forgectrl()
    wid, owner = "switches", "wizard:switches"
    rest = _lease(fc)
    ctx.check("holder" in rest and rest["holder"] is None, "the machine is not free at the start: %s", rest)
    ctx.check(set(rest.get("observed") or {}) == {"sender", "motors_released"},
              "what is observed outside the lease is not reported: %s", rest)
    mode = (fc.get("/mode")[1] or {}).get("mode")
    units = fc.settings().get("ui_units") or "metric"
    stream = _EventStream(fc.base, "127.0.0.6")
    ctx.check(stream.status == 200, "GET /events -> %s", stream.status)
    stream.text(0.5)
    started = False
    try:
        st, body = fc.post("/wiz/%s/start" % wid)
        ctx.check(st == 200, "the %s check did not start (%s %s)", wid, st, _text(body))
        started = True
        ok = ctx.wait_for(lambda: (_lease(fc).get("holder") or {}).get("owner") == owner, 10, poll=0.2)
        held = _lease(fc).get("holder") or {}
        ev["holder"] = held
        ctx.log("the lease while the check waits: %s", held)
        ctx.check(ok is not None and held.get("kind") == "hardware" and "switches" in (held.get("words") or ""),
                  "the lease does not name the running check: %s", held)

        refused = {}
        for name, path, kw in (
                ("a diagnostic", "/diag/flow-verify", {}),
                ("the recorder", "/curve/record", {}),
                ("a log export", "/logs/export", {}),
                ("a mode switch", "/mode", {"params": {"controller": mode}}),
                ("the controller's start", "/controller/start", {}),
                ("a settings write", "/settings", {"params": {"ui_units": units}})):
            st, body = fc.post(path, **kw)
            refused[name] = [st, _text(body)[:160]]
            ctx.check(st == 409 and "holds the machine" in _text(body) and "switches" in _text(body),
                      "%s under the wizard's hold -> %s %s", name, st, _text(body)[:160])
        ctx.check("settings are locked" in refused["a settings write"][1],
                  "the settings refusal does not say they are locked: %s", refused["a settings write"])
        ev["refused"] = refused
        ctx.check((_lease(fc).get("holder") or {}).get("owner") == owner,
                  "a refused request took the lease away: %s", _lease(fc))
    finally:
        if started:
            fc.post("/wiz/%s/abort" % wid)
            ctx.wait_for(lambda: not dark(fc).get("running"), 30)
        # Nothing a failed refusal may have started is left running.
        fc.post("/diag/abort")
        fc.post("/curve/stop")

    ok = ctx.wait_for(lambda: _lease(fc).get("holder") is None, 30, poll=0.5)
    ctx.check(ok is not None, "the lease was not given back after the abort: %s", _lease(fc))
    st, body = fc.post("/settings", params={"ui_units": units})
    ctx.check(st == 200, "POST /settings with the machine free again -> %s %s", st, _text(body)[:120])
    text = stream.text(2.0)
    stream.close()
    changes = [l for l in text.splitlines() if l.startswith("data:") and "owner" in l]
    ev["lease_changed"] = changes
    ctx.log("the event stream: %s", changes)
    i_take, i_free = text.find('"owner":"%s"' % owner), text.find('"owner":null')
    ctx.check("event: lease.changed" in text and 0 <= i_take < i_free,
              "lease.changed did not report the hold and then its end: %r", text[-300:])
    ctx.log("PASS: %s held the machine, six requests were refused by name, the abort freed it, and the "
            "event stream said both", owner)
