#!/usr/bin/env python3
# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT
"""Kernel platform drills - runs ON the board, with forgectrl stopped
(/etc/init.d/forgectrl stop) so the pulse device is free. Restart forgectrl
afterward. Usage: platform_drills.py deadman|rmmod|decay|led|all

deadman  Trip the kernel dead-man mid-run and read back what it touched.
         Before the run: heater and TEC on, pump/exhaust/intake at a known
         duty, measure laser + UV LED lit, Z driver enabled. Open the pulse
         device with flock, motor_lock=15 (nothing moves), stream pads at
         10 kHz, run, then CLOSE the fd mid-program - the final close is the
         dead-man trip. PASS: the run stops; heater and TEC are off; the
         measure laser, UV LED and Z driver are off; pump, exhaust and intake
         are UNCHANGED (airflow and circulation stay with the engine).
rmmod    Three rmmod/modprobe cycles while another thread reads cnc/state
         and cnc/position in a tight loop the whole time. PASS: every cycle
         completes, the attrs come back, no oops/BUG/WARNING in dmesg.
decay    Set every decay mode (0/1/2) and microstep mode on each axis and
         read each back. PASS: readback equals the write, every time.
led      Drive the button and lid LEDs through target/pulse settings and
         back to their resting state. Operator confirms visually.
"""
import errno, fcntl, os, subprocess, sys, threading, time

TICK_HZ = 10000
PAD = b'\x00'
POWER0 = bytes([0x80])
G = '/sys/glowforge/'
LEDS = '/sys/class/leds/'


def wr(attr, val):
    with open(G + attr, 'w') as f:
        f.write(str(val))


def rd(attr):
    with open(G + attr) as f:
        return f.read().strip()


def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout


def open_pulsedev():
    try:
        fd = os.open('/dev/glowforge', os.O_WRONLY)
    except OSError as e:
        if e.errno == errno.EBUSY:
            print('ABORT: /dev/glowforge is busy - stop forgectrl first '
                  '(/etc/init.d/forgectrl stop), then re-run')
            sys.exit(1)
        raise
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def wait_state(want, timeout, poll=0.05):
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = rd('cnc/state')
        if s == want:
            return s
        time.sleep(poll)
    return rd('cnc/state')


def dmesg_since(marker_ts):
    out = sh('dmesg')
    keep = []
    for ln in out.splitlines():
        try:
            ts = float(ln.split(']')[0].strip('[ '))
        except (ValueError, IndexError):
            continue
        if ts >= marker_ts:
            keep.append(ln)
    return keep


def uptime():
    with open('/proc/uptime') as f:
        return float(f.read().split()[0])


# ------------------------------------------------------------- deadman

