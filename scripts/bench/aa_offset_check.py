#!/usr/bin/env python3
"""Does the coolant reading hold still when the run airflow comes on?

With cool_aa_offset_counts at the machine's value the engine takes the
air-assist fan's ground shift off both coolant readings, so /status and
/cool/status should read the same coolant before and after M8 brings the
fans to the run profile, while the raw counts step by the offset. With
the setting at zero the readings drop by about a degree instead. Dark,
no press: M8 opens a run session without a laser-on. Runs on the board
(the bench directory on PYTHONPATH for the Grbl client).

Usage: aa_offset_check.py [dwell_s]
"""
import json
import sys
import time
import urllib.request

from live_fire_drills import Grbl, HOST, PORT, BASE


def raw():
    out = []
    for a in ('pic/water_temp_1', 'pic/water_temp_2'):
        with open('/sys/glowforge/' + a) as f:
            out.append(int(f.read().strip()))
    return out


def status():
    with urllib.request.urlopen(BASE + '/status', timeout=2) as r:
        s = json.load(r)
    with urllib.request.urlopen(BASE + '/cool/status', timeout=2) as r:
        c = json.load(r)
    with urllib.request.urlopen(BASE + '/settings', timeout=2) as r:
        st = json.load(r)
    return (s['coolant']['down_c'], s['coolant']['up_c'], c['down_c'], c['up_c'], c['phase'],
            st.get('cool_aa_offset_counts'))


def mean_over(secs):
    rs, ss = [], []
    end = time.time() + secs
    while time.time() < end:
        rs.append(raw())
        ss.append(status()[:4])
        time.sleep(0.5)
    n = len(rs)
    return ([sum(x[i] for x in rs) / n for i in range(2)],
            [sum(x[i] for x in ss) / n for i in range(4)])


def post_settings(**kv):
    """POST /settings with the panel token; returns the reply dict."""
    import urllib.parse
    try:
        with open('/data/forgefirm/panel.token') as f:
            tok = f.read().strip()
    except OSError:
        tok = ''
    req = urllib.request.Request(BASE + '/settings',
                                 data=urllib.parse.urlencode(kv).encode(),
                                 headers={'X-ForgeFIRM-Token': tok})
    with urllib.request.urlopen(req, timeout=4) as r:
        return json.load(r)


def main():
    dwell = float(sys.argv[1]) if len(sys.argv) > 1 else 20.0
    g = Grbl(HOST, PORT)
    if 'Idle' not in g.status():
        print('REFUSED: controller not Idle')
        return 2
    setting = status()[5]
    print('cool_aa_offset_counts = %s' % setting)
    # A run session starts the flow check, whose heater warms the
    # downstream sensor inside this check's dwell: off for this session,
    # back afterward.
    with urllib.request.urlopen(BASE + '/settings', timeout=2) as r:
        was = json.load(r).get('cool_flow_check_s')
    if was in (None, ''):
        was = '50'                        # unset reads as empty: the shipped default
    print('flow check off for this session (cool_flow_check_s was %s): %s'
          % (was, post_settings(cool_flow_check_s='0').get('cool_flow_check_s')))
    print('before M8: settling 8 s')
    r0, s0 = mean_over(8.0)
    print('  raw %.1f/%.1f  /status %.2f/%.2f  engine %.2f/%.2f' % (r0[0], r0[1], *s0))
    print('M8: %s' % g.cmd('M8'))
    time.sleep(6.0)                       # the fans to run duty, the offset in
    r1, s1 = mean_over(dwell)
    print('  raw %.1f/%.1f  /status %.2f/%.2f  engine %.2f/%.2f  (phase %s)'
          % (r1[0], r1[1], *s1, status()[4]))
    print('M9: %s' % g.cmd('M9'))
    if was is not None:
        post_settings(cool_flow_check_s=str(was))
    else:
        post_settings(cool_flow_check_s='50')
    with urllib.request.urlopen(BASE + '/settings', timeout=2) as r:
        print('flow check restored: cool_flow_check_s = %s' % json.load(r).get('cool_flow_check_s'))
    time.sleep(25.0)                      # the session closes, the fans idle
    r2, s2 = mean_over(8.0)
    print('  raw %.1f/%.1f  /status %.2f/%.2f  engine %.2f/%.2f' % (r2[0], r2[1], *s2))
    print('\nraw counts stepped %+.1f / %+.1f under the run profile; /status moved %+.2f / %+.2f C; '
          'the engine moved %+.2f / %+.2f C'
          % (r1[0] - r0[0], r1[1] - r0[1], s1[0] - s0[0], s1[1] - s0[1], s1[2] - s0[2], s1[3] - s0[3]))
    return 0


if __name__ == '__main__':
    sys.exit(main())
