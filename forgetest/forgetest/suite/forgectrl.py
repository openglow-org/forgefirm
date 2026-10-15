# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""forgectrl.* - the machine-services daemon's API, access control, and panel."""
import json
import socket
import time

from ..catalog import test

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

    # the cooling report channel is the running controller's alone. A
    # loopback peer is not enough: anything on this host is one. The
    # supervisor hands the controller a secret at its spawn, and the route
    # asks for it. An idle report is what the controller sends every
    # period; the engine is idle here, so the accepted one changes nothing.
    # A dual-stack listener reports this peer as ::ffff:127.0.0.1, which
    # the loopback check must recognize in full.
    report = {"mode": "idle", "armed": "0"}
    st, body = fc.post("/cool/state", params=report)
    ev["cool_state_from_loopback_no_secret"] = st
    ctx.log("POST /cool/state from loopback, no secret -> %s %s", st, body if isinstance(body, str) else "")
    ctx.check(st == 403 and "controller's alone" in str(body),
              "/cool/state from loopback without the secret -> %s %r", st, body)
    st, body = fc.post("/cool/state", params=report, headers={"X-ForgeFIRM-Report": "0" * 32})
    ctx.check(st == 403, "/cool/state with a secret that is not the controller's -> %s %r", st, body)
    # The secret itself, as only root on this host can read it: out of
    # the running controller's environment. It is never logged.
    secret = None
    pid = (fc.get("/mode")[1] or {}).get("pid")
    if pid:
        try:
            with open("/proc/%d/environ" % int(pid), "rb") as f:
                for item in f.read().split(b"\0"):
                    if item.startswith(b"GF_REPORT_SECRET="):
                        secret = item.split(b"=", 1)[1].decode("ascii", "replace")
        except OSError:
            pass
    ev["controller_has_a_secret"] = bool(secret)
    ctx.check(secret and len(secret) == 32, "the running controller (pid %s) was handed no report secret", pid)
    st, body = fc.post("/cool/state", params=report, headers={"X-ForgeFIRM-Report": secret})
    ev["cool_state_from_loopback"] = st
    ctx.log("POST /cool/state from loopback with the controller's secret -> %s", st)
    ctx.check(st == 200, "/cool/state refused the controller's own secret (%s %r): the controller's "
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
    token = {"X-ForgeFIRM-Token": fc.token, "X-ForgeFIRM-Report": secret}
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
