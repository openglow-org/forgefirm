# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""forgectrl.* - the machine-services daemon's API, access control, and panel."""
import socket

from ..catalog import test

# The write guard reads the account and the session store (a machine with
# an account needs a session from the LAN; this host and the dev image
# write with the token), so both are the guard's domain. The fuse view
# is two-factor: the token and the physical button, read by button.c. The
# report channel's secret is the supervisor's, handed to the controller
# at its spawn, and the reports it lets through are the cooling engine's
# to keep (its report age).
_COVERS_AUTH = [("forgectrl", "src/auth.*"), ("forgectrl", "src/peer.*"), ("forgectrl", "src/main.c"),
                ("forgectrl", "src/session.*"), ("forgectrl", "src/users.*"), ("forgectrl", "src/button.*"),
                ("forgectrl", "src/ui/login.*"), ("forgectrl", "src/ui/wizard.js"),
                ("forgectrl", "src/super.c"), ("forgectrl", "src/cool.*"), ("forgectrl", "src/coolfmt.*")]


# The running controller reports to the cooling engine once a second
# (glowforge_cooling.c, REPORT_PERIOD_MS); the engine's report age is the
# seconds since the last report it took (-1 with none since a stop).
REPORT_PERIOD_S = 1.0
REPORT_FRESH_S = 3.0
REPORT_READS = 3


def reports_arriving(ctx, fc, reads=REPORT_READS, apart=2 * REPORT_PERIOD_S):
    """The engine's report age read `reads` times, `apart` seconds apart:
    each must be fresh (0 to REPORT_FRESH_S), which only the controller's
    own secret-bearing reports keep it. Returns the ages."""
    ages = []
    for i in range(reads):
        if i:
            ctx.sleep(apart)
        st, cs = fc.get("/cool/status")
        age = cs.get("report_age_s") if st == 200 and isinstance(cs, dict) else None
        ages.append(age)
        ctx.check(isinstance(age, (int, float)) and 0 <= age <= REPORT_FRESH_S,
                  "the cooling engine's report age reads %s (reads %s): the running controller's own "
                  "reports do not reach the engine", age, ages)
    ctx.log("the engine's report age across %d reads %.0f s apart: %s", reads, apart, ages)
    return ages


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
                  "the cooling report channel refuses a loopback report without the running "
                  "controller's secret or with another, while the controller's own reports keep "
                  "the engine's report age fresh, and refuses a non-loopback peer (over HTTP the "
                  "write is sent to HTTPS first, 302; over HTTPS the route answers 403 loopback "
                  "only); the fuse view is two-factor "
                  "(token and the physical button) and refused without either; "
                  "the flash and factory-restore chain is refused unauthenticated; a page "
                  "asked for without a session is sent to the login carrying its path. It runs on "
                  "the machine because what it proves is the booted daemon's listeners as they "
                  "face the network: the real TLS listener and its redirect, the board's own "
                  "hostname and LAN address, and the physical button behind the fuse view.")
def auth(ctx):
    fc = ctx.forgectrl
    ev = ctx.evidence

    st, body = fc.get("/status")
    ctx.log("GET /status -> %s", st)
    ctx.check(st == 200, "GET /status -> %s", st)

    # unauthenticated writes: every one must be refused before it acts
    for path, params in (("/controller/stop", None), ("/controller/start", None),
                         ("/mode", {"controller": "grbl"}), ("/settings", {"ui_units": "mm"}),
                         ("/diag/aa-offset-calibrate", None), ("/diag/abort", None),
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
    # The running controller's own reports, which carry its secret, reach
    # the engine: the engine's report age stays fresh across reads a few
    # report periods apart, with the refused reports above changing nothing.
    # The secret itself is read nowhere.
    ages = reports_arriving(ctx, fc)
    ev["report_age_s"] = ages

    # ...and a non-loopback peer is refused, even with a token. Over HTTP
    # the write is sent to HTTPS first (302, the listener's rule); over
    # HTTPS the route itself refuses the peer (403 loopback only), before
    # it reads any secret. Neither request follows the redirect, and the
    # self-signed certificate is not verified.
    from .setup import request, decode
    ip = lan_ip()
    ev["lan_ip"] = ip
    ctx.check(ip, "cannot determine the board's LAN address")
    token = {"X-ForgeFIRM-Token": fc.token, "X-ForgeFIRM-Report": "0" * 32}
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
