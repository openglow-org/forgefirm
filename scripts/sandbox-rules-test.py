#!/usr/bin/env python3
# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""Host test of the extension sandbox's deny rules.

Loads meta-forgefirm's ffx.nft into a network namespace of its own, joins a
second namespace to it with a veth pair (a peer that is not this machine),
and sends real traffic from real uids. An extension account is refused
everywhere: on loopback, IPv4 and IPv6, at its own LAN address, and at the
peer, TCP at once and UDP with EPERM. The uids on either side of the pool,
and root, are not touched. An allowlist chain lets one uid reach one port of
the peer and nothing else. The machine itself is never a destination: with
loopback and the machine's own LAN address on its allowlist, a pool uid is
still refused at both. And loading the file again takes the allowlist away.

Needs root, nft, ip, nsenter, and a kernel with nf_tables; exits 77 when one
is missing, 0 on a pass, 1 on a failure.

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
HERE_ADDR, PEER_ADDR = "10.99.0.1", "10.99.0.2"
SKIP = 77

# The peer: in its own network namespace, it waits to be given its interface,
# then listens on two TCP ports and one UDP port and says which.
PEER = r'''
import socket, sys
print("pid", flush=True)
sys.stdin.readline()
tcp = []
for _ in range(2):
    s = socket.socket()
    s.bind(("%s", 0))
    s.listen(128)
    tcp.append(s)
u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
u.bind(("%s", 0))
print(tcp[0].getsockname()[1], tcp[1].getsockname()[1], u.getsockname()[1], flush=True)
sys.stdin.readline()
''' % (PEER_ADDR, PEER_ADDR)

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


