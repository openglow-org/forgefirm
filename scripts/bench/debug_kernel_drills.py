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
must be idle with no job.

  drill A  stop forgectrl, rmmod + modprobe glowforge three times with a
           settle between, and read dmesg for any lockdep/mutex splat.
  drill B  unbind the cnc device, unbind the 40 V regulator it needs so
           the re-bind probe returns -EPROBE_DEFER and unwinds, read
           dmesg for a clean unwind, then restore the regulator so the
           deferred probe completes and the rail returns.

PASS = both drills leave dmesg free of BUG/WARNING/lockdep/"held lock"
and the machine ends idle with the rail up and forgectrl serving.

Not a catalog test: it needs a special kernel and cycles the rail, so
it rides the closing burn, not the acceptance campaign.
"""
import glob
import os
import re
import subprocess
import sys
import time

CNC_DRV = '/sys/bus/platform/drivers/glowforge_cnc'
# Real splat markers only - not the benign "RCU lockdep checking is
# enabled" boot banner (a bare "lockdep" match would flag it).
SPLAT = re.compile(r'BUG:|WARNING:|INFO: possible|held lock|'
                   r'circular locking|bad unlock|sleeping function|'
                   r'still has locks held|DEBUG_LOCKS_WARN|'
                   r'lock held when returning')


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
            st = open('/sys/glowforge/cnc/state').read().strip()
        except OSError:
            st = ''
        if st in ('idle', 'disabled'):
            return
        time.sleep(1)
    print('REFUSED: cnc/state is %r, not idle.' % st)
    sys.exit(2)


def forgectrl(action):
    sh('/etc/init.d/forgectrl %s' % action)
    time.sleep(5)


def drill_load_unload():
    print('== drill A: module load/unload under DEBUG_MUTEXES')
    forgectrl('stop')          # release /dev/glowforge so the module can unload
    m = mark('A')
    ok = True
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
    found = splats(dmesg_since(m))
    forgectrl('start')
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
    forgectrl('stop')
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
    forgectrl('start')
    time.sleep(2)
    state = ''
    try:
        state = open('/sys/glowforge/cnc/state').read().strip()
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
