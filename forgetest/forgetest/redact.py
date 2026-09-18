# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""Redaction for the release artifact.

The artifact is committed to a public repository, so it carries no
identity of the machine that ran the campaign. This applies the same two
layers forgectrl's log export applies (forgectrl/src/sanitize.c), in the
same order, to every value the exporter writes:

 1. Known keys: a value held under a key that names identifying data -
    the serial, the machine id, the hostname, the wireless credentials,
    a token - is replaced whole. Naming the key is far more reliable
    than guessing the shape of what is under it.
 2. Pattern classes over every remaining string: MAC addresses, IPv4 and
    IPv6 addresses (loopback and unspecified kept), e-mail addresses,
    bearer and basic credentials, JWTs, and long hex blobs. Each distinct
    value in a class keeps a stable number for the life of one export, so
    "<IP-2>" is the same host wherever it appears and the artifact stays
    readable as evidence.

Integrity values are never touched: the self-hash, the manifest and
catalog hashes, each test's source hash and fingerprint. Those are what
the gate recomputes, and a long hex blob is exactly what they look like.

Over-redaction is accepted where the alternative is a leak.
"""
import re

# Layer 1: the value under one of these keys is replaced whole.
REDACT_KEYS = frozenset((
    "gf_serial", "machine_id", "serial", "serial_number",
    "hostname", "hostname_file", "host",
    "ssid", "psk", "passphrase", "password", "passwd",
    "token", "panel_token", "secret", "api_key", "key",
    "mac", "macaddr", "mac_suffix", "wlan0", "eth0",
    "gf_username", "gf_password", "username", "user", "email",
))

# Never touched, by key: the gate recomputes these and they look like
# exactly what layer 2 hunts for.
KEEP_KEYS = frozenset((
    "sha256", "manifest_sha", "identity_sha", "catalog_hash",
    "source_sha", "fingerprint", "content_sha", "srcrev",
))

_KEEP_IP = frozenset(("0.0.0.0", "255.255.255.255"))

_CLASSES = (
    ("SECRET", re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("MAC", re.compile(r"\b(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b")),
    # Three or more colons, or a "::" run: two colons is a clock time, and
    # tagging every timestamp would make the evidence unreadable.
    ("IP6", re.compile(
        r"(?:[0-9A-Fa-f]{1,4}:){3,7}[0-9A-Fa-f]{1,4}"
        r"|[0-9A-Fa-f]{0,4}(?::[0-9A-Fa-f]{1,4})*::(?:[0-9A-Fa-f]{1,4}(?::[0-9A-Fa-f]{1,4})*)?")),
    ("IP", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
    ("HEX", re.compile(r"\b[0-9A-Fa-f]{32,}\b")),
)


class Redactor:
    """Stable per-export numbering, one instance per artifact."""

    # A known value shorter than this is too generic to replace blind.
    MIN_KNOWN = 4

    def __init__(self):
        self._n = {}
        self._known = {}

    def learn(self, obj, key=None):
        """Collect the values held under the layer-1 keys, so they are
        replaced wherever else they appear - a hostname in a log line is
        the same leak as a hostname in its own field."""
        if key is not None and key in KEEP_KEYS:
            return
        if isinstance(obj, dict):
            for k, v in obj.items():
                self.learn(v, k)
        elif isinstance(obj, list):
            for v in obj:
                self.learn(v)
        elif key in REDACT_KEYS and isinstance(obj, str) and len(obj) >= self.MIN_KNOWN:
            if obj not in self._known:
                self._known[obj] = self._tag("REDACTED", "%s=%s" % (key, obj))

    def _tag(self, cls, value):
        seen = self._n.setdefault(cls, {})
        if value not in seen:
            seen[value] = len(seen) + 1
        return "<%s-%d>" % (cls, seen[value])

    def text(self, s):
        for known in sorted(self._known, key=len, reverse=True):
            if known in s:
                s = s.replace(known, self._known[known])
        for cls, rx in _CLASSES:
            def sub(m, cls=cls):
                v = m.group(0)
                if cls == "IP" and (v.startswith("127.") or v in _KEEP_IP):
                    return v
                if cls == "IP6" and v in ("::", "::1"):
                    return v
                return self._tag(cls, v)
            s = rx.sub(sub, s)
        return s

    def value(self, v, key=None):
        if key is not None and key in KEEP_KEYS:
            return v
        if key is not None and key in REDACT_KEYS and not isinstance(v, (dict, list)):
            if v is None:
                return None
            if isinstance(v, str) and v in self._known:
                return self._known[v]
            return self._tag("REDACTED", "%s=%s" % (key, v))
        if isinstance(v, dict):
            return {k: self.value(x, k) for k, x in v.items()}
        if isinstance(v, list):
            return [self.value(x) for x in v]
        if isinstance(v, str):
            return self.text(v)
        return v


def scrub(obj):
    """Redact a value tree with one export's numbering."""
    r = Redactor()
    r.learn(obj)
    return r.value(obj)
