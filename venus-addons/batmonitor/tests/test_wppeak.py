"""
test_wppeak.py - 0.80-py heat pump peak rule (D1_WP_PEAK): heat pump running and the grid import without the own
discharge above WP_PEAK_ON_W (5000 W) -> the stacks hold the import at 5000 W, at most WP_PEAK_W (1000 W).
A closed loop: the PCC of the next cycle = the house load minus what the MultiPlus give.
usage: python3 tests/test_wppeak.py
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


C = L.Cfg()
for i, p in enumerate(L.BM_PARAMS):
    if p.name in L.SITE_PARAMS:
        C.set_param(i, L.SITE_PARAMS[p.name])
NIGHT_JAN = int(time.mktime((2026, 1, 15, 22, 0, 0, 0, 0, -1)))
NOON_JUL = int(time.mktime((2026, 7, 15, 12, 0, 0, 0, 0, -1)))


def make(now, t, load, wp, given, bat1=0.0, pv=0.0, soc=60.0):
    """load = grid import W the house + heat pump would draw without the stacks; given = what the Multis give now"""
    i = L.In()
    i.now, i.t = now, t
    i.have_pcc = i.have_pv = 1
    i.inv_time = now - 1
    i.pv, i.bat1 = pv, bat1
    i.pcc = -(load - given)
    i.pcc_avg5, i.bat1_avg5 = i.pcc, bat1
    i.have_soc_bat1, i.soc_bat1 = 1, 3.0
    i.wp, i.wp_time = wp, now - 1
    i.grid_ok = 1
    for p in range(L.NPH):
        i.ac_ok[p], i.ac_in[p] = 1, -given / L.NPH
        i.ac_out_ok[p], i.ac_out[p] = 1, 0.0
    for b in range(L.NBANK):
        i.bms_ok[b], i.soc[b] = 1, soc
        i.power_ok[b], i.power[b] = 1, -given
        i.volt_ok[b], i.volt[b] = 1, 52.0
        i.lim_ok[b], i.ccl[b], i.dcl[b] = 1, 200.0, 200.0
        i.cell_ok[b], i.cell_min[b] = 1, 3.30
        i.cell_max_ok[b], i.cell_max[b] = 1, 3.35
    return i


def run(loads, t, wp=3000.0, soc=60.0, bat1=0.0):
    """closed loop over the load sequence; returns [(given W, pcc W, wp_peak, why L1)] per cycle"""
    st, given, res, now = L.State(), 0.0, [], 1000.0
    for load in loads:
        o = L.bm_step(C, make(now, t, load, wp, given, bat1=bat1, soc=soc), st)
        given = float(-sum(min(s, 0) for s in o.sp))
        res.append((given, -(load - given), o.wp_peak, o.why[0]))
        now += L.CYCLE_SECONDS
    return res


check((C.wp_peak_on_w, C.wp_peak_off_w, C.wp_peak_w) == (5000, 4500, 1000), "defaults 5000 / 4500 / 1000 W")

r = run([5600] * 6, NIGHT_JAN)
check(r[-1][2] == 1 and abs(r[-1][0] - 600) <= 2, "winter night, WP on, load 5600 W: discharge %d W (600)" % r[-1][0])
check(abs(r[-1][1] + 5000) <= 2, "... PCC held at %d W (-5000)" % r[-1][1])
check(r[-1][3].startswith("WPPEAK"), "... rule text %r" % r[-1][3])

r = run([7500] * 6, NIGHT_JAN)
check(r[-1][2] == 1 and abs(r[-1][0] - 1000) <= 2, "load 7500 W: capped at %d W (1000)" % r[-1][0])

r = run([7500] * 6, NOON_JUL, soc=60.0)
check(r[-1][2] == 1 and r[-1][0] >= 1000, "summer, no heat pump cap, load 7500 W: PROP gives %d W, the peak rule never lowers it" % r[-1][0])

r = run([5600] * 6, NIGHT_JAN, wp=100.0)
check(r[-1][2] == 0 and not r[-1][3].startswith("WPPEAK"), "heat pump off: no peak rule (%r, %d W as before)" % (r[-1][3], r[-1][0]))

r = run([4900] * 6, NIGHT_JAN)
check(r[-1][2] == 0, "load 4900 W: rule stays off")

r = run([5600] * 4 + [4700] * 4 + [4400] * 4, NIGHT_JAN)
check(r[7][2] == 1 and r[7][0] <= C.wp_winter_w + 2, "drop to 4700 W: latch holds, %d W (no peak amount)" % r[7][0])
check(r[-1][2] == 0, "below 4500 W: rule off")

r = run([7500] * 6, NIGHT_JAN, bat1=-2000.0)
check(r[-1][2] == 1 and abs(r[-1][0] - 1000) <= 2, "Sofar Bat1 discharges (winter cap 0 W): still %d W" % r[-1][0])

r = run([7500] * 6, NIGHT_JAN, soc=6.0)
check(r[-1][0] == 0, "SoC 6 %% (PROT): no discharge (%d W)" % r[-1][0])

print("%d failed" % fails)
sys.exit(1 if fails else 0)
