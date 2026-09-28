# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""The cipher forgectrl's HTTPS listener chooses, and where its records
are sealed.

The listener puts ChaCha20-Poly1305 first, whatever the client prefers,
and the image seals the records in the kernel: GnuTLS hands each
connection's keys to the socket after the handshake. ChaCha20-Poly1305
runs in the kernel's NEON code; AES-GCM, for a client without ChaCha20,
runs its AES on the CAAM crypto engine and its GHASH in NEON. This test
reads all of it from the outside: the cipher the listener picks for a
client that prefers AES-GCM, the kernel's TLS counters, the crypto
engine's interrupt, and the drivers the kernel resolved, with each
connection carrying the same page, which must arrive identical to the
plain-HTTP copy.

Its own module: a test's fingerprint holds the shared text of the module
it lives in, so a test added to forgectrl.py would move the fingerprint
of every test there.
"""

import os
import shutil
import subprocess
import tempfile
import urllib.request

from ..catalog import test

OPENSSL = "/usr/bin/openssl"
ENGINE_IRQ = "2101000.jr"           # the CAAM's first job ring
RECORD = 16384                      # the largest TLS record's plaintext
PAGE = "/"                          # the panel page: large, and the same bytes every time
CHACHA_DRIVER = "rfc7539(chacha20-neon,poly1305-neon)"
# A desktop browser's offer: AES-GCM first, because its CPU has AES instructions.
DESKTOP_13 = "TLS_AES_128_GCM_SHA256:TLS_AES_256_GCM_SHA384:TLS_CHACHA20_POLY1305_SHA256"
DESKTOP_12 = "ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-ECDSA-CHACHA20-POLY1305"


def _tls_stat():
    """The kernel's TLS counters, by name."""
    out = {}
    with open("/proc/net/tls_stat") as f:
        for line in f:
            p = line.split()
            if len(p) == 2 and p[1].isdigit():
                out[p[0]] = int(p[1])
    return out


def _engine_irqs():
    with open("/proc/interrupts") as f:
        for line in f:
            p = line.split()
            if p and p[-1] == ENGINE_IRQ and len(p) > 1 and p[1].isdigit():
                return int(p[1])
    return None


def _driver(name):
    """The driver answering an algorithm name: the entry of that name with
    the highest priority in /proc/crypto."""
    best, drv, entry = -1, None, {}
    with open("/proc/crypto") as f:
        lines = f.read().splitlines() + [""]
    for line in lines:
        if not line.strip():
            if entry.get("name") == name and int(entry.get("priority", -1)) > best:
                best, drv = int(entry["priority"]), entry.get("driver")
            entry = {}
            continue
        k, _, v = line.partition(":")
        entry[k.strip()] = v.strip()
    return drv


