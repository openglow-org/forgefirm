#!/usr/bin/env python3
# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""Host test of the inbound filter: meta-forgefirm's inbound.nft and the
forgefirm-inbound script that keeps its set of on-link prefixes.

The machine is a network namespace of its own, joined by a veth pair to a
second one, the peer, which holds an address in the machine's on-link prefix,
a link-local address, and an address in a prefix the machine reaches through
the peer as its router (off the link). The machine also has a default route
with no gateway. Real connections, IPv6 and IPv4:
- before the rules load, every source gets in;
- with the rules and one pass of the script, the on-link and the link-local
  sources get in, the off-link one is dropped (the drop counted), IPv4 and
  the machine's own loopback are not touched, and the machine's own
  connection out to the off-link address completes (its replies are never
  judged);
- a pass of the script writes the set in one transaction (nft monitor
  counts one generation);
- the set holds the on-link prefix alone: not the routed prefix, the
  default route, the link-local one, a multicast or a local route;
- the prefix of an address of the machine's own that has no on-link route
  (a router's A=1, L=0 prefix) is in the set, and its hosts get in;
- the watcher, run with a short period, adds a prefix that comes on the
  link and lets its hosts in, takes it away when it goes, writes the set
  whole again after the file is loaded by hand, and loads the file again
  after the table is deleted;
- loading the file again empties the set;
- the init script's start loads the rules and starts the watcher, status
  says so, a second start leaves one watcher, and after stop no watcher
  process is left.

Runs as root (in network and mount namespaces of its own) or, where user
namespaces are open, unprivileged (in user, network and mount namespaces).
Needs nft, ip, unshare, nsenter and start-stop-daemon; exits 77 when one is
missing, 0 on a pass, 1 on a failure.

    python3 scripts/inbound-rules-test.py [--nft /path/to/nft] [--rules FILE] [--script FILE]
"""
import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time

HERE =os.path.dirname(os.path.abspath(__file__))
FILES = os.path.join(HERE, "..", "meta-forgefirm", "recipes-forgefirm", "forgefirm-inbound", "files")
SKIP = 77
SEARCH = os.pathsep.join([os.environ.get("PATH", ""), "/usr/sbin", "/sbin"])
ONLINK, ONLINK_PEER = "2001:db8:1::1", "2001:db8:1::2"
NEW, NEW_PEER = "2001:db8:2::1", "2001:db8:2::2"
OWN, OWN_PEER = "2001:db8:3::1", "2001:db8:3::2"
ROUTED_PEER = "2001:db8:99::2"
LOCAL_PEER = "fe80::2"
V4, V4_PEER = "10.98.0.1", "10.98.0.2"

# The peer: in its own network namespace, it waits to be given its
# interface, then answers one command a line: "connect <source> <address>
# <port>" (the result word), or "listen <address>" (the port).
PEER = r'''
import socket, sys
print("pid", flush=True)
held = []
for line in sys.stdin:
    cmd = line.split()
    if cmd[0] == "connect":
        src, dst, port = cmd[1], cmd[2], int(cmd[3])
        fam = socket.AF_INET6 if ":" in dst else socket.AF_INET
        s = socket.socket(fam, socket.SOCK_STREAM)
        s.settimeout(1.5)
        try:
            s.bind((src, 0, 0, socket.if_nametoindex("ibt1")) if src.startswith("fe80") else (src, 0))
            s.connect((dst, port, 0, socket.if_nametoindex("ibt1")) if dst.startswith("fe80") else (dst, port))
            word = "ok"
        except socket.timeout:
            word = "timeout"
        except OSError as e:
            word = "error:%s" % e.errno
        s.close()
        print(word, flush=True)
    elif cmd[0] == "listen":
        s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        s.bind((cmd[1], 0))
        s.listen(16)
        held.append(s)
        print(s.getsockname()[1], flush=True)
'''

failures = []


def check(ok, what, *args):
    text = what % args if args else what
    print("  %s  %s" % ("ok  " if ok else "FAIL", text), flush=True)
    if not ok:
        failures.append(text)


class Peer:
    def __init__(self):
        self.p = subprocess.Popen(["unshare", "-n", sys.executable, "-u", "-c", PEER], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, text=True)
        self.p.stdout.readline()
        self.ns = ["nsenter", "-t", str(self.p.pid), "-n"]

    def ask(self, *words):
        self.p.stdin.write(" ".join(str(w) for w in words) + "\n")
        self.p.stdin.flush()
        return self.p.stdout.readline().strip()

    def close(self):
        self.p.stdin.close()
        self.p.wait(timeout=5)


def ip(*args, ns=()):
    subprocess.run(list(ns) + ["ip"] + list(args), check=True, capture_output=True)


def connect_here(addr, port):
    """The machine's own connection to addr:port: 'ok', 'timeout', or the errno."""
    s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    s.settimeout(1.5)
    try:
        s.connect((addr, port))
        return "ok"
    except socket.timeout:
        return "timeout"
    except OSError as e:
        return "error:%s" % e.errno
    finally:
        s.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--nft", default=shutil.which("nft", path=SEARCH) or "nft")
    ap.add_argument("--rules", default=os.path.join(FILES, "inbound.nft"))
    ap.add_argument("--script", default=os.path.join(FILES, "forgefirm-inbound"))
    args = ap.parse_args()

    if os.environ.get("INBOUND_RULES_NS") != "1":
        missing = [t for t in ("unshare", "nsenter", "ip", "start-stop-daemon") if not shutil.which(t, path=SEARCH)]
        if missing or not (os.path.isfile(args.nft) or shutil.which(args.nft)):
            print("skipped: needs nft, unshare, nsenter, ip, and start-stop-daemon (missing: %s)"
                  % (", ".join(missing) or "nft"))
            return SKIP
        env = dict(os.environ, INBOUND_RULES_NS="1")
        ns = ["unshare", "-n", "-m"] if os.geteuid() == 0 else ["unshare", "-r", "-n", "-m"]
        argv = ns + [sys.executable, os.path.abspath(__file__), "--nft", args.nft,
                     "--rules", os.path.abspath(args.rules), "--script", os.path.abspath(args.script)]
        p = subprocess.run(argv, env=env)
        if p.returncode not in (0, 1):
            print("skipped: no network namespace can be made here (status %d)" % p.returncode)
            return SKIP
        return p.returncode

    def nft(*a, check_rc=True):
        p = subprocess.run([args.nft] + list(a), capture_output=True, text=True)
        if check_rc and p.returncode != 0:
            raise RuntimeError("nft %s -> %d %s" % (" ".join(a), p.returncode, p.stderr.strip()))
        return p

    # The script sets its own PATH, as on the image: it runs the system's
    # nft. Its rule file, for a reload, is the one under test.
    env = dict(os.environ, FF_INBOUND_RULES=os.path.abspath(args.rules))

    def script(*a, **more):
        return subprocess.run(["sh", args.script] + list(a), capture_output=True, text=True,
                              env=dict(env, **more), timeout=30)

    def onlink():
        p = nft("-j", "list", "set", "ip6", "inbound", "onlink", check_rc=False)
        if p.returncode != 0:
            return None
        out = set()
        for o in json.loads(p.stdout)["nftables"]:
            for e in (o.get("set") or {}).get("elem") or []:
                pre = e.get("prefix") if isinstance(e, dict) else None
                out.add("%s/%d" % (pre["addr"], pre["len"]) if pre else str(e))
        return out

    def until(cond, seconds=5):
        deadline = time.time() + seconds
        while time.time() < deadline and not cond():
            time.sleep(0.1)
        return cond()

    def watchers():
        """The pids of the watchers this script runs (`<script> watch`), its
        own command substitutions, which share the command line, left out."""
        found = {}
        for d in os.listdir("/proc"):
            if not d.isdigit():
                continue
            try:
                with open("/proc/%s/cmdline" % d, "rb") as f:
                    argv = f.read().split(b"\0")
                with open("/proc/%s/stat" % d) as f:
                    ppid = int(f.read().rsplit(")", 1)[1].split()[1])
            except (OSError, ValueError, IndexError):
                continue
            if argv[1:3] == [os.fsencode(args.script), b"watch"]:
                found[int(d)] = ppid
        return sorted(pid for pid, ppid in found.items() if ppid not in found)

    def generations(action):
        """The nf_tables transactions action commits, counted from nft
        monitor between two marks of the test's own."""
        mon = subprocess.Popen([args.nft, "monitor"], stdout=subprocess.PIPE, text=True)
        lines = []
        reader = threading.Thread(target=lambda: lines.extend(iter(mon.stdout.readline, "")), daemon=True)
        reader.start()
        try:
            nft("add", "table", "ip6", "testmark")
            until(lambda: any("add table ip6 testmark" in ln for ln in lines))
            action()
            nft("delete", "table", "ip6", "testmark")
            until(lambda: any("delete table ip6 testmark" in ln for ln in lines))
        finally:
            mon.terminate()
            mon.wait(timeout=5)
        text = list(lines)
        start = next(i for i, ln in enumerate(text) if "add table ip6 testmark" in ln)
        end = next(i for i, ln in enumerate(text) if "delete table ip6 testmark" in ln)
        return len([ln for ln in text[start + 2:end] if ln.startswith("# new generation")])

    # /run is the namespace's own, for the init script's pid file.
    subprocess.run(["mount", "--make-rprivate", "/"], check=True, capture_output=True)
    subprocess.run(["mount", "-t", "tmpfs", "tmpfs", "/run"], check=True, capture_output=True)
    ip("link", "set", "lo", "up")
    if nft("list", "tables", check_rc=False).returncode != 0:
        print("skipped: this kernel has no nf_tables")
        return SKIP
    peer = Peer()
    ip("link", "add", "ibt0", "type", "veth", "peer", "name", "ibt1")
    ip("link", "set", "ibt1", "netns", str(peer.p.pid))
    for a in (ONLINK + "/64", "fe80::1/64"):
        ip("-6", "addr", "add", a, "dev", "ibt0", "nodad")
    ip("addr", "add", V4 + "/24", "dev", "ibt0")
    ip("link", "set", "ibt0", "up")
    for a in (ONLINK_PEER + "/64", LOCAL_PEER + "/64", ROUTED_PEER + "/128"):
        ip("-6", "addr", "add", a, "dev", "ibt1", "nodad", ns=peer.ns)
    ip("addr", "add", V4_PEER + "/24", "dev", "ibt1", ns=peer.ns)
    ip("link", "set", "ibt1", "up", ns=peer.ns)
    ip("link", "set", "lo", "up", ns=peer.ns)
    # The routed prefix: the machine reaches it through the peer, its router.
    # And a default route with no gateway, whose prefix is every address.
    ip("-6", "route", "add", "2001:db8:99::/64", "via", LOCAL_PEER, "dev", "ibt0")
    ip("-6", "route", "add", "::/0", "dev", "ibt0")

    listener = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("::", 0))                        # dual stack, as the firmware's listeners are
    listener.listen(64)                             # nobody accepts: the kernel completes into the backlog
    port = listener.getsockname()[1]
    sources = ((ONLINK_PEER, ONLINK, "an on-link source"), (LOCAL_PEER, "fe80::1", "a link-local source"),
               (ROUTED_PEER, ONLINK, "an off-link source"), (V4_PEER, V4, "an IPv4 source"))
    for _ in range(50):                             # the veth pair's carrier
        if peer.ask("connect", ONLINK_PEER, ONLINK, port) == "ok":
            break
        time.sleep(0.1)

    print("without the rules every source gets in")
    for src, dst, what in sources:
        word = peer.ask("connect", src, dst, port)
        check(word == "ok", "%s -> %s", what, word)

    print("the file loads, twice, and one pass of the script fills the set")
    for n in (1, 2):
        p = nft("-f", args.rules, check_rc=False)
        check(p.returncode == 0, "load %d -> %d %s", n, p.returncode, p.stderr.strip()[:300])
    if failures:
        return 1
    p = script("render")
    check(p.returncode == 0, "render -> %d %s", p.returncode, (p.stdout + p.stderr).strip()[:300])
    check(onlink() == {"2001:db8:1::/64"}, "the set holds the on-link prefix alone: %s", onlink())
    # The flush and the add are one transaction: a set written apart would
    # be empty between the two, and an on-link SYN in that window dropped.
    n = generations(lambda: script("render"))
    check(n == 1, "a pass over a filled set commits one transaction: %d", n)

    print("a new connection from off the link is dropped, and nothing else")
    for src, dst, what in sources:
        word = peer.ask("connect", src, dst, port)
        want = "timeout" if src == ROUTED_PEER else "ok"
        check(word == want, "%s -> %s", what, word)
    rule = [e for o in json.loads(nft("-j", "list", "chain", "ip6", "inbound", "input").stdout)["nftables"]
            if "rule" in o for e in o["rule"]["expr"] if "counter" in e]
    check(len(rule) == 1 and rule[0]["counter"]["packets"] > 0, "the drop is counted: %s", rule)
    word = connect_here("::1", port)
    check(word == "ok", "the machine's own connection to ::1 -> %s", word)
    word = connect_here(ROUTED_PEER, int(peer.ask("listen", ROUTED_PEER)))
    check(word == "ok", "the machine's own connection to the off-link address completes -> %s", word)

    print("the prefix of the machine's own address with no on-link route is in the set")
    # A router's A=1, L=0 prefix: the machine takes an address in it, the
    # kernel makes no route for it, and the router is the way there.
    ip("-6", "addr", "add", OWN + "/64", "dev", "ibt0", "nodad", "noprefixroute")
    ip("-6", "route", "add", "2001:db8:3::/64", "via", LOCAL_PEER, "dev", "ibt0")
    ip("-6", "addr", "add", OWN_PEER + "/64", "dev", "ibt1", "nodad", ns=peer.ns)
    script("render")
    check(onlink() == {"2001:db8:1::/64", "2001:db8:3::/64"}, "the set: %s", onlink())
    word = peer.ask("connect", OWN_PEER, OWN, port)
    check(word == "ok", "a host in that prefix -> %s", word)
    ip("-6", "addr", "del", OWN + "/64", "dev", "ibt0")
    ip("-6", "route", "del", "2001:db8:3::/64", "via", LOCAL_PEER, "dev", "ibt0")
    script("render")
    check(onlink() == {"2001:db8:1::/64"}, "the address gone, its prefix is out of the set: %s", onlink())

    print("the watcher follows the routes and keeps the table")
    w = subprocess.Popen(["sh", args.script, "watch"], env=dict(env, FF_INBOUND_INTERVAL="0.2"),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ip("-6", "addr", "add", NEW + "/64", "dev", "ibt0", "nodad")
        ip("-6", "addr", "add", NEW_PEER + "/64", "dev", "ibt1", "nodad", ns=peer.ns)
        both = {"2001:db8:1::/64", "2001:db8:2::/64"}
        check(until(lambda: onlink() == both), "the new prefix is in the set: %s", onlink())
        word = peer.ask("connect", NEW_PEER, NEW, port)
        check(word == "ok", "a host in the new prefix -> %s", word)
        ip("-6", "addr", "del", NEW + "/64", "dev", "ibt0")
        check(until(lambda: onlink() == {"2001:db8:1::/64"}), "the prefix that went is out of the set: %s",
              onlink())
        nft("-f", args.rules)
        check(until(lambda: onlink() == {"2001:db8:1::/64"}), "a load by hand: the set is whole again: %s",
              onlink())
        nft("delete", "table", "ip6", "inbound")
        check(until(lambda: onlink() == {"2001:db8:1::/64"}), "the table deleted: it is back, the set whole: %s",
              onlink())
    finally:
        w.terminate()
        w.wait(timeout=5)

    print("loading the file again empties the set")
    nft("-f", args.rules)
    check(onlink() == set(), "the set is empty: %s", onlink())
    word = peer.ask("connect", ONLINK_PEER, ONLINK, port)
    check(word == "timeout", "an on-link source before the next pass -> %s", word)

    print("the init script starts, reports and stops the watcher")
    nft("delete", "table", "ip6", "inbound")
    running = []
    try:
        p = script("start", FF_INBOUND_INTERVAL="0.2")
        check(p.returncode == 0, "start -> %d %s", p.returncode, (p.stdout + p.stderr).strip()[:300])
        check(until(lambda: onlink() == {"2001:db8:1::/64"}), "the rules loaded and the set filled: %s", onlink())
        p = script("status")
        check(p.returncode == 0 and "watcher: running" in p.stdout, "status -> %d %s", p.returncode,
              p.stdout.strip())
        p = script("start", FF_INBOUND_INTERVAL="0.2")
        check(p.returncode == 0 and len(watchers()) == 1, "a second start leaves one watcher: %s", watchers())
        # the start loaded the file again, which empties the set until the
        # new watcher's first pass
        check(until(lambda: onlink() == {"2001:db8:1::/64"}), "and that watcher fills the set: %s", onlink())
        running = watchers()
    finally:
        p = script("stop")
        stopped = until(lambda: not watchers())
        for pid in watchers():                      # what a stop left running ends with the test
            os.kill(pid, 15)
    check(p.returncode == 0, "stop -> %d", p.returncode)
    check(running and stopped, "no watcher process survives the stop: %s ran", running)
    check(until(lambda: script("status").returncode == 1), "status after the stop -> %s",
          script("status").stdout.strip())
    check(onlink() == {"2001:db8:1::/64"}, "the rules stay in the kernel: %s", onlink())

    listener.close()
    peer.close()
    print("%s: %d failure%s" % ("FAIL" if failures else "PASS", len(failures), "" if len(failures) == 1 else "s"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
