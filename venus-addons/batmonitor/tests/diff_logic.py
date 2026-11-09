"""
diff_logic.py - differential test of two bm_logic.py versions (each with its own tables next to it): the same random
cycles through both bm_step, every setpoint, rule text, DO4 / force_full / split result and the whole state compared.
Values are drawn around the thresholds, so every predicate flips often.
usage: python3 tests/diff_logic.py <dir of the old bm_logic.py> [<dir of the new one, default: ..>] [runs] [cycles]
"""
import dataclasses
import importlib.util
import os
import random
import sys
import time

os.environ["TZ"] = "Europe/Berlin"
time.tzset()
HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


def load(d, name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(d, "bm_logic.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


sys.path.insert(0, HERE)
from bm_selftest import make_in, site_cfg  # noqa: E402  (the same generator as the Pi self-test)


def main():
    old_dir = sys.argv[1]
    new_dir = sys.argv[2] if len(sys.argv) > 2 else HERE
    runs = int(sys.argv[3]) if len(sys.argv) > 3 else 5
    cycles = int(sys.argv[4]) if len(sys.argv) > 4 else 3000
    A, B = load(old_dir, "bm_old"), load(new_dir, "bm_new")
    ca, cb = site_cfg(A), site_cfg(B)
    ca.ladesperre = cb.ladesperre = 1
    diffs = 0
    for run in range(runs):
        r = random.Random(1000 + run)
        t0 = int(time.mktime((2026, r.randint(1, 12), r.randint(1, 28), r.randint(0, 23), 0, 0, 0, 0, -1)))
        sa, sb = A.State(), B.State()
        now, sp = 1000.0, [0] * 3
        for k in range(cycles):
            now += r.choice((5, 5, 5, 30, 300))
            seed = r.random()
            ia = make_in(A, random.Random(seed), now, t0 + int(now), sp)
            ib = make_in(B, random.Random(seed), now, t0 + int(now), sp)
            oa, ob = A.bm_step(ca, ia, sa), B.bm_step(cb, ib, sb)
            # the state fields both versions have (0.80-py: a new latch is no difference by itself)
            da, db = dataclasses.asdict(sa), dataclasses.asdict(sb)
            common = sorted(set(da) & set(db))
            got_a = (oa.sp, oa.why, oa.do4_pulse, oa.ff_done, oa.split_all, oa.rot, oa.d1_id, oa.surplus,
                     [da[f] for f in common])
            got_b = (ob.sp, ob.why, ob.do4_pulse, ob.ff_done, ob.split_all, ob.rot, ob.d1_id, ob.surplus,
                     [db[f] for f in common])
            if repr(got_a) != repr(got_b):
                diffs += 1
                if diffs <= 5:
                    print("DIFF run %d cycle %d\n  old %r\n  new %r" % (run, k, got_a[:8], got_b[:8]))
            sp = list(oa.sp)
    print("%d runs x %d cycles: %d differences" % (runs, cycles, diffs))
    return 1 if diffs else 0


if __name__ == "__main__":
    sys.exit(main())