def drill_deadman():
    print('=== dead-man trip: what the kernel touches ===')
    watch = ['thermal/heater_pwm', 'thermal/tec_on', 'thermal/water_pump_on',
             'thermal/exhaust_pwm', 'thermal/intake_pwm',
             'head/measure_laser', 'head/uv_led', 'head/z_enable',
             'head/air_assist_pwm', 'head/white_led']
    before = {a: rd(a) for a in watch}
    print('resting: %s' % before)
    # Known pre-trip posture (all harmless for a few seconds).
    wr('thermal/heater_pwm', 30)
    wr('thermal/tec_on', 1)
    wr('thermal/water_pump_on', 1)
    wr('thermal/exhaust_pwm', 40)
    wr('thermal/intake_pwm', 40)
    wr('head/air_assist_pwm', 40)
    wr('head/measure_laser', 20)
    wr('head/uv_led', 20)
    wr('head/z_enable', 1)
    time.sleep(0.3)
    armed = {a: rd(a) for a in watch}
    print('pre-trip: %s' % armed)

    fd = open_pulsedev()
    t_mark = uptime()
    wr('cnc/motor_lock', 15)
    wr('cnc/laser_latch', 1)
    wr('cnc/step_freq', TICK_HZ)
    os.lseek(fd, 1, os.SEEK_SET)          # clear ring + counters
    os.write(fd, POWER0 + PAD * (TICK_HZ * 6))   # 6 s of pads
    wr('cnc/run', 1)
    st = wait_state('running', 2)
    print('run: state=%s' % st)
    time.sleep(1.0)
    print('closing the flock\'d fd mid-program (dead-man trip)')
    os.close(fd)
    time.sleep(0.5)
    after = {a: rd(a) for a in watch}
    print('post-trip: state=%s %s' % (rd('cnc/state'), after))
    for ln in dmesg_since(t_mark):
        if 'glowforge' in ln:
            print('  dmesg: %s' % ln)

    ok = True
    def expect(attr, val):
        nonlocal ok
        got = after[attr]
        good = got == str(val)
        ok &= good
        print('  %-24s %s (want %s) %s' % (attr, got, val, 'ok' if good else 'FAIL'))
    def unchanged(attr):
        nonlocal ok
        good = after[attr] == armed[attr]
        ok &= good
        print('  %-24s %s (want unchanged %s) %s'
              % (attr, after[attr], armed[attr], 'ok' if good else 'FAIL'))
    print('--- heat sources off:')
    expect('thermal/heater_pwm', 0)
    expect('thermal/tec_on', 0)
    print('--- head safe:')
    expect('head/measure_laser', 0)
    expect('head/uv_led', 0)
    expect('head/z_enable', 0)
    print('--- airflow and circulation left to the engine:')
    unchanged('thermal/water_pump_on')
    unchanged('thermal/exhaust_pwm')
    unchanged('thermal/intake_pwm')
    unchanged('head/air_assist_pwm')
    stopped = rd('cnc/state') != 'running'
    ok &= stopped
    print('  run stopped: %s' % stopped)
    # Restore the resting posture we found.
    for a, v in before.items():
        try:
            wr(a, v)
        except OSError:
            pass
    wr('cnc/motor_lock', 0)
    print('DEADMAN %s' % ('PASS' if ok else 'FAIL'))
    return 0 if ok else 1


# --------------------------------------------------------------- rmmod

def drill_rmmod():
    print('=== rmmod/modprobe x3 with concurrent attr reads ===')
    stop = threading.Event()
    reads = {'n': 0, 'err': 0}

    def reader():
        while not stop.is_set():
            for a in ('cnc/state', 'cnc/position', 'cnc/faults', 'head/hall_sensor'):
                try:
                    with open(G + a, 'rb') as f:
                        f.read(64)
                    reads['n'] += 1
                except OSError:
                    reads['err'] += 1      # expected while the module is out
    th = threading.Thread(target=reader, daemon=True)
    th.start()
    t_mark = uptime()
    ok = True
    for i in range(3):
        r1 = subprocess.run('rmmod glowforge', shell=True, capture_output=True, text=True)
        gone = not os.path.exists(G + 'cnc/state')
        r2 = subprocess.run('modprobe glowforge', shell=True, capture_output=True, text=True)
        time.sleep(1.0)
        back = os.path.exists(G + 'cnc/state') and rd('cnc/state') in ('idle', 'disabled')
        print('cycle %d: rmmod rc=%d %s| unloaded=%s | modprobe rc=%d %s| back=%s state=%s'
              % (i + 1, r1.returncode, r1.stderr.strip(), gone, r2.returncode,
                 r2.stderr.strip(), back, rd('cnc/state') if back else '-'))
        ok &= r1.returncode == 0 and gone and r2.returncode == 0 and back
    stop.set()
    th.join(2)
    print('concurrent reads: %d ok, %d refused while unloaded' % (reads['n'], reads['err']))
    bad = [ln for ln in dmesg_since(t_mark)
           if any(k in ln for k in ('Oops', 'BUG', 'WARNING', 'Call trace', 'Unable to handle'))]
    for ln in dmesg_since(t_mark):
        if 'glowforge' in ln and ('probe' in ln or 'init' in ln or 'remove' in ln):
            print('  dmesg: %s' % ln)
    if bad:
        ok = False
        print('KERNEL COMPLAINTS:')
        for ln in bad:
            print('  ' + ln)
    print('note: a module reload resets the analog config and the lid LED; the')
    print('      controller re-applies its analog config at start, relight the lid')
    print('      LED via /sys/class/leds/lid_led_*/target if wanted')
    print('RMMOD %s' % ('PASS' if ok else 'FAIL'))
    return 0 if ok else 1


