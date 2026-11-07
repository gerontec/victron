"""
bm_selftest.py - smoke test of a bm_logic module with its decision tables (0.78-py, live DMN editor):
random cycles with values around the thresholds through bm_step. A table that names a column the chain code does
not supply, an inputExpression with an unknown input, an output word the code cannot use: each raises here, before
the file goes live. host/bm_params_pull.py runs it on the Pi on every DMN version from the editor;
tests/diff_logic.py uses the same input generator.
usage: python3 bm_selftest.py [cycles]       (the bm_logic.py / bm_decisions.dmn next to this file)
"""
import os
import random
import sys
import time


def site_cfg(m):
    c = m.Cfg()
    for i, p in enumerate(m.BM_PARAMS):
        if p.name in m.SITE_PARAMS:
            c.set_param(i, m.SITE_PARAMS[p.name])
    return c


def near(r, points, lo, hi):
    """a value near one of the thresholds (or anywhere in lo..hi)"""
    if r.random() < 0.7:
        return r.choice(points) + r.choice((-1, 0, 1)) * r.choice((0.001, 0.5, 3.0))
    return r.uniform(lo, hi)


def make_in(m, r, now, t, prev_sp):
    i = m.In()
    i.now, i.t = now, t
    i.have_pcc, i.have_pv = r.random() < 0.95, r.random() < 0.95
    i.inv_time = now - (r.choice((0, 2, 10, 200)) if r.random() < 0.9 else 0) if r.random() < 0.97 else 0.0
    i.pcc = near(r, [-100, 20, 200, 18200, 20000, 23000], -15000, 25000)
    i.pv = near(r, [100], 0, 20000)
    i.bat1 = near(r, [-100, 0], -2500, 2500)
    i.pcc_avg5, i.bat1_avg5 = i.pcc + r.uniform(-300, 300), i.bat1
    i.have_soc_bat1 = r.random() < 0.9
    i.soc_bat1 = near(r, [5, 7], 0, 100)
    i.wp_time = now - r.choice((1, 100, 200)) if r.random() < 0.9 else 0.0
    i.wp = near(r, [300], 0, 6000)
    i.aussen_time, i.aussen = now - 10, r.uniform(-10, 30)
    i.season, i.season_ts = r.choice((0, 1, 2, 3)), t - r.choice((100, 300000))
    i.fc_target, i.fc_ts = r.choice((-1, 50, 100)), t - 100
    i.grid_ok = int(r.random() < 0.95)
    for p in range(m.NPH):
        i.ac_ok[p] = int(r.random() < 0.97)
        i.ac_in[p] = prev_sp[p] + r.uniform(-60, 60)
        i.ac_out_ok[p] = int(r.random() < 0.97)
        i.ac_out[p] = near(r, [0, 30, 99, -5000], -6000, 3000) / 3
    for b in range(m.NBANK):
        i.bms_ok[b] = int(r.random() < 0.97)
        i.soc[b] = max(0.0, min(100.0, near(r, [0, 5, 8, 10, 100], 0, 100)))
        i.power_ok[b] = int(r.random() < 0.95)
        i.power[b] = near(r, [200], -8000, 8000)
        i.volt_ok[b], i.volt[b] = int(r.random() < 0.95), r.uniform(48, 57)
        i.lim_ok[b] = int(r.random() < 0.95)
        i.ccl[b], i.dcl[b] = near(r, [0, 1, 100], 0, 600), near(r, [0, 1, 100], 0, 600)
        i.cell_ok[b], i.cell_min[b] = int(r.random() < 0.95), near(r, [3.05, 3.2], 2.8, 3.5)
        i.cell_max_ok[b], i.cell_max[b] = int(r.random() < 0.95), near(r, [3.45], 3.2, 3.65)
    i.force_full = r.choice((0, 0, 0, 1, 2))
    return i


def run(m, cycles=600, seed=1):
    """bm_step over `cycles` random cycles in four seasons; raises on the first error. Returns the modes seen."""
    cfg = site_cfg(m)
    modes = set()
    for run_no, month in enumerate((1, 4, 7, 10)):
        r = random.Random(seed * 100 + run_no)
        t0 = int(time.mktime((2026, month, 15, 0, 0, 0, 0, 0, -1)))
        st, now, sp = m.State(), 1000.0, [0] * m.NPH
        for _ in range(cycles // 4):
            now += r.choice((5, 5, 5, 30, 300))
            o = m.bm_step(cfg, make_in(m, r, now, t0 + int(now), sp), st)
            sp = list(o.sp)
            modes.update(w.split(" ")[0].split("(")[0] for w in o.why)
    return modes


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import bm_logic
    print("ok, modes seen: %s" % " ".join(sorted(run(bm_logic, int(sys.argv[1]) if len(sys.argv) > 1 else 600))))
