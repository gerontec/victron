"""
test_house.py - 0.81-py D7 with the UPS rotary switch on AC-out1: detection (on after 60 s above 200 W, off after 120 s
below 100 W) and the amount (whole AC-out1 load, plus HOUSE_WP_EXTRA_W in winter while the heat pump runs).
usage: python3 tests/test_house.py
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
import bm_logic as L  # noqa: E402

fails = 0


def check(cond, what):
    global fails
    print(("ok   " if cond else "FAIL ") + what)
    if not cond:
        fails += 1


NIGHT_JAN = int(time.mktime((2026, 1, 15, 22, 0, 0, 0, 0, -1)))
NOON_JUL = int(time.mktime((2026, 7, 15, 12, 0, 0, 0, 0, -1)))


def cfg(mode=2):
    c = L.Cfg()
    for i, p in enumerate(L.BM_PARAMS):
        if p.name in L.SITE_PARAMS:
            c.set_param(i, L.SITE_PARAMS[p.name])
    c.house_acout = mode
    return c


def make(now, t, acout, wp, given, soc, other):
    load = acout + (wp if wp >= 300 else 0) + other
    i = L.In()
    i.now, i.t = now, t
    i.have_pcc = i.have_pv = 1
    i.inv_time = now - 1
    i.pv, i.bat1 = 0.0, 0.0
    i.pcc = -(load - given)
    i.pcc_avg5, i.bat1_avg5 = i.pcc, 0.0
    i.have_soc_bat1, i.soc_bat1 = 1, 3.0
    i.wp, i.wp_time = wp, now - 1
    i.grid_ok = 1
    for p in range(L.NPH):
        i.ac_ok[p], i.ac_in[p] = 1, -given / L.NPH
        i.ac_out_ok[p], i.ac_out[p] = 1, acout / L.NPH
    for b in range(L.NBANK):
        i.bms_ok[b], i.soc[b] = 1, soc
        i.power_ok[b], i.power[b] = 1, -given
        i.volt_ok[b], i.volt[b] = 1, 52.0
        i.lim_ok[b], i.ccl[b], i.dcl[b] = 1, 200.0, 200.0
        i.cell_ok[b], i.cell_min[b] = 1, 3.30
        i.cell_max_ok[b], i.cell_max[b] = 1, 3.35
    return i


def run(profile, t=NIGHT_JAN, wp=3000.0, soc=60.0, mode=2, other=0.0):
    """profile = [(AC-out W, seconds)]; returns [(elapsed s, house, given W, rule text of the phase that gives most)]"""
    c, st, given, now, res, el = cfg(mode), L.State(), 0.0, 1000.0, [], 0.0
    for acout, secs in profile:
        for _ in range(int(secs / L.CYCLE_SECONDS)):
            o = L.bm_step(c, make(now, t, acout, wp, given, soc, other), st)
            given = float(-sum(min(s, 0) for s in o.sp))
            why = o.why[min(range(L.NPH), key=lambda p: o.sp[p])]
            res.append((el, o.house, given, why))
            now += L.CYCLE_SECONDS
            el += L.CYCLE_SECONDS
    return res


def first_on(r):
    return next((x[0] for x in r if x[1]), None)


check((L.HOUSE_ON_W, L.HOUSE_OFF_W, L.HOUSE_ON_S, L.HOUSE_OFF_S) == (200.0, 100.0, 60.0, 120.0), "thresholds 200 W / 60 s, 100 W / 120 s")
check(cfg().house_wp_extra_w == 40, "HOUSE_WP_EXTRA_W default 40 W")

r = run([(450, 300)])
t = first_on(r)
check(t is not None and 60 <= t <= 60 + 3 * L.CYCLE_SECONDS, "450 W on AC-out: D7 on after %s s (60)" % t)
check(not any(x[1] for x in r if x[0] < 60), "... not before 60 s")
check(abs(r[-1][2] - 490) <= 2, "winter, WP on: discharge %d W (house 450 + 40)" % r[-1][2])
check(r[-1][3].startswith("HOUSE") and " 1PH" in r[-1][3] and r[-1][3].endswith(" WP+40"), "... rule text %r" % r[-1][3])
check("WPMAX" not in r[-1][3], "... no WPMAX cap")

r = run([(450, 300)], wp=50.0)
check(abs(r[-1][2] - 450) <= 2 and "WP+" not in r[-1][3], "heat pump off: discharge %d W (house 450, no extra)" % r[-1][2])

r = run([(1500, 300)])
check(abs(r[-1][2] - 1540) <= 3 and " EQ" in r[-1][3], "house 1500 W: %d W shared by all three (%r)" % (r[-1][2], r[-1][3]))

r = run([(130, 600)])
check(first_on(r) is None, "130 W (grid position idle peak): D7 stays off")

r = run([(40, 60), (1367, 50), (40, 300)])
check(first_on(r) is None, "1367 W for 50 s: too short, D7 stays off")

r = run([(450, 200), (150, 300)])
check(r[-1][1] == 1, "load falls to 150 W (between the thresholds): D7 stays on")

r = run([(450, 200), (40, 110)])
check(r[-1][1] == 1, "switch back, 40 W for 110 s: still on")
r = run([(450, 200), (40, 130)])
check(r[-1][1] == 0, "... after 130 s below 100 W: off")
check("HOUSE" not in r[-1][3], "... rule again %r" % r[-1][3])

r = run([(450, 200), (40, 110), (450, 30), (40, 110)])
check(r[-1][1] == 1, "a load between restarts the 120 s timer")

r = run([(450, 300)], mode=0)
check(first_on(r) is None, "HOUSE_ACOUT 0: never on")
r = run([(40, 30)], mode=1)
check(r[0][1] == 1, "HOUSE_ACOUT 1: on at once")

r = run([(450, 300)], soc=6.0)
check(r[-1][2] == 0, "SoC 6 %% (PROT): no discharge (%d W)" % r[-1][2])

r = run([(450, 300)], t=NOON_JUL, wp=3000.0)
check("WP+" not in r[-1][3], "summer: no extra, PCC rule covers the heat pump (%r)" % r[-1][3])

print("%d failed" % fails)
sys.exit(1 if fails else 0)