# --------------------------------------------------------------- decay

def drill_decay():
    print('=== decay + microstep mode readback per axis ===')
    ok = True
    saved = {a: rd(a) for a in ('cnc/x_decay', 'cnc/y_decay', 'cnc/x_mode', 'cnc/y_mode')}
    print('resting: %s' % saved)
    for axis in ('x', 'y'):
        for v in (0, 1, 2, 1):
            wr('cnc/%s_decay' % axis, v)
            got = rd('cnc/%s_decay' % axis)
            good = got == str(v)
            ok &= good
            print('  %s_decay <- %d  reads %s  %s' % (axis, v, got, 'ok' if good else 'FAIL'))
        for v in (1, 2, 4, 8, 16, 32):
            wr('cnc/%s_mode' % axis, v)
            got = rd('cnc/%s_mode' % axis)
            good = got == str(v)
            ok &= good
            print('  %s_mode  <- %-2d reads %s  %s' % (axis, v, got, 'ok' if good else 'FAIL'))
        # Out-of-range writes must be refused, not wrapped.
        for a, bad in (('%s_decay' % axis, 3), ('%s_mode' % axis, 3)):
            try:
                wr('cnc/' + a, bad)
                print('  %s <- %d ACCEPTED (FAIL: should be refused)' % (a, bad))
                ok = False
            except OSError as e:
                print('  %s <- %d refused (%s) ok' % (a, bad, errno.errorcode.get(e.errno, e.errno)))
    for a, v in saved.items():
        wr(a, v)
    print('restored: %s' % {a: rd(a) for a in saved})
    print('DECAY %s' % ('PASS' if ok else 'FAIL'))
    return 0 if ok else 1


# ----------------------------------------------------------------- led

def led_wr(name, attr, val):
    with open(LEDS + name + '/' + attr, 'w') as f:
        f.write(str(val))


def led_rd(name, attr):
    with open(LEDS + name + '/' + attr) as f:
        return f.read().strip()


def drill_led():
    print('=== LED behavior (operator: watch the button and the lid) ===')
    names = [n for n in os.listdir(LEDS) if n.startswith('button_led_') or n.startswith('lid_led')]
    names.sort()
    saved = {n: (led_rd(n, 'target'), led_rd(n, 'pulse_on'), led_rd(n, 'pulse_off')) for n in names}
    print('leds: %s resting=%s' % (names, saved))
    print('  all to 255 (bright) for 2 s')
    for n in names:
        led_wr(n, 'pulse_on', 0); led_wr(n, 'pulse_off', 0); led_wr(n, 'target', 255)
    time.sleep(2)
    print('  all to 0 (dark) for 2 s')
    for n in names:
        led_wr(n, 'target', 0)
    time.sleep(2)
    print('  button LEDs pulsing 300/300 ms for 4 s')
    for n in names:
        if n.startswith('button_led_'):
            led_wr(n, 'pulse_on', 300); led_wr(n, 'pulse_off', 300)
    time.sleep(4)
    print('  restoring')
    for n, (t, on, off) in saved.items():
        led_wr(n, 'pulse_on', on); led_wr(n, 'pulse_off', off); led_wr(n, 'target', t)
    print('LED sequence done - operator verdict: bright / dark / pulse / restored?')
    return 0


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ''
    drills = {'deadman': drill_deadman, 'rmmod': drill_rmmod,
              'decay': drill_decay, 'led': drill_led}
    if mode == 'all':
        rc = 0
        for name in ('decay', 'led', 'deadman', 'rmmod'):
            rc |= drills[name]()
            print()
        return rc
    if mode not in drills:
        print(__doc__)
        return 2
    return drills[mode]()


if __name__ == '__main__':
    sys.exit(main())
