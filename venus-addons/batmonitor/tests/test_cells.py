"""
test_cells.py - 0.79-py: the weakest cell decides the discharge stop (PROT) in the critical range, and the substitute
while the MUST (CAN) BMS is offline (weakest cell = lowest Pytes cell - CELL_AUX_MISSING_OFFSET_V).
usage: python3 tests/test_cells.py        (batmonitor.py part needs python3-dbus and paho, else it is skipped)
"""
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
import bm_logic as L  # noqa: E402

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
    return c


C = cfg()


def prot_step(prot, soc, cell_ok, cell_min):
    x = L.d2_bank_bits(C, 1, soc, 1, -1000.0, 1, 200.0, cell_ok, cell_min)
    return L.keep(L.first_hit(L.TABLES["D2_PROT"], x), prot)


# ---- D2_PROT, values of the night 2026-11-07/08 (Pytes SoC, weakest cell of both stacks) -------------------------
check(C.cell_prot_v == 3.10 and C.cell_prot_release_v == 3.20 and C.cell_prot_soc == 30, "defaults 3.10 / 3.20 / 30")
check(prot_step(0, 17.3, 1, 3.149) == 0, "SoC 17.3 %, cell 3.149 V: discharge")
check(prot_step(0, 15.3, 1, 3.095) == 1, "SoC 15.3 %, cell 3.095 V (02:56): PROT by the cell")
check(prot_step(0, 45.0, 1, 3.050) == 0, "SoC 45 % (outside the critical range), cell 3.05 V: no PROT by the cell")
check(prot_step(1, 12.0, 1, 3.150) == 1, "latched, cell recovers at rest 3.15 V: stays PROT")
check(prot_step(1, 10.0, 1, 3.190) == 1, "latched, SoC 10 %, cell 3.19 V: stays PROT")
check(prot_step(1, 9.0, 1, 3.223) == 1, "latched, cell 3.223 V but SoC 9 % < SOC_MIN_RELEASE: stays PROT")
check(prot_step(1, 10.0, 1, 3.223) == 0, "SoC 10 %, cell 3.223 V (07:36): released")
check(prot_step(0, 7.9, 1, 3.30) == 1, "SoC 7.9 % < SOC_MIN: PROT by the SoC as before")
check(prot_step(1, 12.0, 0, 0.0) == 0, "latched, no cell value at all, SoC 12 %: SoC release only")
check(prot_step(0, 15.0, 0, 0.0) == 0, "no cell value, SoC 15 %: no PROT")

# ---- batmonitor.read_bank: who supplies the weakest cell --------------------------------------------------------
try:
    sys.modules.setdefault("dbus", types.ModuleType("dbus"))
    pm = types.ModuleType("paho")
    pm.mqtt = types.ModuleType("paho.mqtt")
    pm.mqtt.client = types.ModuleType("paho.mqtt.client")
    sys.modules.setdefault("paho", pm)
    sys.modules.setdefault("paho.mqtt", pm.mqtt)
    sys.modules.setdefault("paho.mqtt.client", pm.mqtt.client)
    import batmonitor as B  # noqa: E402
except Exception as e:                      # noqa: BLE001
    print("skip batmonitor.read_bank: %s" % e)
    B = None

if B is not None:
    P, M = L.BM_BANKS[0].service, L.AUX_SERVICE
    logs = []
    B.log = lambda s: logs.append(("info", s))
    B.loge = lambda s: logs.append(("ERROR", s))

    def bank(pytes, must):
        """pytes / must: None = offline, else (soc, min cell or None)"""
        vals = {}
        for svc, v in ((P, pytes), (M, must)):
            if v is None:
                continue
            vals[(svc, "/Connected")] = 1.0
            vals[(svc, "/Soc")] = v[0]
            vals[(svc, "/Dc/0/Voltage")] = 51.0
            vals[(svc, "/Dc/0/Power")] = -500.0
            vals[(svc, "/Info/MaxChargeCurrent")] = 100.0
            vals[(svc, "/Info/MaxDischargeCurrent")] = 100.0
            if v[1] is not None:
                vals[(svc, "/System/MinCellVoltage")] = v[1]
                vals[(svc, "/System/MaxCellVoltage")] = v[1] + 0.03
        return vals

    bm = B.Batmonitor.__new__(B.Batmonitor)
    bm.cfg, bm.master, bm.volt_diff, bm.volt_diff_bad, bm.cell_est = C, None, None, False, False
    bm.hist = [[] for _ in range(L.NBANK)]

    def read(pytes, must):
        vals = bank(pytes, must)
        bm.get = lambda s, p: vals.get((s, p))
        i = L.In()
        bm.read_bank(i)
        return i

    i = read((15.3, 3.123), (19.0, 3.053))
    check(i.cell_ok[0] == 1 and abs(i.cell_min[0] - 3.053) < 1e-9 and not bm.cell_est,
          "both online: weakest cell = MUST 3.053 V (measured)")
    i = read((15.3, 3.160), None)
    check(i.cell_ok[0] == 1 and abs(i.cell_min[0] - 3.090) < 1e-9 and bm.cell_est,
          "MUST offline: substitute 3.160 - 0.070 = 3.090 V")
    check(logs and logs[-1][0] == "ERROR" and "substitute" in logs[-1][1], "MUST offline: ERROR log line once")
    n = len(logs)
    read((15.0, 3.158), None)
    check(len(logs) == n, "MUST still offline: no repeated log line")
    check(prot_step(0, 15.0, i.cell_ok[0], i.cell_min[0]) == 1, "MUST offline: PROT on the substitute 3.090 V")
    i = read((15.3, 3.160), (19.0, None))
    check(bm.cell_est and abs(i.cell_min[0] - 3.090) < 1e-9, "MUST online without cell value: substitute too")
    i = read((15.3, 3.123), (19.0, 3.053))
    check(not bm.cell_est and logs[-1][0] == "info" and "back" in logs[-1][1], "MUST back: measured again, info log")
    i = read(None, (19.0, 3.053))
    check(i.cell_ok[0] == 1 and abs(i.cell_min[0] - 3.053) < 1e-9 and not bm.cell_est and bm.master == M,
          "Pytes offline: MUST master, its weak cell counts directly")
    i = read((15.3, None), (19.0, None))
    check(i.cell_ok[0] == 0 and not bm.cell_est, "no cell values at all: cell_ok 0 (SoC rules only)")

print("%d failed" % fails)
sys.exit(1 if fails else 0)
