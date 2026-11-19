# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""Redaction for the release artifact.

The artifact is committed to a public repository, so it carries no
identity of the machine that ran the campaign. One Redactor serves one
export (artifact.redact). It is seeded with the machine's own names, read
where the machine keeps them (machine_identity(), the values forgectrl's
log export registers in logs.c load_known()), and then taught every value
the export holds under an identifying key, before it redacts any record.
It applies the same two layers forgectrl's log export applies
(forgectrl/src/sanitize.c), in the same order, to every value it redacts:

 1. Known values: a value held under a key that names identifying data -
    the serial, the machine id, the hostname, an account name, the
    panel's certificate fingerprint, the wireless credentials, a token -
    is replaced whole, and so is each member of a list held under such a
    key. That value, and each of the machine's own names, is also
    replaced wherever else it appears, in any letter case, in values and
    in the keys of nested objects: a hostname in a log line is the same
    leak as a hostname in its own field. An integer equal to a known
    value goes too. Naming the key is far more reliable than guessing the
    shape of what is under it.
 2. Pattern classes over every remaining string: MAC addresses, IPv4 and
    IPv6 addresses (loopback and unspecified kept), e-mail addresses,
    bearer and basic credentials, JWTs, and long hex blobs. Each distinct
    value in a class keeps a stable number for the life of one export, so
    "<IP-2>" is the same host wherever it appears and the artifact stays
    readable as evidence.

