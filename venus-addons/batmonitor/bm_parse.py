"""
bm_parse.py - the MQTT payloads batmonitor.py reads, a port of c/bm_parse.c (0.48-c).

A value counts only when it is a JSON number, finite and in a plausible range: a missing key, null, a string, true or
1e999 never becomes 0 W. A rejected sample leaves the last good one in place and its timestamp is not refreshed, so
the stale logic in bm_logic takes over. Differences of Python's json to cJSON handled here: bool is an int in Python
(excluded), json.loads accepts NaN / Infinity (excluded by isfinite).
"""
import json
import math

PCC_MAX_KW = 100.0
BAT1_MAX_KW = 20.0
PV_MAX_KW = 60.0
R290_MAX_HZ = 200.0


def _num(o, k, lo, hi):
    """o[k] as a finite JSON number in lo..hi, else None"""
    x = o.get(k)
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    x = float(x)
    if not math.isfinite(x) or x < lo or x > hi:
        return None
    return x


def _object(text):
    try:
        d = json.loads(text)
    except (ValueError, TypeError):
        return None
    return d if isinstance(d, dict) else None


def parse_inverter(text):
    """inverter/power_grid_exchange/json (sofar_fast.py, kW) -> dict in W, None = PCC or Bat1 missing / invalid"""
    d = _object(text)
    if d is None:
        return None
    pcc = _num(d, "ActivePower_PCC_Total", -PCC_MAX_KW, PCC_MAX_KW)
    bat1 = _num(d, "Power_Bat1", -BAT1_MAX_KW, BAT1_MAX_KW)
    if pcc is None or bat1 is None:
        return None
    pcc5 = _num(d, "ActivePower_PCC_Total_avg5", -PCC_MAX_KW, PCC_MAX_KW)
    bat5 = _num(d, "Power_Bat1_avg5", -BAT1_MAX_KW, BAT1_MAX_KW)
    pv1 = _num(d, "Power_PV1", -0.5, PV_MAX_KW)
    pv2 = _num(d, "Power_PV2", -0.5, PV_MAX_KW)
    soc1 = _num(d, "SOC_Bat1", 0.0, 100.0)
    have_pv = pv1 is not None and pv2 is not None
    return {
        "pcc": pcc * 1000.0,
        "bat1": bat1 * 1000.0,
        "pcc_avg5": (pcc5 if pcc5 is not None else pcc) * 1000.0,     # 5 min means fall back to the instant values
        "bat1_avg5": (bat5 if bat5 is not None else bat1) * 1000.0,
        "have_pv": int(have_pv),                                      # both PV values valid, else no night from it
        "pv": (pv1 + pv2) * 1000.0 if have_pv else 0.0,
        "have_soc_bat1": int(soc1 is not None),
        "soc_bat1": soc1 if soc1 is not None else -1.0,
    }


def parse_r290(text):
    """r290/heatpump/all: comp_freq_actual (Hz), None = invalid"""
    d = _object(text)
    v = _num(d, "comp_freq_actual", 0.0, R290_MAX_HZ) if d is not None else None
    return int(v) if v is not None else None


def parse_number(text, lo, hi):
    """plain number topics (em0/power, outdoor temperature): the whole text one number in lo..hi, None = invalid"""
    try:
        x = float(text.strip())
    except (ValueError, AttributeError):
        return None
    if not math.isfinite(x) or x < lo or x > hi:      # float() takes "nan" / "inf": no numbers here
        return None
    return x