def join_peer():
    """Start the peer in its own namespace and join it with a veth pair.
    Returns (process, tcp port, second tcp port, udp port)."""
    peer = subprocess.Popen(["unshare", "-n", sys.executable, "-u", "-c", PEER], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, text=True)
    peer.stdout.readline()
    ns = ["nsenter", "-t", str(peer.pid), "-n"]
    for cmd in (["ip", "link", "add", "ffxt0", "type", "veth", "peer", "name", "ffxt1"],
                ["ip", "link", "set", "ffxt1", "netns", str(peer.pid)],
                ["ip", "addr", "add", HERE_ADDR + "/24", "dev", "ffxt0"],
                ["ip", "link", "set", "ffxt0", "up"],
                ns + ["ip", "addr", "add", PEER_ADDR + "/24", "dev", "ffxt1"],
                ns + ["ip", "link", "set", "ffxt1", "up"],
                ns + ["ip", "link", "set", "lo", "up"]):
        subprocess.run(cmd, check=True, capture_output=True)
    peer.stdin.write("go\n")
    peer.stdin.flush()
    ports = [int(x) for x in peer.stdout.readline().split()]
    return peer, ports[0], ports[1], ports[2]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--nft", default=shutil.which("nft") or "nft")
    ap.add_argument("--rules", default=RULES)
    args = ap.parse_args()

    if os.environ.get("FFX_RULES_NS") != "1":
        if os.geteuid() != 0:
            print("skipped: needs root (it makes network namespaces and changes uid)")
            return SKIP
        missing = [t for t in ("unshare", "nsenter", "ip") if not shutil.which(t)]
        if missing or not (os.path.isfile(args.nft) or shutil.which(args.nft)):
            print("skipped: needs nft, unshare, nsenter, and ip (missing: %s)" % (", ".join(missing) or "nft"))
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
    peer, pport, pport2, pudp = join_peer()
    for _ in range(50):                                  # the veth pair's carrier
        if attempt(0, socket.AF_INET, PEER_ADDR, pport)[0] == "ok":
            break
        time.sleep(0.1)

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
    check({"pool", "refuse"} <= set(chains) and maps == ["allow"], "chains pool and refuse and map allow exist: %s %s",
          sorted(chains), maps)
    text = nft("list", "table", "inet", "ffx").stdout
    check("meta skuid %d-%d jump pool" % (POOL_FIRST, POOL_LAST) in text, "the jump names the pool's range, %d-%d",
          POOL_FIRST, POOL_LAST)
    pool = text[text.find("chain pool"):].split("}")[0]
    check(0 <= pool.find('oifname "lo" jump refuse') < pool.find("vmap @allow"),
          "the machine itself is refused ahead of the allow map")

    l4, port4 = tcp_listener(socket.AF_INET, "127.0.0.1")
    l6, port6 = tcp_listener(socket.AF_INET6, "::1")
    lself, portself = tcp_listener(socket.AF_INET, HERE_ADDR)
    u4 = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    u4.bind(("127.0.0.1", 0))
    uport = u4.getsockname()[1]
    everywhere = ((socket.AF_INET, "127.0.0.1", port4, "loopback"), (socket.AF_INET6, "::1", port6, "IPv6 loopback"),
                  (socket.AF_INET, HERE_ADDR, portself, "its own LAN address"), (socket.AF_INET, PEER_ADDR, pport, "the peer"))

    print("outside the pool nothing is touched")
    for uid in (0, POOL_FIRST - 1, POOL_LAST + 1):
        for fam, addr, port, what in everywhere:
            word, _ = attempt(uid, fam, addr, port)
            check(word == "ok", "uid %d TCP to %s -> %s", uid, what, word)
        for addr, port in (("127.0.0.1", uport), (PEER_ADDR, pudp)):
            word, _ = attempt(uid, socket.AF_INET, addr, port, udp=True)
            check(word == "ok", "uid %d UDP to %s -> %s", uid, addr, word)

    print("a pool uid is refused everywhere, and at once")
    for uid in (POOL_FIRST, (POOL_FIRST + POOL_LAST) // 2, POOL_LAST):
        for fam, addr, port, what in everywhere:
            word, took = attempt(uid, fam, addr, port)
            check(word == "refused" and took < 1.5, "uid %d TCP to %s -> %s in %.2f s", uid, what, word, took)
        for addr, port in (("127.0.0.1", uport), (PEER_ADDR, pudp)):
            word, _ = attempt(uid, socket.AF_INET, addr, port, udp=True)
            check(word == "eperm", "uid %d UDP to %s -> %s", uid, addr, word)
    u4.settimeout(0.3)
    got = 0
    try:
        while True:
            u4.recvfrom(16)
            got += 1
    except socket.timeout:
        pass
    check(got == 3, "the UDP receiver heard the three senders outside the pool and nobody else: %d", got)
    counted = [e["counter"]["packets"] for c in json.loads(nft("-j", "list", "chain", "inet", "ffx", "refuse").stdout)["nftables"]
               if "rule" in c for e in c["rule"]["expr"] if "counter" in e]
    check(len(counted) == 2 and all(n > 0 for n in counted), "both refusals counted packets: %s", counted)

    print("an allowlist opens one destination to one uid")
    chain = "u%d" % POOL_FIRST
    nft("add", "chain", "inet", "ffx", chain)
    nft("add", "rule", "inet", "ffx", chain, "ip", "daddr", PEER_ADDR, "tcp", "dport", str(pport), "accept")
    nft("add", "element", "inet", "ffx", "allow", "{ %d : jump %s }" % (POOL_FIRST, chain))
    for uid, addr, port, want, why in (
            (POOL_FIRST, PEER_ADDR, pport, "ok", "the listed destination"),
            (POOL_FIRST, PEER_ADDR, pport2, "refused", "another port of the peer"),
            (POOL_FIRST, "127.0.0.1", port4, "refused", "loopback"),
            (POOL_FIRST + 1, PEER_ADDR, pport, "refused", "another uid")):
        word, _ = attempt(uid, socket.AF_INET, addr, port)
        check(word == want, "uid %d to %s port %d (%s) -> %s", uid, addr, port, why, word)
    word, _ = attempt(POOL_FIRST, socket.AF_INET, PEER_ADDR, pudp, udp=True)
    check(word == "eperm", "uid %d UDP to the peer, which its list does not name -> %s", POOL_FIRST, word)

    print("the machine itself is never a destination")
    nft("add", "rule", "inet", "ffx", chain, "ip", "daddr", "127.0.0.1", "tcp", "dport", str(port4), "accept")
    nft("add", "rule", "inet", "ffx", chain, "ip", "daddr", HERE_ADDR, "tcp", "dport", str(portself), "accept")
    nft("add", "rule", "inet", "ffx", chain, "ip6", "daddr", "::1", "tcp", "dport", str(port6), "accept")
    for fam, addr, port, what in everywhere[:3]:
        word, _ = attempt(POOL_FIRST, fam, addr, port)
        check(word == "refused", "uid %d to %s, which is on its allowlist -> %s", POOL_FIRST, what, word)
    word, _ = attempt(POOL_FIRST, socket.AF_INET, PEER_ADDR, pport)
    check(word == "ok", "and the peer still answers it -> %s", word)

    print("loading the file again fails closed")
    nft("-f", args.rules)
    word, _ = attempt(POOL_FIRST, socket.AF_INET, PEER_ADDR, pport)
    check(word == "refused", "uid %d to its old destination -> %s", POOL_FIRST, word)
    gone = nft("list", "chain", "inet", "ffx", chain, check_rc=False)
    check(gone.returncode != 0, "the allowlist chain is gone")

    for s in (l4, l6, lself, u4):
        s.close()
    peer.stdin.close()
    peer.wait(timeout=5)
    print("%s: %d failure%s" % ("FAIL" if failures else "PASS", len(failures), "" if len(failures) == 1 else "s"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
