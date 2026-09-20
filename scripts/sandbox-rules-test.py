#!/usr/bin/env python3
# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""Host test of the extension sandbox's deny rules.

Loads meta-forgefirm's ffx.nft into a network namespace of its own and sends
real traffic at it from real uids: an extension account is refused on
loopback, IPv4 and IPv6, TCP at once and UDP with EPERM; the uids on either
side of the pool, and root, are not touched; an allowlist chain lets one uid
reach one destination and nothing else; and loading the file again takes the
allowlist away. Needs root, nft, and a kernel with nf_tables; exits 77 when
one is missing, 0 on a pass, 1 on a failure.

    sudo python3 scripts/sandbox-rules-test.py [--nft /path/to/nft] [--rules FILE]
"""
import argparse
import errno
import fcntl
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
RULES = os.path.join(HERE, "..", "meta-forgefirm", "recipes-forgefirm", "forgefirm-sandbox", "files", "ffx.nft")
POOL_FIRST, POOL_LAST = 800, 831
SKIP = 77

failures = []


def check(ok, what, *args):
    text = what % args if args else what
    print("  %s  %s" % ("ok  " if ok else "FAIL", text), flush=True)
    if not ok:
        failures.append(text)


def lo_up():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        ifr = struct.pack("16sH14x", b"lo", 0)
        flags = struct.unpack("16sH14x", fcntl.ioctl(s, 0x8913, ifr))[1]        # SIOCGIFFLAGS
        fcntl.ioctl(s, 0x8914, struct.pack("16sH14x", b"lo", flags | 1))        # SIOCSIFFLAGS, IFF_UP
    finally:
        s.close()


def tcp_listener(family, addr):
    s = socket.socket(family, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((addr, 0))
    s.listen(128)       # nobody accepts: the kernel completes each connect into the backlog
    return s, s.getsockname()[1]


def attempt(uid, family, addr, port, udp=False):
    """What a process of this uid gets when it sends to addr:port: 'ok',
    'refused', 'eperm', 'timeout', or the errno's name; and how long it took."""
    r, w = os.pipe()
    t0 = time.time()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        word = "ok"
        try:
            if uid:
                os.setgroups([])
                os.setgid(uid)
                os.setuid(uid)
            s = socket.socket(family, socket.SOCK_DGRAM if udp else socket.SOCK_STREAM)
            s.settimeout(4.0)
            if udp:
                s.sendto(b"x", (addr, port))
            else:
                s.connect((addr, port))
            s.close()
        except socket.timeout:
            word = "timeout"
        except OSError as e:
            word = {errno.ECONNREFUSED: "refused", errno.EPERM: "eperm"}.get(
                e.errno, errno.errorcode.get(e.errno, str(e.errno)))
        os.write(w, word.encode())
        os._exit(0)
    os.close(w)
    word = os.read(r, 64).decode()
    os.close(r)
    os.waitpid(pid, 0)
    return word, time.time() - t0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--nft", default=shutil.which("nft") or "nft")
    ap.add_argument("--rules", default=RULES)
    args = ap.parse_args()

    if os.environ.get("FFX_RULES_NS") != "1":
        if os.geteuid() != 0:
            print("skipped: needs root (it makes a network namespace and changes uid)")
            return SKIP
        if not (os.path.isfile(args.nft) or shutil.which(args.nft)) or not shutil.which("unshare"):
            print("skipped: needs nft and unshare")
            return SKIP
        env = dict(os.environ, FFX_RULES_NS="1")
        os.execvpe("unshare", ["unshare", "-n", sys.executable, os.path.abspath(__file__),
                               "--nft", args.nft, "--rules", os.path.abspath(args.rules)], env)

    def nft(*a, check_rc=True):
        p = subprocess.run([args.nft] + list(a), capture_output=True, text=True)
        if check_rc and p.returncode != 0:
            raise RuntimeError("nft %s -> %d %s" % (" ".join(a), p.returncode, p.stderr.strip()))
        return p

    lo_up()
    probe = nft("list", "tables", check_rc=False)
    if probe.returncode != 0:
        print("skipped: this kernel has no nf_tables (%s)" % probe.stderr.strip())
        return SKIP

    print("the file loads, twice")
    for n in (1, 2):
        p = nft("-f", args.rules, check_rc=False)
        check(p.returncode == 0, "load %d -> %d %s", n, p.returncode, p.stderr.strip()[:300])
    if failures:
        return 1
    table = json.loads(nft("-j", "list", "table", "inet", "ffx").stdout)["nftables"]
    chains = {c["chain"]["name"]: c["chain"] for c in table if "chain" in c}
    maps = [m["map"]["name"] for m in table if "map" in m]
    out = chains.get("output", {})
    check(out.get("hook") == "output" and out.get("policy") == "accept" and out.get("type") == "filter",
          "chain output is a filter on the output hook with policy accept: %s", out)
    check("pool" in chains and maps == ["allow"], "chain pool and map allow exist: %s %s", sorted(chains), maps)
    rules_text = nft("list", "table", "inet", "ffx").stdout
    check("meta skuid %d-%d jump pool" % (POOL_FIRST, POOL_LAST) in rules_text,
          "the jump names the pool's range, %d-%d", POOL_FIRST, POOL_LAST)

    l4, port4 = tcp_listener(socket.AF_INET, "127.0.0.1")
    l4b, port4b = tcp_listener(socket.AF_INET, "127.0.0.1")
    l6, port6 = tcp_listener(socket.AF_INET6, "::1")
    u4 = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    u4.bind(("127.0.0.1", 0))
    uport = u4.getsockname()[1]

    print("outside the pool nothing is touched")
    for uid in (0, POOL_FIRST - 1, POOL_LAST + 1):
        for fam, addr, port in ((socket.AF_INET, "127.0.0.1", port4), (socket.AF_INET6, "::1", port6)):
            word, _ = attempt(uid, fam, addr, port)
            check(word == "ok", "uid %d TCP to %s -> %s", uid, addr, word)
        word, _ = attempt(uid, socket.AF_INET, "127.0.0.1", uport, udp=True)
        check(word == "ok", "uid %d UDP to 127.0.0.1 -> %s", uid, word)

    print("a pool uid is refused, and at once")
    for uid in (POOL_FIRST, (POOL_FIRST + POOL_LAST) // 2, POOL_LAST):
        for fam, addr, port in ((socket.AF_INET, "127.0.0.1", port4), (socket.AF_INET6, "::1", port6)):
            word, took = attempt(uid, fam, addr, port)
            check(word == "refused" and took < 1.5, "uid %d TCP to %s -> %s in %.2f s", uid, addr, word, took)
        word, _ = attempt(uid, socket.AF_INET, "127.0.0.1", uport, udp=True)
        check(word == "eperm", "uid %d UDP to 127.0.0.1 -> %s", uid, word)
    u4.settimeout(0.3)
    got = 0
    try:
        while True:
            u4.recvfrom(16)
            got += 1
    except socket.timeout:
        pass
    check(got == 3, "the UDP receiver heard the three senders outside the pool and nobody else: %d", got)
    counted = [e["counter"]["packets"] for c in json.loads(nft("-j", "list", "chain", "inet", "ffx", "pool").stdout)["nftables"]
               if "rule" in c for e in c["rule"]["expr"] if "counter" in e]
    check(len(counted) == 2 and all(n > 0 for n in counted), "both refusals counted packets: %s", counted)

    print("an allowlist opens one destination to one uid")
    nft("add", "chain", "inet", "ffx", "u%d" % POOL_FIRST)
    nft("add", "rule", "inet", "ffx", "u%d" % POOL_FIRST, "ip", "daddr", "127.0.0.1", "tcp", "dport", str(port4), "accept")
    nft("add", "element", "inet", "ffx", "allow", "{ %d : jump u%d }" % (POOL_FIRST, POOL_FIRST))
    for uid, fam, addr, port, want, why in (
            (POOL_FIRST, socket.AF_INET, "127.0.0.1", port4, "ok", "the listed destination"),
            (POOL_FIRST, socket.AF_INET, "127.0.0.1", port4b, "refused", "another port"),
            (POOL_FIRST, socket.AF_INET6, "::1", port6, "refused", "another address"),
            (POOL_FIRST + 1, socket.AF_INET, "127.0.0.1", port4, "refused", "another uid")):
        word, _ = attempt(uid, fam, addr, port)
        check(word == want, "uid %d to %s port %d (%s) -> %s", uid, addr, port, why, word)

    print("loading the file again fails closed")
    nft("-f", args.rules)
    word, _ = attempt(POOL_FIRST, socket.AF_INET, "127.0.0.1", port4)
    check(word == "refused", "uid %d to its old destination -> %s", POOL_FIRST, word)
    gone = nft("list", "chain", "inet", "ffx", "u%d" % POOL_FIRST, check_rc=False)
    check(gone.returncode != 0, "the allowlist chain is gone")

    for s in (l4, l4b, l6, u4):
        s.close()
    print("%s: %d failure%s" % ("FAIL" if failures else "PASS", len(failures), "" if len(failures) == 1 else "s"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
