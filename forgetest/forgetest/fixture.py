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

The fixture does not read the machine: every action the runner asks of
it is verified through the machine's own readings afterward, exactly as
an operator's hand is (Context.act). Contract: fixture/README.md.
"""
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


class FixtureError(Exception):
    pass


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
            deadline = time.time() + timeout
            while True:
                left = deadline - time.time()
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
    action per channel, a release, its state. Every request carries the
    key; the address comes from the config's ip or the mDNS lookup,
    re-resolved when a request fails to connect."""

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
        self._resolved_at = time.time() if self.ip_override else 0.0
        self._lock = threading.Lock()
        self.last_state = None
        self._pulse_end = 0.0       # when this client's last press ends (its pulse_ms)

    # -- address -------------------------------------------------------
    def address(self, refresh=False):
        with self._lock:
            if self.ip_override:
                return self.ip_override
            stale = time.time() - self._resolved_at > RESOLVE_TTL_S
            if self._ip is None or refresh or stale:
                ip = self._resolver(self.hostname)
                if ip is None:
                    raise FixtureError("%s.local did not answer the mDNS lookup (set ip in the config "
                                       "to skip it)" % self.hostname)
                self._ip = ip
                self._resolved_at = time.time()
            return self._ip

    def where(self):
        return "%s (%s)" % (self.hostname, self._ip or "unresolved")

    # -- requests ------------------------------------------------------
    def _request(self, method, path, body=None, retry=True):
        ip = self.address()
        data = json.dumps(body).encode() if body is not None else None
        host = ip if self.port == 80 else "%s:%d" % (ip, self.port)
        req = urllib.request.Request("http://%s%s" % (host, path), data=data, method=method,
                                     headers={"X-Fixture-Key": self.key, "Host": host,
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8", "replace") or "{}")
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read().decode("utf-8", "replace") or "{}")
            except ValueError:
                body = {}
            return e.code, body
        except (urllib.error.URLError, socket.timeout, OSError) as e:
            if retry and not self.ip_override:
                # the address may have moved: one fresh lookup, one retry
                self.address(refresh=True)
                return self._request(method, path, body, retry=False)
            raise FixtureError("fixture %s: %s %s: %s" % (self.where(), method, path, e))

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

    def act(self, channel, state):
        if channel == "button":
            if state != "press":
                raise FixtureError("the button is only ever pressed")
            st, body = self._press()
        else:
            if state not in ("open", "close"):
                raise FixtureError("%s: unknown state %r" % (channel, state))
            st, body = self._request("POST", "/" + channel, {"state": state})
        if st != 200:
            raise FixtureError("fixture %s: %s %s -> %s %s" % (self.where(), channel, state, st,
                                                              body.get("error") or body))
        self.last_state = body
        return body

    def _press(self):
        """One press, spaced from the last one. The controller detects
        the button's rising edge, so a press must follow a release it has
        seen: the next press waits for the last pulse to end (the
        fixture's own pulse_ms) plus BUTTON_GAP_S. A 409 for a pulse in
        progress (a press this client did not time) is waited out once
        against the fixture's state, then retried."""
        wait = self._pulse_end + BUTTON_GAP_S - time.time()
        if wait > 0:
            time.sleep(wait)
        st, body = self._request("POST", "/button", {})
        if st == 409 and "in progress" in str(body.get("error", "")):
            deadline = time.time() + PULSE_WAIT_S
            while time.time() < deadline:
                time.sleep(0.1)
                s, b = self._request("GET", "/")
                if s == 200 and not b.get("button_pulsing"):
                    break
            time.sleep(BUTTON_GAP_S)
            st, body = self._request("POST", "/button", {})
        if st == 200:
            self._pulse_end = time.time() + float(body.get("pulse_ms") or 500) / 1000.0
        return st, body

    def release(self):
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


__all__ = ["Fixture", "FixtureError", "CHANNELS", "FIXTURE_CONFIG", "load_config", "probe",
           "resolve_mdns", "mdns_query", "mdns_answers"]
