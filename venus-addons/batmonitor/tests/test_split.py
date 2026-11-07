"""
test_split.py - 0.77-py one parallel bank: closed-loop runs of bm_step against a simple plant (PCC = PV - load - the
Multis' AC-in), checks the split rule: up to SINGLE_MAX_W one MultiPlus (rotating every ROTATE_S), above all three.
usage: python3 tests/test_split.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import bm_logic as L  # noqa: E402

os.environ["TZ"] = "Europe/Berlin"
time.tzset()
T0 = 1794060000                      # 2026-11-07 15:00 local, winter
fails = 0


def check(cond, what):
    global fails
    print(("ok   " if cond else "FAIL ") + what)
    if not cond:
        fails += 1


def cfg():
    c = L.Cfg()
    for i, p in enumerate(L.BM_PARAMS):
        if p.name in L.SITE_PARAMS:
            c.set_param(i, L.SITE_PARAMS[p.name])
    c.ladesperre = 0
    return c


def run(c, load, pv, seconds, st=None, soc=50.0, ccl=586.0, t0=0.0):
    """load / pv: W or a function of the time. Returns (st, [(t, out)])"""
    st = st or L.State()
    ac = [0.0] * L.NPH
    res = []
    t = t0
    while t < t0 + seconds:
        ld = load(t) if callable(load) else load
        pw = pv(t) if callable(pv) else pv
        i = L.In()
        i.now, i.t = 1000.0 + t, int(T0 + t)
        i.have_pcc = i.have_pv = 1
        i.inv_time = i.now
        i.pv = pw
        i.pcc = pw - ld - sum(ac)
        i.have_soc_bat1, i.soc_bat1 = 1, 3.0           # Sofar Bat1 empty: no Sofar first
        i.grid_ok = 1
        for p in range(L.NPH):
            i.ac_ok[p], i.ac_in[p] = 1, ac[p]
            i.ac_out_ok[p], i.ac_out[p] = 1, 0.0
        i.bms_ok[0], i.soc[0] = 1, soc
        i.power_ok[0], i.power[0] = 1, sum(ac) * 0.9
        i.volt_ok[0], i.volt[0] = 1, 52.5
        i.lim_ok[0], i.ccl[0], i.dcl[0] = 1, ccl, 586.0
        i.cell_ok[0], i.cell_min[0] = 1, 3.28
        i.cell_max_ok[0], i.cell_max[0] = 1, 3.30
        o = L.bm_step(c, i, st)
        ac = [float(x) for x in o.sp]                  # the Multis follow at once
        res.append((t, o))
        t += L.CYCLE_SECONDS
    return st, res


def active(o):
    return [p for p in range(L.NPH) if o.sp[p] != 0]


c = cfg()
# 1. discharge 400 W: one MultiPlus, the house import goes to ~0
st, r = run(c, 400.0, 0.0, 120)
o = r[-1][1]
check(len(active(o)) == 1 and o.sp[active(o)[0]] < -300, "400 W load: one MultiPlus discharges %s" % o.sp)
check("1PH" in o.why[active(o)[0]], "rule text 1PH: %s" % o.why)
check(not o.split_all, "split_all 0")
# rotation: 0..600 s L1, then L2, then L3, then L1
st, r = run(c, 400.0, 0.0, 1900)
seen = []
for t, o in r:
    a = active(o)
    if a and (not seen or seen[-1][1] != a[0]):
        seen.append((t, a[0]))
check([p for _, p in seen] == [0, 1, 2, 0], "rotation L1 -> L2 -> L3 -> L1: %s" % seen)
check(all(abs(t1 - t0 - 600) <= L.CYCLE_SECONDS for (t0, _), (t1, _) in zip(seen[1:], seen[2:])),
      "every 600 s: %s" % [t for t, _ in seen])
# 2. discharge 2000 W: all three alike
st, r = run(c, 2000.0, 0.0, 120)
o = r[-1][1]
check(len(active(o)) == 3 and max(o.sp) - min(o.sp) <= 2 and "EQ" in o.why[0], "2000 W load: all three EQ %s" % o.sp)
# 3. hysteresis: 2000 W -> 560 W stays all three, 450 W back to one
st, r = run(c, 560.0, 0.0, 120, st=st, t0=120)
check(len(active(r[-1][1])) == 3, "560 W after 2000 W: still all three %s" % r[-1][1].sp)
st, r = run(c, 450.0, 0.0, 120, st=st, t0=240)
check(len(active(r[-1][1])) == 1, "450 W: back to one %s" % r[-1][1].sp)
# 4. charge 400 W surplus: one MultiPlus; 3000 W: all three
st, r = run(c, 300.0, 700.0, 180)
o = r[-1][1]
check(len(active(o)) == 1 and o.sp[active(o)[0]] > 200, "400 W surplus: one MultiPlus charges %s %s" % (o.sp, o.why))
st, r = run(c, 300.0, 3300.0, 180)
o = r[-1][1]
check(len(active(o)) == 3 and min(o.sp) > 800, "3000 W surplus: all three charge %s %s" % (o.sp, o.why))
# 5. Pytes full (SoC 100): no charge
st, r = run(c, 300.0, 3300.0, 60, soc=100.0)
o = r[-1][1]
check(all(x <= 0 for x in o.sp) and "FULL" in o.why[0], "SoC 100: FULL, no charge %s %s" % (o.sp, o.why))
# 6. one phase capped (CCL tiny): the single phase spills to the others
st, r = run(c, 300.0, 700.0, 180, ccl=1.0)
o = r[-1][1]
check(sum(o.sp) <= 200, "CCL 1 A: charge limited to the BMS %s %s" % (o.sp, o.why))
# 7. SINGLE_MAX_W 0: always all three
c0 = cfg()
c0.single_max_w = 0
st, r = run(c0, 400.0, 0.0, 120)
check(len(active(r[-1][1])) == 3, "SINGLE_MAX_W 0: all three %s" % r[-1][1].sp)
print("%d failed" % fails)
sys.exit(1 if fails else 0)
