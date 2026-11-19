#!/usr/bin/env python3
# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT
"""Debug-kernel drills: lock correctness across glowforge.ko load/unload
and a forced -EPROBE_DEFER unwind.

Runs only on the debug-kernel image (kas/forgefirm-glowforge-debug.yml),
which carries CONFIG_DEBUG_MUTEXES and lockdep. Both drills cycle the
40 V rail - a deliberate power cycle, the gamble the rail policy avoids
in normal operation and accepts here for one bench slot. The machine
must be idle with no job, and forgectrl, the controller and the cloud
client stopped so the module can unload: the bench page's takeover
stops them and starts forgectrl again after the run; from a shell, stop
forgectrl first and start it after. The drills refuse while any of the
three runs, and never start or stop forgectrl themselves.

  drill A  rmmod + modprobe glowforge three times with a settle between,
           while another thread reads cnc/state, cnc/position, cnc/faults
           and head/hall_sensor in a tight loop the whole time, and read
           dmesg for any lockdep/mutex splat or oops.
  drill B  unbind the cnc device, unbind the 40 V regulator it needs so
           the re-bind probe returns -EPROBE_DEFER and unwinds, read
           dmesg for a clean unwind, then restore the regulator so the
           deferred probe completes.

PASS = both drills leave dmesg free of BUG/WARNING/lockdep/"held lock"
and the module bound and idle (or disabled, the rail off until
forgectrl's start enables it).

Not a catalog test: it needs a special kernel and cycles the rail, so
it rides the closing burn, not the acceptance campaign.
"""
import glob
import os
import re
import subprocess
import sys
import threading
import time

from gfbench import arming_processes

CNC_DRV = '/sys/bus/platform/drivers/glowforge_cnc'
STATE = '/sys/glowforge/cnc/state'
# Real splat markers only - not the benign "RCU lockdep checking is
# enabled" boot banner (a bare "lockdep" match would flag it).
SPLAT = re.compile(r'BUG:|WARNING:|INFO: possible|held lock|'
                   r'circular locking|bad unlock|sleeping function|'
                   r'still has locks held|DEBUG_LOCKS_WARN|'
                   r'lock held when returning|Oops|Unable to handle|'
                   r'Call trace')
SYSFS = '/sys/glowforge/'
# What drill A reads while the module goes and comes back.
RACE_ATTRS = ('cnc/state', 'cnc/position', 'cnc/faults', 'head/hall_sensor')


class Reader(object):
    """A thread that reads the module's attributes in a tight loop: the
    remove path must not oops under a reader. A read refused while the
    module is out is counted, never an error."""

    def __init__(self):
        self.stop = threading.Event()
        self.reads = {'n': 0, 'err': 0}
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self.stop.is_set():
            for a in RACE_ATTRS:
                try:
                    with open(SYSFS + a, 'rb') as f:
                        f.read(64)
                    self.reads['n'] += 1
                except OSError:
                    self.reads['err'] += 1

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.thread.join(2)
        return False


def sh(cmd, check=False):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if check and r.returncode != 0:
        print('  ! %s -> rc %d %s' % (cmd, r.returncode, (r.stderr or '').strip()[:120]))
    return r


def dmesg_since(mark):
    out = sh('dmesg').stdout
    i = out.rfind(mark)
    return out[i + len(mark):] if i >= 0 else out


def mark(tag):
    # A unique needle in the kernel log to bound a drill's window.
    sh('echo "DRILL-%s" > /dev/kmsg' % tag)
    return 'DRILL-%s' % tag


def splats(text):
    return [ln for ln in text.splitlines() if SPLAT.search(ln)]


def require_debug_kernel():
    cfg = ''
    if os.path.exists('/proc/config.gz'):
        cfg = sh('zcat /proc/config.gz').stdout
    if 'CONFIG_DEBUG_MUTEXES=y' not in cfg:
        print('REFUSED: not a debug kernel (CONFIG_DEBUG_MUTEXES not set). '
              'Flash the debug image (kas/forgefirm-glowforge-debug.yml).')
        sys.exit(2)


def require_idle():
    # The controller re-init after a forgectrl restart passes briefly
    # through 'running'; wait a few seconds for it to settle before
    # refusing.
    st = ''
    for _ in range(8):
        try:
            with open(STATE) as f:
                st = f.read().strip()
        except OSError:
            st = ''
        if st in ('idle', 'disabled'):
            return
        time.sleep(1)
    print('REFUSED: cnc/state is %r, not idle.' % st)
    sys.exit(2)


