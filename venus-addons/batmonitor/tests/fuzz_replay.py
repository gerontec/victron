"""
fuzz_replay.py: runs the Python port (bm_logic.py) on the inputs "fuzz_dump <seed> io" (c/tests/fuzz_dump.c) prints
and writes the result lines in the same format, so `make py-compare` can check C and Python byte for byte.
usage: c/tests/fuzz_dump <seed> io | python3 tests/fuzz_replay.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import bm_logic as L  # noqa: E402

os.environ["TZ"] = "Europe/Berlin"
time.tzset()


def replay_forecast(cfg, f):
    """a FCIN line -> the FC result line of bm_full_forecast"""
    run, k = int(f[0]), int(f[1])
    it = iter(f[2:])
    fi = L.FcIn()
    fi.t = int(next(it))
    fi.have_pv = int(next(it))
    fi.pv_sofar = float(next(it))
    fi.lead = int(next(it))
    fi.corr = float(next(it))
    n_slots = int(next(it))
    fi.slots = [(int(next(it)), float(next(it))) for _ in range(n_slots)]
    for b in range(L.NBANK):
        fi.soc_ok[b] = int(next(it))
        fi.soc[b] = float(next(it))
        fi.has_avg[b] = int(next(it))
        fi.avg[b] = float(next(it))
        fi.volt_ok[b] = int(next(it))
        fi.volt[b] = float(next(it))
    o = L.bm_full_forecast(cfg, fi)
    return "FC %d %d %d %d %.6f %.6f %.6f %.6f %.3f %.6f %d\n" % (
        run, k, o.full_at[0], o.full_at[1], o.soc_sunset[0], o.soc_sunset[1], o.anchor, o.kt_now, o.pv_now_w,
        o.rest_kwh, o.weather)


def main():
    cfg = st = None
    run_prev = -1
    out_lines = []
    for line in sys.stdin:
        if line.startswith("FCIN "):           # fuzz_dump.c prints the forecast before the cycle's result line
            out_lines.insert(len(out_lines) - 2, replay_forecast(cfg, line.split()[1:]))   # before result + D5
            continue
        if not line.startswith("IN "):
            continue
        f = line.split()[1:]
        run, k = int(f[0]), int(f[1])
        if run != run_prev:                       # a new run: fresh cfg and state, as fuzz_dump.c
            cfg = L.Cfg()
            cfg.pi_armed, cfg.forecast = int(f[2]), int(f[3])
            st = L.State()
            run_prev = run
        it = iter(f[4:])
        i = L.In()
        i.now = float(next(it))
        i.t = int(next(it))
        i.have_pcc, i.have_pv = int(next(it)), int(next(it))
        i.pcc, i.pv, i.bat1, i.pcc_avg5, i.bat1_avg5, i.inv_time = (float(next(it)) for _ in range(6))
        i.have_soc_bat1 = int(next(it))
        i.soc_bat1 = float(next(it))
        i.r290_hz = int(next(it))
        i.r290_time, i.aussen, i.aussen_time, i.wp, i.wp_time = (float(next(it)) for _ in range(5))
        i.season = int(next(it))
        i.season_ts = int(next(it))
        i.fc_target = float(next(it))
        i.fc_ts = int(next(it))
        for p in range(L.NPH):
            i.ac_ok[p] = int(next(it))
            i.ac_in[p] = float(next(it))
        for b in range(L.NBANK):
            i.bms_ok[b] = int(next(it))
            i.soc[b] = float(next(it))
            i.power_ok[b] = int(next(it))
            i.power[b] = float(next(it))
            i.volt_ok[b] = int(next(it))
            i.volt[b] = float(next(it))
            i.lim_ok[b] = int(next(it))
            i.ccl[b] = float(next(it))
            i.dcl[b] = float(next(it))
        i.grid_ok = int(next(it))
        for p in range(L.NPH):
            i.ac_out_ok[p] = int(next(it))
            i.ac_out[p] = float(next(it))
        i.fc_corr = float(next(it))
        n_slots = int(next(it))
        i.fc_slots = [(int(next(it)), float(next(it))) for _ in range(n_slots)]
        o = L.bm_step(cfg, i, st)
        out_lines.append("%d %d %d %d %d\t%s\t%s\t%s\t%.3f %.3f %.3f %.3f %d %d %d %d %d %d\n" % (
            run, k, o.sp[0], o.sp[1], o.sp[2], o.why[0], o.why[1], o.why[2], o.wp_eff, o.surplus, o.pi.u, o.pi.y,
            o.pi.sp[0], o.pi.sp[1], o.pi.sp[2], o.fc_active, o.fc_bad, o.bat1_first))
        out_lines.append("D5 %d %d %d\n" % (run, k, o.do4_pulse))
    sys.stdout.writelines(out_lines)


if __name__ == "__main__":
    main()
