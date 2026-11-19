# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""The bench actuator (forgefixture): the box on the bench network that
opens the lid loop, pulls the interlock loop and presses the button on
request, so an operator test can run with nobody in the room.

Configured by a bench-local file, FIXTURE_CONFIG (default
/data/forgetest/fixture.json, mode 0600, never in the repo):

    {"hostname": "forgefixture", "key": "<the fixture's API key>",
     "ip": null, "channels": ["lid", "interlock", "button"],
     "arm_press": false}

`hostname` is resolved as <hostname>.local by the resolver here (the
image carries no mDNS resolver; the fixture answers the query itself);
`ip` overrides that. `channels` names what is wired. `arm_press` is the
one policy switch: whether the fixture may press the button to arm the
laser for a live test (a person's press by default).

The key never travels: every request carries a nonce the fixture issued
for it (GET /nonce) and an HMAC-SHA256 of the request under the key
(request_mac), and the fixture takes each nonce once.

The fixture does not read the machine: every action the runner asks of
it is verified through the machine's own readings afterward, exactly as
an operator's hand is (Context.act). Contract: fixture/README.md.
"""
import hashlib
import hmac
import http.client
import json
import os
import socket
import struct
import threading
import time
import urllib.error
import urllib.request

FIXTURE_CONFIG = os.environ.get("FORGETEST_FIXTURE_CONFIG", "/data/forgetest/fixture.json")
CHANNELS = ("lid", "interlock", "button")
MDNS_GROUP = "224.0.0.251"
MDNS_PORT = 5353
RESOLVE_TTL_S = 300.0           # a resolved address is trusted this long
BUTTON_GAP_S = 0.3              # the release between two presses: long enough for the controller to see it
PULSE_WAIT_S = 1.5              # a pulse in progress ends within the firmware's 500 ms clamp
PULSE_MAX_S = 0.5               # the firmware's clamp: the longest a press the box made can still be held
NONCE_LIFE_S = 10.0             # the firmware's AUTH_NONCE_LIFE_US: how long the box takes a request after its nonce
# The requests the box takes the same way twice: a resend of one whose
# answer was lost changes nothing. A press is never among them.
TAKES_TWICE = (("GET", "/"), ("POST", "/lid"), ("POST", "/interlock"), ("POST", "/release"))
# The answers the firmware gives a press before it calls the relay
# (fixture/main/api.c): a body it will not take, a request it does not
# authorize, the enable jumper out or a pulse in progress. Any other answer
# but 200 comes after the relay call (a reply it could not build), so the
# press may have been made.
PRESS_REFUSED = (400, 401, 409, 413)
# The requests that set or let go a loop.
LOOP_PATHS = ("/lid", "/interlock", "/release")
# A request that went out and got no answer may still be served by the box
# until its nonce's life is over (a press: and held for the longest pulse
# after): until when, on the monotonic clock, for the presses and for the
# loop requests. Kept for the process, not for a client: the runner makes a
# new client at every probe, and the box is the same one.
_unanswered = {"press": 0.0, "loop": 0.0}


def unanswered_left(kind=None):
    """Seconds until the box can no longer act on a request this process
    sent and got no answer to: a press ("press"), a loop request ("loop"),
    or either; 0 when there is none."""
    until = _unanswered[kind] if kind else max(_unanswered.values())
    return max(0.0, until - time.monotonic())


def request_mac(key, method, uri, nonce, body):
    """The MAC a request carries (fixture/main/auth.h): HMAC-SHA256 under
    the key over the method, the URI as sent, the nonce and the body's
    bytes, the first three each ended by a newline."""
    msg = b"\n".join((method.encode("ascii"), uri.encode("ascii"), nonce.encode("ascii"))) + b"\n" + (body or b"")
    return hmac.new(key.encode("utf-8"), msg, hashlib.sha256).hexdigest()


class FixtureError(Exception):
    """A request the fixture did not carry out as asked. `sent` is True
    when the failure came after the request went out (no reply, or a
    reply cut off), so the box may have acted on it; a refusal, a failed
    lookup and a failed connect leave it False."""

    def __init__(self, *args, sent=False):
        super().__init__(*args)
        self.sent = sent


class PressWithheld(FixtureError):
    """A press the caller's judgment refused right before its request went
    out (Fixture.act's judge): nothing was sent. `why` is the judgment, in
    words an operator can be shown; `error` is the exception a judgment that
    failed ended with, for the log."""

    def __init__(self, why, error=None):
        super().__init__("the press was withheld at its request: %s" % why)
        self.why = why
        self.error = error


# ---------------------------------------------------------------- mDNS

def mdns_query(name):
    """One mDNS A query for `name` (e.g. "forgefixture.local"), as the
    bytes on the wire: a standard DNS header with id 0, one question,
    class IN with the unicast-response bit set so the answer comes back
    to this socket rather than the group."""
    labels = b"".join(struct.pack("B", len(p)) + p.encode("ascii") for p in name.rstrip(".").split("."))
    header = struct.pack("!HHHHHH", 0, 0, 1, 0, 0, 0)
    return header + labels + b"\x00" + struct.pack("!HH", 1, 0x8001)


def _read_name(data, off):
    """A DNS name at `off`, compression pointers followed; (name, next
    offset)."""
    parts = []
    jumped = False
    end = off
    guard = 0
    while True:
        if off >= len(data):
            raise ValueError("truncated name")
        n = data[off]
        if n == 0:
            off += 1
            break
        if n & 0xC0 == 0xC0:
            ptr = struct.unpack("!H", data[off:off + 2])[0] & 0x3FFF
            if not jumped:
                end = off + 2
            jumped = True
            off = ptr
            guard += 1
            if guard > 32:
                raise ValueError("pointer loop")
            continue
        parts.append(data[off + 1:off + 1 + n].decode("ascii", "replace"))
        off += 1 + n
    if not jumped:
        end = off
    return ".".join(parts).lower(), end


def mdns_answers(data, name):
    """The IPv4 addresses in an mDNS response's A records for `name`
    (answers and additionals; a response is anything with QR set)."""
    name = name.rstrip(".").lower()
    out = []
    try:
        _id, flags, qd, an, ns, ar = struct.unpack("!HHHHHH", data[:12])
        if not flags & 0x8000:
            return out
        off = 12
        for _ in range(qd):
            _n, off = _read_name(data, off)
            off += 4
        for _ in range(an + ns + ar):
            rname, off = _read_name(data, off)
            rtype, rclass, _ttl, rdlen = struct.unpack("!HHIH", data[off:off + 10])
            off += 10
            rdata = data[off:off + rdlen]
            off += rdlen
            if rtype == 1 and rdlen == 4 and rname == name:
                out.append(socket.inet_ntoa(rdata))
    except (struct.error, ValueError):
        pass
    return out


def _local_ipv4s():
    """The interface addresses to send a multicast query from: the
    default first (None), then every IPv4 address this host answers to."""
    out = [None]
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET, socket.SOCK_DGRAM):
            ip = info[4][0]
            if ip not in out and not ip.startswith("127."):
                out.append(ip)
    except OSError:
        pass
    return out


def resolve_mdns(hostname, timeout=2.0, tries=3):
    """The IPv4 address of <hostname>.local, asked of the network
    directly; None when nothing answers."""
    name = hostname if hostname.endswith(".local") else hostname + ".local"
    query = mdns_query(name)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        # On the mDNS port and in the group, so a responder that answers
        # the group rather than the asker is heard too; a port already
        # taken (a resolver daemon) means that daemon would have served
        # the name, so the ephemeral port and unicast replies will do.
        try:
            sock.bind(("", MDNS_PORT))
        except OSError:
            sock.bind(("", 0))
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                            socket.inet_aton(MDNS_GROUP) + socket.inet_aton("0.0.0.0"))
        except OSError:
            pass            # no group membership: unicast replies still arrive
        for _ in range(tries):
            # The query goes out of every local IPv4 interface that will
            # take it (a host with a VPN or a VM adapter as its default
            # route would otherwise never reach the bench network).
            sent = False
            for ifaddr in _local_ipv4s():
                try:
                    if ifaddr is not None:
                        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ifaddr))
                    sock.sendto(query, (MDNS_GROUP, MDNS_PORT))
                    sent = True
                except OSError:
                    continue
            if not sent:
                return None
            deadline = time.monotonic() + timeout
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                sock.settimeout(left)
                try:
                    data, _peer = sock.recvfrom(2048)
                except socket.timeout:
                    break
                except OSError:
                    break
                for ip in mdns_answers(data, name):
                    return ip
        return None
    finally:
        sock.close()


# -------------------------------------------------------------- client

def load_config(path=None):
    """The bench's fixture config, or None when there is none (a bench
    without a fixture: the runner behaves exactly as before)."""
    path = path or FIXTURE_CONFIG
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except OSError:
        return None
    except ValueError as e:
        raise FixtureError("%s: not JSON: %s" % (path, e))
    if not isinstance(cfg, dict) or not cfg.get("key"):
        raise FixtureError("%s: needs at least a key" % path)
    cfg.setdefault("hostname", "forgefixture")
    cfg.setdefault("ip", None)
    cfg.setdefault("port", 80)
    chans = cfg.get("channels")
    if chans is None:
        chans = list(CHANNELS)
    bad = [c for c in chans if c not in CHANNELS]
    if bad:
        raise FixtureError("%s: unknown channel(s) %s" % (path, ", ".join(bad)))
    cfg["channels"] = list(chans)
    cfg["arm_press"] = bool(cfg.get("arm_press", False))
    return cfg


class Fixture:
    """The actuator as the runner sees it: which channels it covers, an
    action per channel, a release, its state. Every request carries a
    fresh nonce and its MAC under the key; the address comes from the
    config's ip or the mDNS lookup, re-resolved when a request fails to
    connect. Times are on the monotonic clock: a step of the board's wall
    clock (its first time sync after a boot) moves no spacing or age."""

    def __init__(self, cfg, timeout=3.0, resolver=resolve_mdns):
        self.hostname = cfg["hostname"]
        self.ip_override = cfg.get("ip") or None
        self.port = int(cfg.get("port") or 80)
        self.key = cfg["key"]
        self.channels = tuple(cfg["channels"])
        self.arm_press = bool(cfg.get("arm_press", False))
        self.timeout = timeout
        self._resolver = resolver
        self._ip = self.ip_override
        self._resolved_at = time.monotonic() if self.ip_override else None
        self._lock = threading.Lock()
        self.last_state = None
        self._pulse_end = 0.0       # when this client's last press ends (its pulse_ms)
        self._nonce_at = 0.0        # when the last request's nonce was in hand: the box issued it before

    # -- address -------------------------------------------------------
    def address(self, refresh=False):
        with self._lock:
            if self.ip_override:
                return self.ip_override
            stale = self._resolved_at is None or time.monotonic() - self._resolved_at > RESOLVE_TTL_S
            if self._ip is None or refresh or stale:
                ip = self._resolver(self.hostname)
                if ip is None:
                    raise FixtureError("%s.local did not answer the mDNS lookup (set ip in the config "
                                       "to skip it)" % self.hostname)
                self._ip = ip
                self._resolved_at = time.monotonic()
            return self._ip

    def where(self):
        return "%s (%s)" % (self.hostname, self._ip or "unresolved")

    # -- requests ------------------------------------------------------
    def _nonce(self, host):
        """A nonce for one request, from the box. Asking for one acts on
        nothing, so any way this fails leaves the request unsent: a
        transport failure comes out as a URLError (the request may be tried
        again at a fresh address), anything else as a FixtureError. The box
        says how long its nonce lives (expires_s): one that lives longer
        than NONCE_LIFE_S is refused, since every wait for a request the
        box may still serve counts that life."""
        req = urllib.request.Request("http://%s/nonce" % host, method="GET", headers={"Host": host})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            raise FixtureError("fixture %s: no nonce (%s)" % (self.where(), e.code))
        except (http.client.HTTPException, socket.timeout, OSError) as e:
            raise urllib.error.URLError(e)
        try:
            answer = json.loads(raw.decode("utf-8", "replace") or "{}")
            nonce, life = answer.get("nonce"), answer.get("expires_s")
        except (ValueError, AttributeError):
            nonce, life = None, None
        if not isinstance(nonce, str) or not nonce:
            raise FixtureError("fixture %s: no nonce in its answer" % self.where())
        if isinstance(life, (int, float)) and life > NONCE_LIFE_S:
            raise FixtureError("fixture %s: its nonce lives %s s, and this client counts on %g s at most"
                               % (self.where(), life, NONCE_LIFE_S))
        return nonce

    def _request(self, method, path, body=None, retry=True, judge=None):
        """One signed request. judge, when given, is asked right before the
        request goes out, after its nonce (a press's policy, judged on the
        machine): an answer other than None withholds it (PressWithheld),
        and so does a judgment that fails. A retry at a fresh address asks
        it again. A loop request that went out and got no whole answer is
        timed as the box can still serve it (_unanswered)."""
        ip = self.address()
        data = json.dumps(body).encode() if body is not None else None
        host = ip if self.port == 80 else "%s:%d" % (ip, self.port)
        try:
            nonce = self._nonce(host)
            self._nonce_at = time.monotonic()
            if judge is not None:
                error = None
                try:
                    why = judge()
                except Exception as e:  # noqa: BLE001 - a judgment that fails withholds the press
                    why = "the press could not be judged (the machine could not be read)"
                    error = "%s: %s" % (type(e).__name__, e)
                if why is not None:
                    raise PressWithheld(why, error)
            req = urllib.request.Request("http://%s%s" % (host, path), data=data, method=method,
                                         headers={"X-Fixture-Nonce": nonce,
                                                  "X-Fixture-Mac": request_mac(self.key, method, path, nonce, data),
                                                  "Host": host, "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    status, raw = resp.status, resp.read()
            except urllib.error.URLError:
                raise               # an error status, or a request that never went out
            except (http.client.HTTPException, OSError):
                # It went out and no whole answer came back. A loop request
                # the box has not served yet (held up behind another
                # connection, or on its way) is taken until its nonce's life
                # is over, whatever a resend of it is answered.
                if path in LOOP_PATHS:
                    _unanswered["loop"] = max(_unanswered["loop"], self._nonce_at + NONCE_LIFE_S)
                raise
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read().decode("utf-8", "replace") or "{}")
            except (ValueError, OSError, http.client.HTTPException):
                body = {}
            return e.code, body if isinstance(body, dict) else {}
        except http.client.HTTPException as e:
            # an answer cut short or malformed: the request went out, and is
            # not sent again (a second press is a second press)
            raise FixtureError("fixture %s: %s %s: the answer could not be read (%s)"
                               % (self.where(), method, path, type(e).__name__), sent=True)
        except (urllib.error.URLError, socket.timeout, OSError) as e:
            # urllib raises a failed connect or send as a URLError; a
            # timeout or a reset after the request went out comes bare, and
            # the box may have acted on the request
            sent = not isinstance(e, urllib.error.URLError)
            # The address may have moved: one fresh lookup, one retry. A
            # request that may have gone out is sent again only where the
            # box takes it the same way twice: a press whose answer was lost
            # is reported, never made again.
            if retry and not self.ip_override and (not sent or (method, path) in TAKES_TWICE):
                try:
                    self.address(refresh=True)
                    return self._request(method, path, body, retry=False, judge=judge)
                except FixtureError as again:
                    # a failure of either still says the first one went out
                    again.sent = again.sent or sent
                    raise
            raise FixtureError("fixture %s: %s %s: %s" % (self.where(), method, path, e), sent=sent)
        # Every answer the runner reads is a JSON object; anything else is
        # the box's failure, never an exception of another kind.
        try:
            body = json.loads(raw.decode("utf-8", "replace") or "{}")
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise FixtureError("fixture %s: %s %s: the answer is not a JSON object" % (self.where(), method, path),
                               sent=True)
        return status, body

    def status(self):
        st, body = self._request("GET", "/")
        if st == 401:
            raise FixtureError("fixture %s refused the key" % self.where())
        if st != 200 or body.get("device") != "forgefixture":
            raise FixtureError("fixture %s: unexpected answer %s %s" % (self.where(), st, body))
        self.last_state = body
        return body

    def covers(self, channel):
        """The runner asks this per action. The button needs the jumper
        in, which the fixture reports; a channel it does not cover falls
        back to the operator."""
        if channel not in self.channels:
            return False
        if channel == "button":
            st = self.last_state
            return bool(st and st.get("button_enabled"))
        return True

    def act(self, channel, state, ms=None, judge=None):
        """One action. judge (a press only): asked right before each request
        that would press, as _request asks it."""
        if channel == "button":
            if state != "press":
                raise FixtureError("the button is only ever pressed")
            st, body = self._press(ms, judge)
        else:
            if state not in ("open", "close"):
                raise FixtureError("%s: unknown state %r" % (channel, state))
            self._loop_clear()
            st, body = self._request("POST", "/" + channel, {"state": state})
        if st != 200:
            raise FixtureError("fixture %s: %s %s -> %s %s" % (self.where(), channel, state, st,
                                                              body.get("error") or body))
        self.last_state = body
        return body

    def _press(self, ms=None, judge=None):
        """One press, spaced from the last one. `ms` is how long the box
        holds it: the box's own 200 ms when it is not said, and the box
        clamps a request into 20 to 500, so no press is ever held. Every
        reader of the button catches the default press (an edge, or a poll
        of 100 ms or less), so no check needs a longer one. The controller
        detects the button's rising edge, so a press must follow a release
        it has seen: the next press waits for the last pulse to end (the
        fixture's own pulse_ms) plus BUTTON_GAP_S. A 409 for a pulse in
        progress (a press this client did not time) is waited out once
        against the fixture's state, then retried. judge is asked right
        before each request that would press, after the spacing, the nonce
        and any fresh lookup (_request). An answer that is neither 200 nor
        one of the refusals the box makes before it presses
        (PRESS_REFUSED) is a press that may have been made (sent). Such a
        press, and one whose answer was lost, is timed as the box can still
        make it: until its nonce's life is over (NONCE_LIFE_S from the
        nonce in hand: a request held up on its way is taken that long),
        and the longest pulse after that. A poll of the state in the 409
        wait presses nothing: one that fails is only a poll that told
        nothing."""
        # after this client's last pulse, and after a press of any client of
        # this process that the box may still make (its answer was lost)
        wait = max(self._pulse_end, _unanswered["press"]) + BUTTON_GAP_S - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        try:
            st, body = self._request("POST", "/button", {"ms": int(ms)} if ms else {}, judge=judge)
            if st == 409 and "in progress" in str(body.get("error", "")):
                deadline = time.monotonic() + PULSE_WAIT_S
                while time.monotonic() < deadline:
                    time.sleep(0.1)
                    try:
                        s, b = self._request("GET", "/")
                    except FixtureError:
                        continue
                    if s == 200 and not b.get("button_pulsing"):
                        break
                time.sleep(BUTTON_GAP_S)
                st, body = self._request("POST", "/button", {"ms": int(ms)} if ms else {}, judge=judge)
            if st != 200 and st not in PRESS_REFUSED:
                raise FixtureError("fixture %s: button press -> %s %s: the press may have been made"
                                   % (self.where(), st, body.get("error") or body), sent=True)
        except PressWithheld:
            raise
        except FixtureError as e:
            if e.sent:
                self._pulse_end = self._nonce_at + NONCE_LIFE_S + PULSE_MAX_S
                _unanswered["press"] = max(_unanswered["press"], self._pulse_end)
            raise
        if st == 200:
            self._pulse_end = time.monotonic() + float(body.get("pulse_ms") or 500) / 1000.0
        return st, body

    def pulse_left(self):
        """Seconds until the last press this client made is let go by the
        box, or until a press whose answer was lost can no longer be made
        and held by it; 0 when there is none."""
        return max(0.0, self._pulse_end - time.monotonic())

    def nonce_age(self):
        """Seconds since the nonce of this client's last request was in
        hand. The box issued it before that, so the request is taken no
        later than NONCE_LIFE_S after."""
        return time.monotonic() - self._nonce_at

    def _loop_clear(self):
        """Before a loop request: wait until no earlier one that got no
        answer can still be served. Served after this one, a close or a
        release that was held up would close a loop this one opened."""
        wait = unanswered_left("loop")
        if wait > 0:
            time.sleep(wait)

    def release(self):
        self._loop_clear()
        st, body = self._request("POST", "/release", {})
        if st != 200:
            raise FixtureError("fixture %s: release -> %s %s" % (self.where(), st, body.get("error") or body))
        self.last_state = body
        return body

    @staticmethod
    def energized(state):
        """The channels a state report shows energized (a loop open, the
        button pressed)."""
        ch = (state or {}).get("channels") or {}
        return [c for c, s in ch.items() if s in ("open", "pressed")]

    def summary(self):
        # The radio signal and the uptime go in the record: this actuator
        # is on wifi, and a run that lost it wants both numbers to say
        # whether the link faded or the box restarted. Without them a
        # drop is only ever "it was gone".
        st = self.last_state or {}
        wifi = st.get("wifi") or {}
        return {"hostname": self.hostname, "ip": self._ip, "channels": list(self.channels),
                "button_enabled": bool(st.get("button_enabled")), "arm_press": self.arm_press,
                "version": st.get("version"), "uptime_s": st.get("uptime_s"),
                "rssi": wifi.get("rssi")}


def probe(log, path=None, resolver=resolve_mdns):
    """The bench's fixture, up and answering, or None: no config means
    no fixture; a config whose fixture does not answer is logged and
    treated the same, so the run goes to the operator."""
    try:
        cfg = load_config(path)
    except FixtureError as e:
        log("fixture: %s - running without it" % e)
        return None
    if cfg is None:
        return None
    fx = Fixture(cfg, resolver=resolver)
    try:
        st = fx.status()
    except FixtureError as e:
        log("fixture: %s - running without it" % e)
        return None
    chans = ", ".join(fx.channels)
    log("fixture up: %s at %s, v%s, covers %s%s" % (
        fx.hostname, fx._ip, st.get("version"), chans,
        "" if st.get("button_enabled") or "button" not in fx.channels
        else " (button disabled: the enable jumper is out)"))
    return fx


__all__ = ["Fixture", "FixtureError", "PressWithheld", "CHANNELS", "FIXTURE_CONFIG", "load_config", "probe",
           "resolve_mdns", "mdns_query", "mdns_answers", "request_mac", "unanswered_left"]