def require_stopped():
    # The module unloads only with /dev/glowforge free, and nothing may
    # arm a job across the rail cycles: forgectrl, the controller and the
    # cloud client are stopped by whoever runs the drill (the takeover).
    owners = arming_processes()
    if owners:
        print('REFUSED: %s running; stop forgectrl first (the bench page runs this '
              'drill under its takeover).' % ', '.join(owners))
        sys.exit(2)


def drill_load_unload():
    print('== drill A: module load/unload under DEBUG_MUTEXES')
    m = mark('A')
    ok = True
    with Reader() as reader:
        for i in range(3):
            r = sh('rmmod glowforge', check=True)
            if r.returncode != 0:
                print('  rmmod failed on cycle %d - aborting drill A' % (i + 1))
                ok = False
                break
            time.sleep(2)          # rail settle (the module powered it off)
            r = sh('modprobe glowforge', check=True)
            if r.returncode != 0:
                print('  modprobe failed on cycle %d' % (i + 1))
                ok = False
                break
            time.sleep(2)
            print('  cycle %d: unload/reload ok' % (i + 1))
    print('  concurrent reads: %d ok, %d refused while unloaded'
          % (reader.reads['n'], reader.reads['err']))
    found = splats(dmesg_since(m))
    if found:
        print('  FAIL: %d lock splat(s):' % len(found))
        for ln in found[:8]:
            print('   ', ln)
        return False
    print('  PASS: three load/unload cycles, no lock splat')
    return ok


def regulator_provider():
    # The 40 V regulator the cnc probe holds exclusively; unbinding its
    # provider makes the re-bind probe defer. Resolve from the cnc node's
    # 40v-supply phandle if present, else the known fixed regulator name.
    drivers = (glob.glob('/sys/bus/platform/drivers/*regulator*') +
               glob.glob('/sys/bus/platform/drivers/reg-fixed*'))
    for d in drivers:
        for dev in os.listdir(d):
            if dev in ('bind', 'unbind', 'module', 'uevent'):
                continue
            if '40v' in dev.lower():          # e.g. regulators:40v
                return os.path.basename(d), dev
    return None, None


def drill_forced_defer():
    print('== drill B: forced -EPROBE_DEFER unwind')
    rdrv, rdev = regulator_provider()
    if not rdev:
        print('  SKIP: could not resolve the 40 V regulator provider to unbind '
              '(inspect /sys/bus/platform/drivers/*regulator*); drill A stands.')
        return None
    m = mark('B')
    # Take cnc down, remove the resource, bring cnc back -> probe defers.
    sh('echo cnc > %s/unbind' % CNC_DRV, check=True)
    time.sleep(1)
    sh('echo %s > /sys/bus/platform/drivers/%s/unbind' % (rdev, rdrv), check=True)
    time.sleep(1)
    sh('echo cnc > %s/bind' % CNC_DRV)   # returns -EPROBE_DEFER, unwinds
    time.sleep(2)
    deferred = 'cnc' not in os.listdir(CNC_DRV)
    print('  cnc probe deferred (not bound after the resource was removed): %s' % deferred)
    # Restore the resource -> the deferred probe retries and completes.
    sh('echo %s > /sys/bus/platform/drivers/%s/bind' % (rdev, rdrv), check=True)
    time.sleep(3)
    bound = 'cnc' in os.listdir(CNC_DRV)
    found = splats(dmesg_since(m))
    state = ''
    try:
        with open(STATE) as f:
            state = f.read().strip()
    except OSError:
        pass
    print('  cnc bound after restore: %s; cnc/state=%r' % (bound, state))
    if found:
        print('  FAIL: %d lock splat(s) on the unwind:' % len(found))
        for ln in found[:8]:
            print('   ', ln)
        return False
    if not (deferred and bound and state in ('idle', 'disabled')):
        print('  FAIL: defer=%s bound=%s state=%r' % (deferred, bound, state))
        return False
    print('  PASS: probe deferred and unwound clean, then completed on restore')
    return True


def main():
    require_debug_kernel()
    require_stopped()
    require_idle()
    a = drill_load_unload()
    require_idle()
    b = drill_forced_defer()
    print('== summary: load/unload %s, forced-defer %s'
          % ('PASS' if a else 'FAIL',
             'PASS' if b else ('SKIP' if b is None else 'FAIL')))
    sys.exit(0 if a and b is not False else 1)


if __name__ == '__main__':
    main()