def _fetch(version, offer, work):
    """GET the page over HTTPS on loopback with the given offer. Returns
    the suite, the status line, the body, the kernel counters' change,
    and the crypto engine's interrupts during the connection."""
    req = os.path.join(work, "req")
    with open(req, "wb") as f:
        f.write(b"GET " + PAGE.encode() + b" HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
    opt = ["-tls1_3", "-ciphersuites", offer] if version == "1.3" else ["-tls1_2", "-cipher", offer]
    s0, i0 = _tls_stat(), _engine_irqs()
    with open(req, "rb") as stdin:
        r = subprocess.run([OPENSSL, "s_client", "-connect", "127.0.0.1:443", "-brief", "-ign_eof"] + opt,
                           stdin=stdin, capture_output=True, timeout=60)
    s1, i1 = _tls_stat(), _engine_irqs()
    info = {}
    for line in r.stderr.decode("utf-8", "replace").splitlines():
        k, _, v = line.partition(":")
        info[k.strip()] = v.strip()
    head, _, body = r.stdout.partition(b"\r\n\r\n")
    delta = {k: s1.get(k, 0) - s0.get(k, 0) for k in ("TlsTxSw", "TlsRxSw", "TlsDecryptError")}
    return (info.get("Ciphersuite"), head.split(b"\r\n")[0].decode("latin-1"), body, delta,
            (i1 or 0) - (i0 or 0))


@test("forgectrl.tls-records", title="HTTPS: ChaCha20 chosen, records sealed in the kernel",
      subsystem="forgectrl", kind="auto", est_min=1,
      covers=[("forgectrl", "src/tls.*"), ("forgectrl", "src/main.c")],
      description="forgectrl's HTTPS listener chooses the cipher, ChaCha20-Poly1305 first, and the "
                  "image seals the records in the kernel: GnuTLS hands each connection's keys to the "
                  "socket after the handshake (/etc/gnutls/config turns it on). A client that lists "
                  "AES-GCM first, as a desktop browser does, gets ChaCha20-Poly1305 over TLS 1.3 and "
                  "TLS 1.2, and the kernel takes both directions' keys; the kernel's rfc7539 is the "
                  "NEON ChaCha20 and Poly1305. A client offering AES-GCM alone gets it, the kernel "
                  "takes its keys, and the crypto engine's interrupt counts the records: the AES runs "
                  "on the CAAM (gcm(aes) is its ctr(aes) with the NEON GHASH), which writes each "
                  "record by DMA while the CPU fills the next, so those connections run three times "
                  "over each protocol and every copy must be exact. A TLS 1.2 client "
                  "offering a CBC suite alone still connects and stays in GnuTLS, which is the control "
                  "for the kernel's counters; the ChaCha20 runs are the control for the engine's "
                  "interrupt. Every copy of the panel page must be the plain-HTTP page byte for byte, "
                  "and the kernel must count no decrypt error. Nothing on the machine changes.")
def tls_records(ctx):
    ev = ctx.evidence

    conf = ""
    if os.path.exists("/etc/gnutls/config"):
        with open("/etc/gnutls/config") as f:
            conf = f.read()
    ctx.check(any(line.split() == ["ktls", "=", "true"] for line in conf.splitlines()),
              "/etc/gnutls/config does not turn kernel TLS on: %r", conf)
    with open("/proc/sys/net/ipv4/tcp_available_ulp") as f:
        ulp = f.read().split()
    ev["tcp_available_ulp"] = ulp
    ctx.check("tls" in ulp, "the kernel has no TLS layer (tcp_available_ulp %s)", ulp)
    ctx.check(_engine_irqs() is not None, "/proc/interrupts has no %s", ENGINE_IRQ)

    with urllib.request.urlopen("http://127.0.0.1" + PAGE, timeout=30) as r:
        plain = r.read()
    ev["page_bytes"] = len(plain)
    ctx.check(len(plain) > 8 * RECORD, "the page is %d bytes, too small to span records", len(plain))
    records = len(plain) // RECORD

    work = tempfile.mkdtemp(prefix="forgetest-tls-")
    try:
        runs = {}
        # The AES-GCM connections run three times each: their records are written by the crypto
        # engine's DMA while the CPU fills the next one, which is where a record's last bytes were
        # once lost.
        cases = [("desktop-1.3", "1.3", DESKTOP_13, "TLS_CHACHA20_POLY1305_SHA256", True),
                 ("desktop-1.2", "1.2", DESKTOP_12, "ECDHE-ECDSA-CHACHA20-POLY1305", True)]
        for n in (1, 2, 3):
            cases += [("aes-1.3#%d" % n, "1.3", "TLS_AES_128_GCM_SHA256", "TLS_AES_128_GCM_SHA256", True),
                      ("aes-1.2#%d" % n, "1.2", "ECDHE-ECDSA-AES128-GCM-SHA256", "ECDHE-ECDSA-AES128-GCM-SHA256",
                       True)]
        cases.append(("cbc-1.2", "1.2", "ECDHE-ECDSA-AES128-SHA", "ECDHE-ECDSA-AES128-SHA", False))
        for label, version, offer, want, kernel in cases:
            suite, status, body, delta, irqs = _fetch(version, offer, work)
            run = {"offer": offer, "suite": suite, "status": status, "bytes": len(body),
                   "same_as_http": body == plain, "tls_stat": delta, "engine_irqs": irqs}
            runs[label] = run
            ctx.log("%s: offer %s -> %s, %s, %d bytes, same %s, tls_stat %s, %s +%d",
                    label, offer, suite, status, len(body), body == plain, delta, ENGINE_IRQ, irqs)
            ctx.check(suite == want, "%s: offered %s, the listener chose %s, not %s", label, offer, suite, want)
            ctx.check(status.endswith("200 OK") and body == plain,
                      "%s: the page arrived %s, %d bytes, not the plain-HTTP %d", label, status, len(body),
                      len(plain))
            ctx.check(delta["TlsDecryptError"] == 0, "%s: the kernel counted %d decrypt errors", label,
                      delta["TlsDecryptError"])
            if kernel:
                ctx.check(delta["TlsTxSw"] == 1 and delta["TlsRxSw"] == 1,
                          "%s: the kernel did not take both directions' keys (tls_stat %s)", label, delta)
            else:
                ctx.check(delta["TlsTxSw"] == 0 and delta["TlsRxSw"] == 0,
                          "%s: the kernel took keys for a cipher it does not seal (tls_stat %s)", label, delta)
        ev["runs"] = runs

        # The AES of AES-GCM runs on the crypto engine: its interrupt counts
        # the records. ChaCha20 never touches the engine, so those runs are
        # the control.
        for label in ("aes-1.3#1", "aes-1.2#1"):
            ctx.check(runs[label]["engine_irqs"] >= records // 4,
                      "%s: %d records crossed and the crypto engine interrupted %d times", label, records,
                      runs[label]["engine_irqs"])
        for label in ("desktop-1.3", "desktop-1.2"):
            ctx.check(runs[label]["engine_irqs"] < max(2, records // 8),
                      "%s: the crypto engine interrupted %d times for ChaCha20 records", label,
                      runs[label]["engine_irqs"])
    finally:
        shutil.rmtree(work, ignore_errors=True)

    # The drivers the kernel resolved for the two record ciphers (both
    # exist now: the connections above instantiated them).
    chacha, gcm = _driver("rfc7539(chacha20,poly1305)"), _driver("gcm(aes)")
    ev["drivers"] = {"rfc7539(chacha20,poly1305)": chacha, "gcm(aes)": gcm}
    ctx.log("rfc7539(chacha20,poly1305) is %s; gcm(aes) is %s", chacha, gcm)
    ctx.check(chacha == CHACHA_DRIVER, "the kernel's ChaCha20-Poly1305 is %s, not %s", chacha, CHACHA_DRIVER)
    ctx.check(gcm and "ctr-aes-caam" in gcm and "ghash-ce" in gcm,
              "the kernel's gcm(aes) is %s: not the crypto engine's AES with the NEON GHASH", gcm)