The names that identify no machine (the image's and the build's default
hostnames, as in logs.c, the bench actuator's default name and this
tool's own) are never known values.

What the gate reads and recomputes is kept by its place in the artifact,
which the exporter names (artifact._KEEP_RECORD), never by the name of a
key: a test's own evidence under a key named like an integrity value is
redacted like any other value.

The limits: a known value is found only whole, within one string, and in
the forms registered for it (any letter case; a hardware address with
colons, dashes, percent-encoded colons or none). A value split across two
log lines, or written in another encoding, stays.

Over-redaction is accepted where the alternative is a leak.
"""
import base64
import hashlib
import ipaddress
import os
import re
import socket
import struct

from . import fixture as _fixture

# Layer 1: the value under one of these keys is replaced whole.
REDACT_KEYS = frozenset((
    "gf_serial", "machine_id", "serial", "serial_number", "sheet_id",
    "hostname", "hostname_file", "host",
    "record_accounts", "account_before", "temp_account",
    "tls_fingerprint",
    "ssid", "psk", "passphrase", "password", "passwd",
    "token", "panel_token", "secret", "api_key", "key",
    "mac", "macaddr", "mac_suffix", "wlan0", "eth0",
    "gf_username", "gf_password", "username", "user", "email",
))

# Never a known value: the image's hostname before forgefirm-hostname has
# read the MAC address, the two build-time defaults, the bench actuator's
# default name, and this tool's own name (an account the campaign itself
# makes can carry it).
GENERIC_NAMES = frozenset(("forgefirm", "glowforge", "localhost", "forgefixture", "forgetest"))

_KEEP_IP = frozenset(("0.0.0.0", "255.255.255.255"))
# A tag: what a redaction leaves in place of a value.
_TAG = re.compile(r"<([A-Z][A-Z0-9]*)-(\d+)>")

# IPv4 comes before IPv6, so an IPv4 address written behind an IPv6 prefix
# (::ffff:192.0.2.1) is taken whole by its own class.
_CLASSES = (
    ("SECRET", re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("MAC", re.compile(r"\b(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b")),
    ("IP", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
    # Three or more colons, or a "::" run: two colons is a clock time, and
    # tagging every timestamp would make the evidence unreadable. A "::"
    # run starts at no letter, digit, colon or dot, so a leading "::" is
    # one and C++'s "std::" is not.
    ("IP6", re.compile(
        r"\b(?:[0-9A-Fa-f]{1,4}:){3,7}[0-9A-Fa-f]{1,4}\b"
        r"|(?<![0-9A-Za-z:.])[0-9A-Fa-f]{0,4}(?::[0-9A-Fa-f]{1,4})*::(?:[0-9A-Fa-f]{1,4}(?::[0-9A-Fa-f]{1,4})*)?")),
    ("HEX", re.compile(r"\b[0-9A-Fa-f]{32,}\b")),
)


def _ip6_kept(v):
    """The unspecified and the loopback address, however written ("0::" is
    the unspecified one, as in a cgroup line "0::/..."), and a run of
    one- and two-digit decimal numbers (a time with frames, a ratio)."""
    if re.fullmatch(r"\d{1,2}(?::\d{1,2})+", v):
        return True
    try:
        a = ipaddress.IPv6Address(v)
    except ValueError:
        return False
    return a.is_unspecified or a.is_loopback


def _kept(path, keep):
    """Whether `path` (the keys and list indexes from the redacted value's
    root) is one of the places in `keep`, where None stands for any list
    index."""
    return any(len(p) == len(path) and all(a is None or a == b for a, b in zip(p, path)) for p in keep)


def _on_kept(path, keep):
    """Whether `path` leads to one of the places in `keep` or is one."""
    return any(len(p) >= len(path) and all(a is None or a == b for a, b in zip(p, path)) for p in keep)


class Redactor:
    """Stable per-export numbering, one instance per artifact.

    identity: (kind, value, word) for each of the machine's own names
    (machine_identity()). A `word` value is replaced only where it stands
    alone, not inside a longer run of letters and digits: the short hex
    suffix of an interface address, or a short account name, would
    otherwise eat every hex blob, number and word that happens to contain
    it.

    A tag already in what it redacts (forgectrl's log export writes the
    same "<IP-2>" form) is redacted already: it is kept, and a new tag of
    its class never takes its number, so one tag names one value."""

    # A value learned from a field shorter than this is too generic to
    # replace blind.
    MIN_KNOWN = 4
    # One of the machine's own names is known from forgectrl's floor
    # (sanitize.c: 3 characters, 5 when it is all digits), and a whole word
    # (an account name) from 2.
    MIN_SEED = 3
    MIN_SEED_DIGITS = 5
    MIN_SEED_WORD = 2

    def __init__(self, identity=()):
        self._n = {}
        self._known = {}        # a value, in lower case -> the kind it is known as
        self._words = set()     # the known values replaced only as a whole word
        self._taken = {}        # a class -> the numbers its tags already have
        self._rx = {}
        self._order = None
        for kind, value, word in identity:
            self.know(kind, value, word, seed=True)

    def know(self, kind, value, word=False, seed=False):
        """Replace `value` wherever it appears, in any letter case."""
        if not isinstance(value, str) or _TAG.fullmatch(value):
            return
        if not seed:
            floor = self.MIN_KNOWN
        elif word:
            floor = self.MIN_SEED_WORD
        else:
            floor = self.MIN_SEED_DIGITS if value.isdigit() else self.MIN_SEED
        if len(value) < floor:
            return
        k = value.lower()
        if k in GENERIC_NAMES:
            return
        if k not in self._known:
            self._known[k] = kind
            self._order = None
            if word:
                self._words.add(k)
        elif not word and k in self._words:
            # known as a bare word and now from a field: anywhere it appears
            self._words.discard(k)
            self._rx.pop(k, None)

    def _reserve(self, s):
        for m in _TAG.finditer(s):
            self._taken.setdefault(m.group(1), set()).add(int(m.group(2)))

    def learn(self, obj, keep=(), _path=(), _key=None):
        """Collect the values held under the layer-1 keys (a list's
        members under its key), so they are replaced wherever else they
        appear in the export, and the numbers of the tags already there.
        The places in `keep` hold no identity and are skipped."""
        if _path and _kept(_path, keep):
            return
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(k, str):
                    self._reserve(k)
                self.learn(v, keep, _path + (k,), k)
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                self.learn(v, keep, _path + (i,), _key)
        elif isinstance(obj, str):
            self._reserve(obj)
            if _key in REDACT_KEYS:
                self.know(_key, obj)

    def _tag(self, cls, value):
        seen = self._n.setdefault(cls, {})
        if value not in seen:
            n = max(seen.values(), default=0) + 1
            taken = self._taken.get(cls, ())
            while n in taken:
                n += 1
            seen[value] = n
        return "<%s-%d>" % (cls, seen[value])

    def _known_tag(self, k):
        return self._tag("REDACTED", "%s=%s" % (self._known[k], k))

    def _pattern(self, k):
        rx = self._rx.get(k)
        if rx is None:
            e = re.escape(k)
            if k in self._words:
                e = r"(?<![0-9A-Za-z])%s(?![0-9A-Za-z])" % e
            rx = self._rx[k] = re.compile(e, re.IGNORECASE)
        return rx

    def text(self, s):
        if self._order is None:
            self._order = sorted(self._known, key=len, reverse=True)
        for k in self._order:
            s = self._pattern(k).sub(lambda m, k=k: self._known_tag(k), s)
        for cls, rx in _CLASSES:
            def sub(m, cls=cls):
                v = m.group(0)
                if cls == "IP" and (v.startswith("127.") or v in _KEEP_IP):
                    return v
                if cls == "IP6" and _ip6_kept(v):
                    return v
                return self._tag(cls, v)
            s = rx.sub(sub, s)
        return s

    def value(self, v, keep=(), _path=(), _key=None):
        """The value redacted; the places in `keep` as they are. The keys
        of a nested object are redacted as text too, except on the way to
        a kept place; the keys at the root are the exporter's own."""
        if _path and _kept(_path, keep):
            return v
        if _key in REDACT_KEYS and not isinstance(v, (dict, list)):
            if v is None or (isinstance(v, str) and _TAG.fullmatch(v)):
                return v
            if isinstance(v, str) and v.lower() in self._known:
                return self._known_tag(v.lower())
            return self._tag("REDACTED", "%s=%s" % (_key, v))
        if isinstance(v, dict):
            out = {}
            for k, x in v.items():
                nk = k
                if _path and isinstance(k, str) and not _on_kept(_path + (k,), keep):
                    nk = self.text(k)
                    n = 1
                    while nk in out:
                        n += 1
                        nk = "%s (%d)" % (self.text(k), n)
                out[nk] = self.value(x, keep, _path + (k,), k)
            return out
        if isinstance(v, list):
            return [self.value(x, keep, _path + (i,), _key) for i, x in enumerate(v)]
        if isinstance(v, str):
            return self.text(v)
        if isinstance(v, int) and not isinstance(v, bool) and str(v) in self._known:
            return self._known_tag(str(v))
        return v


# -- the machine's own names ----------------------------------------------

# Where machine_identity() reads: the machine's filesystem.
MACHINE_ROOT = "/"
# The OCOTP fuses as forgectrl reads them (forgectrl/src/main.c): the
# nvmem view, one 32-bit little-endian word per fuse, else the legacy
# fsl_otp files in hex text. The serial is HW_OCOTP_MAC0 (bank 4 word 2);
# the cloud password is HW_OCOTP_SRK0 to SRK7 (bank 3 words 0 to 7).
_NVMEM = "/sys/bus/nvmem/devices/imx-ocotp0/nvmem"
_MAC0_OFFSET = (4 * 8 + 2) * 4
_SRK_OFFSET = (3 * 8 + 0) * 4
_LEGACY = "/sys/fsl_otp/HW_OCOTP_%s"
# The base-23 alphabet of the machine id (the factory hostname).
_ID_ALPHABET = "BCDFGHJKMQRTVWXY2346789"
# forgectrl's data directory and settings file, as its paths.h and
# settings.c name them.
_DATA_DIR = "/data/forgefirm"
_SETTINGS = "/data/forgefirm/forgefirm.conf"
# The settings that override an identity or name a host (logs.c).
_SETTINGS_KNOWN = ("gf_serial", "gf_password", "syslog_server")
# The wireless configuration's identifying lines (logs.c add_wpa_known()).
_WPA_FILES = ("/data/etc/wpa_supplicant.conf", "/etc/wpa_supplicant.conf")
_WPA_KEYS = (("ssid=", "ssid"), ("psk=", "psk"), ("identity=", "wifi_identity"),
             ("password=", "wifi_password"), ("bssid=", "bssid"))


def _machine_id(serial):
    """The fuse serial -> the machine id, XXX-YYY, as forgectrl derives it."""
    enc = ""
    while serial > 0 and len(enc) < 6:
        enc = _ID_ALPHABET[serial % 23] + enc
        serial //= 23
    return "%s-%s" % (enc[:3], enc[3:]) if len(enc) > 3 else enc


def _fuse_words(path, offset, count):
    """`count` fuse words from the nvmem view, or None."""
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read(count * 4)
    except OSError:
        return None
    return list(struct.unpack("<%dI" % count, data)) if len(data) == count * 4 else None


def _legacy_word(path):
    try:
        with open(path, encoding="ascii") as f:
            return int(f.read().strip(), 16)
    except (OSError, ValueError):
        return None


def _lines(path):
    """The lines of a text file, or none. A byte that is not UTF-8 stays
    as it is (surrogateescape) and costs no other line."""
    try:
        with open(path, encoding="utf-8", errors="surrogateescape") as f:
            return f.read().splitlines()
    except OSError:
        return []


def _cert_fingerprint(path):
    """The SHA-256 fingerprint of the PEM certificate at `path`, in the
    form the panel shows (colon-separated upper-case hex), or None."""
    try:
        with open(path, encoding="ascii") as f:
            pem = f.read()
        body = pem.split("-----BEGIN CERTIFICATE-----", 1)[1].split("-----END CERTIFICATE-----", 1)[0]
        der = base64.b64decode("".join(body.split()), validate=True)
    except (OSError, IndexError, ValueError):
        return None
    return ":".join("%02X" % b for b in hashlib.sha256(der).digest())


def machine_identity(root=None):
    """The names of the machine this runs on, read where the machine keeps
    them: [(kind, value, word)] for Redactor. They are what forgectrl's log
    export registers (logs.c load_known()) and more:

    - its hostname: the live name and /etc/hostname;
    - every interface's hardware address, with colons, dashes,
      percent-encoded colons or none, and, as a whole word only, its last
      four hex digits (the suffix the hostname is made from);
    - the bench actuator's hostname (fixture.py);
    - in forgectrl's data directory: the panel's certificate fingerprint,
      every account name in the users record (as a whole word: a name can
      be as short as 2 characters), the panel token and the camera key;
    - forgectrl's settings that override an identity or name a host;
    - the wireless configuration's SSID, passphrase, identity, password
      and BSSID;
    - the three fuse identities: the serial (in decimal and in hex), the
      machine id it gives, and the cloud password.

    A name that cannot be read is left out. Paths are the machine's,
    under `root` (MACHINE_ROOT when None)."""
    root = root or MACHINE_ROOT

    def at(path):
        return os.path.join(root, path.lstrip("/"))

    out = []

    def add(kind, value, word=False):
        if value:
            out.append((kind, value, word))

    try:
        add("hostname", socket.gethostname())
    except OSError:
        pass
    for line in _lines(at("/etc/hostname"))[:1]:
        add("hostname_file", line.strip())
    try:
        nets = sorted(os.listdir(at("/sys/class/net")))
    except OSError:
        nets = []
    for dev in nets:
        lines = _lines(at("/sys/class/net/%s/address" % dev))
        addr = lines[0].strip().lower() if lines else ""
        bare = addr.replace(":", "")
        if not re.fullmatch(r"[0-9a-f]{12}", bare) or bare == "0" * 12:
            continue
        add("mac", addr)
        add("mac", bare)
        add("mac", addr.replace(":", "-"))
        add("mac", addr.replace(":", "%3a"))
        add("mac_suffix", bare[-4:], word=True)
    try:
        cfg = _fixture.load_config(at(_fixture.FIXTURE_CONFIG))
    except _fixture.FixtureError:
        cfg = None
    if cfg:
        add("fixture", cfg.get("hostname"))
    data = os.environ.get("FORGECTRL_DATA_DIR") or _DATA_DIR
    fp = _cert_fingerprint(at(data + "/tls/cert.pem"))
    if fp:
        add("tls_fingerprint", fp)
        add("tls_fingerprint", fp.replace(":", ""))
    for line in _lines(at(data + "/users")):
        if line.strip() and not line.startswith("#"):
            add("account", line.split(":")[0].strip(), word=True)
    for name, kind in (("panel.token", "panel_token"), ("camera.key", "camera_key")):
        for line in _lines(at(data + "/" + name))[:1]:
            add(kind, line.strip())
    for line in _lines(at(os.environ.get("FORGECTRL_CONF") or _SETTINGS)):
        k, eq, v = line.strip().partition("=")
        if eq and not k.startswith("#") and k.strip() in _SETTINGS_KNOWN:
            add(k.strip(), v.strip())
    for path in _WPA_FILES:
        for line in _lines(at(path)):
            s = line.strip()
            for prefix, kind in _WPA_KEYS:
                if s.startswith(prefix):
                    v = s[len(prefix):]
                    if len(v) >= 2 and v[0] == v[-1] == '"':
                        v = v[1:-1]
                    add(kind, v)
    serial = (_fuse_words(at(_NVMEM), _MAC0_OFFSET, 1) or [_legacy_word(at(_LEGACY % "MAC0"))])[0]
    if serial:
        add("serial", str(serial))
        add("serial", "%08x" % serial)
        add("machine_id", _machine_id(serial))
    srk = _fuse_words(at(_NVMEM), _SRK_OFFSET, 8) or [_legacy_word(at(_LEGACY % ("SRK%d" % i))) for i in range(8)]
    if None not in srk and any(srk):
        add("password", "".join("%08x" % w for w in srk))
    return out
