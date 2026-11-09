"""
bm_logic.py - the batmonitor control logic; the decisions D1-D7 as decision tables (first_hit, bm_tables.h).

0.77-py (user 2026-11-07): both 300 Ah stacks are physically parallel now, one DC bus of 600 Ah behind all three
MultiPlus. One bank, the Pytes BMS (EBox) is the master: its SoC and its CCL / DCL, charged only up to the Pytes limit
(no MUST top hold, no balancing lead, no charge priority). Split of one amount over the phases: up to SINGLE_MAX_W
only one MultiPlus works (charge and discharge), the next one every ROTATE_S; above it all three alike.

One cycle = bm_step(cfg, inp, st) -> Out. Pure: no D-Bus, MQTT, clock or files; batmonitor.py collects the inputs and
writes the outputs. The names, stages and tables are the ones of the C file, so both can be read side by side:

    inputs -> D1 (Sofar first, S3 source, winter heat pump rule) -> S0 caps -> D2 mode
           -> mode matrix BM_MODE_RULE -> charge path (D4 charge block, FORCE, charge amount, split 1PH / EQ)
                                                -> discharge branch (BM_SRC_RULE, D3, discharge amount, split 1PH / EQ)
           -> D5 DO4 pulse -> PI shadow

Since 2026-11-05 only this Python version is maintained; c/ is dead (the C helpers below - truncating divisions,
lround, fmin/fmax with NaN, 0-based tm_yday - stay so the numbers are the ones of before).
"""
import ast
import math
import os
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

NPH = 3
NBANK = 1
CYCLE_SECONDS = 5
WHY_LEN = 96
BM_SUMMER, BM_WINTER, BM_TRANSITION = 1, 2, 3
BM_NOMINAL_V = 51.2            # 16S LFP, for BMS current limits without a voltage
BM_FF_HOLD = 1                 # Out.ff_done: held 100 % SoC for FORCE_FULL_HOLD_MIN (0.62-c)
BM_FF_SPREAD = 2               # cell spread down to FORCE_FULL_SPREAD_MV for FF_SPREAD_STABLE_S (0.67-c)
BM_FF_MAX = 3                  # FORCE_FULL_MAX_MIN after the command (0.67-c)
FF_SPREAD_STABLE_S = 60

# soyo, 1:1 from sofar_waveshare.yaml, power values doubled (POWER_SCALE)
POWER_SCALE = 2
KP_PM = 1010                   # proportional gain in per mille: the amounts are whole watts (0.45-c / 0.48-c)
KP = KP_PM / 1000.0
B_DAY_IDLE = 10 * POWER_SCALE
PCC_IMPORT_TH = -100.0
PCC_SURPLUS_TH = 200.0
SOYO_HOLD_TH = 20.0            # W: charging / proportional discharging continues while surplus / deficit is above
CHARGING_TH = 200.0
NIGHT_PV_TH = 100.0
SUMMER_FROM, SUMMER_TO = 5, 9
STALE_SECONDS = 180.0
WP_MAX_AGE = 150.0             # s, older: the heat pump counts as off (B1FIRST, winter heat pump rule)
WP_ON_TH = 300.0               # W: heat pump counts as running (no night floor)
SEASON_MAX_AGE = 2 * 86400     # s: older batmonitor/season -> month rule
BAT1_FIRST_HYST = 2.0          # %: Sofar-first back on above BAT1_SOC_MIN + this
WINTER_FROM, WINTER_TO = 10, 3 # winter half-year October..March: the heat pump rule (0.61-c)
B1_DIS_TH = 100.0              # W: the Sofar Bat1 counts as discharging below -this (0.61-c)
BAT1_CHARGE_FACTOR = 0.5
# charge block, 1:1 from waveshare/fox2db_logic.h (fox2db v2.9) and sofar_waveshare.yaml
LADESPERRE_FROM, LADESPERRE_TO = 5, 8
LADESPERRE_RATIO = 0.50
LADESPERRE_HYST = 0.25
LADESPERRE_NOW_RATIO = 0.20
PCC_PEAK_TH = 20000.0
DC_RATIO_MIN = 5000.0
AUSSEN_MAX_AGE = 900.0
LAT, LON = 47.6811, 11.5732
HOR_AZ_SPLIT, HOR_EAST, HOR_SOUTH, HOR_DIFFUSE = 120.0, 23.0, 15.0, 0.25
TEMP_COEFF, NOCT, T_STC = -0.0030, 45.0, 25.0
# PI prototype: u(k) = u_applied(k-1) + KP*(e(k) - e(k-1)) + KI*dt*e(k), velocity form
PI_TARGET, PI_DEADBAND, PI_KP, PI_KI = 0.0, 75.0, 0.2, 0.06
# full_at forecast (0.28-c)
FC_STEP = 300
FC_ANCHOR_TAU = 5400.0
FC_ANCHOR_MIN_W = 500.0
FC_SLOT_REACH = 5400
FC_CHARGE_EFF = 0.90
BM_FC_SLOTS = 24
# D5 DO4 pulse (0.46-c)
DO4_LOCKOUT = 300.0
DO4_PCC_MAX_AGE = 15.0
DO4_PULSE_S = 3.0

BM_PH = ["L1", "L2", "L3"]


@dataclass(frozen=True)
class Bank:
    name: str
    service: str
    phases: tuple
    capacity_wh: float


# 0.77-py: Stack1 MUST + Stack2 Pytes parallel = one 600 Ah bank on L1 + L2 + L3; the Pytes BMS (EBox) is the master.
# The MUST BMS (can0) still reports: its power and cells count too, and it is the master while the Pytes BMS is
# missing (batmonitor.py read_bank, user 2026-11-07: "wenn eines der beiden BMS ausfaellt, ist immer ein Fallback da")
BM_BANKS = [
    Bank("BATTERY", "com.victronenergy.battery.ebox", (0, 1, 2), 600 * 51.2),
]
AUX_SERVICE = "com.victronenergy.battery.socketcan_can0"   # also the fallback master while the Pytes BMS is missing

# fitted per string (gen_pv_strings.py, 14 clear days): Sofar PV1 west / PV2 south, FoxESS pv2 east / pv1 west
ARRAYS = [(60, 33, 19430), (68, -12, 7690)]           # (tilt, azimuth from south, W)
ARRAYS_EAST = [(59, -29, 22036), (67, 32, 2781)]
KT_MONTH = [0, .331, .402, .563, .838, .909, .880, .840, .820, .760, .600, .350, .134]


# ---- C arithmetic, so this port gives the same numbers as bm_logic.c -------------------------------------------

def c_div(a, b):
    """C integer division: truncates toward zero (Python // floors)"""
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b >= 0) else -q


def lround(v):
    """C lround: half away from zero (Python round() rounds half to even)"""
    t = math.trunc(v)
    if abs(v - t) >= 0.5:
        t += 1 if v > 0 else -1
    return int(t)


def fmin(a, b):
    """C fmin: a NaN argument loses"""
    if a != a:
        return b
    if b != b:
        return a
    return a if a < b else b


def fmax(a, b):
    if a != a:
        return b
    if b != b:
        return a
    return a if a > b else b


def watt(v):
    """whole watts from a measurement or ceiling: NaN -> 0, clamped to +-1 MW"""
    if v != v:
        return 0
    return 1000000 if v > 1e6 else -1000000 if v < -1e6 else lround(v)


def localtime(t):
    """(month 1..12, yday 0..365 as C tm_yday, hour)"""
    lt = time.localtime(t)
    return lt.tm_mon, lt.tm_yday - 1, lt.tm_hour


def bank_of(p):
    for b, bank in enumerate(BM_BANKS):
        if p in bank.phases:
            return b
    return -1


# ---- parameters: name (env BATMONITOR_<name>), default, allowed range, unit, meaning ---------------------------

@dataclass(frozen=True)
class Param:
    name: str
    field: str
    default: float
    min: float
    max: float
    unit: str
    help: str


BM_PARAMS = [
    Param("W_MAX", "w_max", 10000, 0, 12000, "W", "total discharge (soyo was 1800 W)"),
    Param("DISCHARGE_MAX_PHASE", "discharge_max_phase", 3800, 0, 5000, "W", "discharge per phase"),
    Param("CHARGE_MAX_PHASE", "charge_max_phase", 4900, 0, 5000, "W", "PV charge per phase (5000 VA peaks)"),
    Param("CHARGER_CAP_PHASE", "charger_cap_phase", 4200, 0, 5000, "W", "real charger capacity per phase, priority fill"),
    Param("SOC_MIN", "soc_min", 5, 0, 50, "%", "no discharge below"),
    Param("SOC_MIN_RELEASE", "soc_min_release", 7, 0, 60, "%", "discharge again from"),
    Param("SOC_FORCE", "soc_force", 3, 0, 30, "%", "grid force charge below"),
    Param("SOC_FORCE_RELEASE", "soc_force_release", 5, 0, 40, "%", "force charge until"),
    Param("FORCE_CHARGE_W", "force_charge_w", 500, 0, 4000, "W", "force charge per phase"),
    Param("DCL_FORCE_A", "dcl_force_a", 1, 0, 50, "A", "force charge (LOW) while the BMS discharge limit is below this, no discharge (0 = off)"),
    Param("CELL_FORCE_V", "cell_force_v", 3.0, 0, 3.4, "V", "force charge (LOW) while the lowest cell is below this, no discharge (0 = off)"),
    Param("CELL_FORCE_RELEASE_V", "cell_force_release_v", 3.25, 0, 3.5, "V", "LOW ends once the lowest cell is at least this and DCL is back"),
    Param("CELL_PROT_V", "cell_prot_v", 3.10, 0, 3.4, "V", "0.79-py: no discharge (PROT) while the weakest cell of both stacks is below this and the SoC is below CELL_PROT_SOC (0 = off)"),
    Param("CELL_PROT_RELEASE_V", "cell_prot_release_v", 3.20, 0, 3.5, "V", "0.79-py: PROT ends only once the weakest cell is at least this (and the SoC at least SOC_MIN_RELEASE)"),
    Param("CELL_PROT_SOC", "cell_prot_soc", 30, 0, 101, "%", "0.79-py: the critical range: the cell rule of PROT starts only below this SoC (101 = always)"),
    Param("CELL_AUX_MISSING_OFFSET_V", "cell_aux_missing_offset_v", 0.07, 0, 0.3, "V", "0.79-py: MUST (CAN) BMS offline, Pytes present: weakest cell = lowest Pytes cell minus this (the weak cell sits in the MUST stack; measured 2026-11-07/08 up to 71 mV lower at SoC < 20 % under load)"),
    Param("SOYO_TARGET", "soyo_target", 0, -500, 500, "W", "PCC target (+ = export)"),
    Param("CHARGE_RESERVE", "charge_reserve", 0, 0, 1000, "W", "charge: this much less into the stacks than the PCC surplus (0.72-c, user 2026-10-29: 100 W)"),
    Param("SOFAR_TRICKLE", "sofar_trickle", 30, 0, 500, "W", "night: stacks give this much more than the house needs"),
    Param("CHARGER_A", "charger_a", 70, 0, 70, "A", "MultiPlus-II charger current per unit (DC), ceiling = this x BMS voltage"),
    Param("CHARGE_EFF", "charge_eff", 0.93, 0.7, 1.0, "", "AC-in per DC at the charger limit"),
    Param("BAT1_SOC_MIN", "bat1_soc_min", 5, 0, 101, "%", "night: the Sofar Bat1 serves house + heat pump first down to this SoC; 101 = off"),
    Param("EXPORT_CAP", "export_cap", 18000, 0, 30000, "W", "summer peak window: while the forecast expects export above this, the stacks charge only the export above it (0 = off)"),
    Param("HOUSE_EST", "house_est", 1000, 0, 5000, "W", "summer peak window: house load assumed when turning the PV forecast into export"),
    Param("DO4_GRACE_S", "do4_grace_s", 120, 0, 600, "s", "DO4 (FoxESS shedding) only after the PCC stays above 20 kW this long: R290 boost and air conditioning first"),
    Param("DO4_HARD_W", "do4_hard_w", 23000, 20000, 40000, "W", "DO4 at once above this PCC export"),
    Param("ACOUT_FEED_MAX_W", "acout_feed_max_w", 5000, 0, 10000, "W", "grid offline: DO4 when PV feeds more than this into one MultiPlus AC-out (factor 1.0 rule, 5000 VA; 0 = off)"),
    Param("ACOUT_FEED_S", "acout_feed_s", 10, 0, 120, "s", "... for this long"),
    Param("CHARGER_DC_W", "charger_dc_w", 3600, 500, 5000, "W", "full_at forecast: DC at the BMS per MultiPlus at its limit"),
    Param("WP_WINTER_W", "wp_winter_w", 1000, 0, 12000, "W", "winter half-year (October..March), heat pump running: the stacks discharge nothing while the Sofar Bat1 discharges, else at most this (heat pump off: PCC near 0)"),
    Param("WP_PEAK_ON_W", "wp_peak_on_w", 5000, 0, 30000, "W", "0.80-py (user 2026-11-08): heat pump running and the grid import without the own discharge above this: the stacks hold the import at this (PCC = -this), in any season (30000 = off)"),
    Param("WP_PEAK_OFF_W", "wp_peak_off_w", 4500, 0, 30000, "W", "0.80-py: ... until that import falls below this (hysteresis) or the heat pump stops"),
    Param("WP_PEAK_W", "wp_peak_w", 1000, 0, 12000, "W", "0.80-py: at most this discharge for the heat pump peak rule (user: only 1000 W)"),
    Param("FORCE_FULL_HOLD_MIN", "force_full_hold_min", 60, 0, 1440, "min", "force_full_from_pv: the command ends once its stack held 100 % SoC this long (BMS cell balancing)"),
    Param("FORCE_FULL_MAX_MIN", "force_full_max_min", 240, 0, 1440, "min", "force_full_from_pv: the command ends at the latest this long after it came, also below 100 % SoC (0 = off)"),
    Param("FORCE_FULL_SPREAD_MV", "force_full_spread_mv", 30, 0, 500, "mV", "force_full_from_pv: the command ends once the cell spread of its stack stayed at or below this for 60 s with the highest cell at FORCE_FULL_TOP_V (0 = off)"),
    Param("FORCE_FULL_TOP_V", "force_full_top_v", 3.45, 3.0, 3.7, "V", "... the highest cell at least this: the spread counts only at the top of the charge, not at rest"),
    Param("HOUSE_ACOUT", "house_acout", 2, 0, 2, "", "house on AC-out1 (0.69-c): winter Z2 near 0 (discharge = AC-out1 load), summer Z1 near 0 (PCC, at least the AC-out1 load); 0 = never (0.68-c matrix, house on the grid), 1 = always, 2 = auto (AC-out total > 200 W for 60 s, off after 120 s < 100 W)"),
    Param("HOUSE_WP_EXTRA_W", "house_wp_extra_w", 40, 0, 2000, "W", "0.81-py (user 2026-11-09): house on AC-out1, winter, heat pump running: discharge this much above the AC-out1 load for the heat pump"),

    Param("SINGLE_MAX_W", "single_max_w", 600, 0, 5000, "W", "0.77-py: up to this total (charge or discharge) only one MultiPlus works, above it all three alike (0 = always all three)"),
    Param("SINGLE_HYST_W", "single_hyst_w", 100, 0, 1000, "W", "0.77-py: back to one MultiPlus only below SINGLE_MAX_W minus this"),
    Param("ROTATE_S", "rotate_s", 600, 10, 86400, "s", "0.77-py: the single MultiPlus changes to the next phase this often (L1 -> L2 -> L3)"),
]


class Cfg:
    """bm_cfg: the flags and every BM_PARAMS value as an attribute of the same name (cfg.w_max, ...)"""

    def __init__(self):
        self.ladesperre = 1    # BATMONITOR_LADESPERRE (default on)
        self.pi_armed = 0      # BATMONITOR_PI=1: the PI replaces the soyo amounts
        for p in BM_PARAMS:
            setattr(self, p.field, float(p.default))

    def set_param(self, i, v):
        """0 = set, -1 = outside min..max (unchanged), as bm_param_set"""
        p = BM_PARAMS[i]
        if not (p.min <= v <= p.max):
            return -1
        setattr(self, p.field, float(v))
        return 0


@dataclass
class In:
    """bm_in: everything one cycle reads; times are monotonic seconds like now, 0 = never received"""
    now: float = 0.0
    t: int = 0                                  # unix time; local month/hour via TZ (Europe/Berlin)
    have_pcc: int = 0
    have_pv: int = 0
    pcc: float = 0.0                            # W, + = export
    pv: float = 0.0
    bat1: float = 0.0                           # W, + = the Sofar battery charges
    pcc_avg5: float = 0.0
    bat1_avg5: float = 0.0
    inv_time: float = 0.0
    have_soc_bat1: int = 0
    soc_bat1: float = 0.0
    r290_hz: int = 0
    r290_time: float = 0.0
    aussen: float = 0.0
    aussen_time: float = 0.0
    wp: float = 0.0                             # SDM72D heat pump W
    wp_time: float = 0.0
    season: int = 0                             # batmonitor/season: 0 none, BM_SUMMER / WINTER / TRANSITION
    season_ts: int = 0
    fc_target: float = 0.0                      # batmonitor/forecast target_soc %, < 0 = none
    fc_ts: int = 0
    fc_corr: float = 0.0                        # forecast.py correction, <= 0 = none (0.49-c)
    fc_slots: list = field(default_factory=list)                 # [(unix t, kt)] OWM 3 h slots (kt_slots)
    ac_ok: list = field(default_factory=lambda: [0] * NPH)
    ac_in: list = field(default_factory=lambda: [0.0] * NPH)     # vebus /Ac/ActiveIn/Lx/P, + = the Multi takes
    grid_ok: int = 0                            # vebus /Ac/ActiveIn/Connected == 1: grid there (0.51-c)
    ac_out_ok: list = field(default_factory=lambda: [0] * NPH)
    ac_out: list = field(default_factory=lambda: [0.0] * NPH)
    bms_ok: list = field(default_factory=lambda: [0] * NBANK)    # Connected == 1 and /Soc valid
    soc: list = field(default_factory=lambda: [0.0] * NBANK)
    power_ok: list = field(default_factory=lambda: [0] * NBANK)
    power: list = field(default_factory=lambda: [0.0] * NBANK)   # BMS /Dc/0/Power, + = charge
    volt_ok: list = field(default_factory=lambda: [0] * NBANK)
    volt: list = field(default_factory=lambda: [0.0] * NBANK)
    lim_ok: list = field(default_factory=lambda: [0] * NBANK)    # the BMS sends CCL / DCL
    ccl: list = field(default_factory=lambda: [0.0] * NBANK)     # A DC
    dcl: list = field(default_factory=lambda: [0.0] * NBANK)
    cell_ok: list = field(default_factory=lambda: [0] * NBANK)
    cell_min: list = field(default_factory=lambda: [0.0] * NBANK)  # BMS /System/MinCellVoltage V (0.52-c)
    force_full: int = 0                         # batmonitor/force_full_from_pv (0.62-c): 0 off, 1 / 2 = on (S1 / S2, one bank since 0.77)
    cell_max_ok: list = field(default_factory=lambda: [0] * NBANK)
    cell_max: list = field(default_factory=lambda: [0.0] * NBANK)  # BMS /System/MaxCellVoltage V (0.67-c)


@dataclass
class State:
    """bm_state: what survives from cycle to cycle"""
    prot: list = field(default_factory=lambda: [0] * NBANK)
    force: list = field(default_factory=lambda: [0] * NBANK)
    low: list = field(default_factory=lambda: [0] * NBANK)       # LOW force charge latched (0.52-c)
    soc_ok: list = field(default_factory=lambda: [0] * NBANK)
    soc: list = field(default_factory=lambda: [0.0] * NBANK)
    soyo_chg_prev: int = 0
    soyo_prop_prev: int = 0
    ls_yday: int = -1
    peak_today: int = 0                         # PCC > 20 kW seen today (display)
    bat1_first: int = 0
    do4_last: float = 0.0
    do4_over_since: float = 0.0                 # monotonic s since the PCC is above PCC_PEAK_TH, 0 = not (0.50-c)
    acout_over_since: float = 0.0               # monotonic s since an AC-out feeds above ACOUT_FEED_MAX_W (0.51-c)
    pi_e_prev: float = 0.0
    pi_t_prev: float = 0.0
    ff_full_since: float = 0.0                  # monotonic s since the force_full stack is at 100 %, 0 = not (0.62-c)
    ff_since: float = 0.0                       # monotonic s since the force_full command is on, 0 = off (0.67-c)
    ff_spread_since: float = 0.0                # monotonic s since its cell spread is down at the top, 0 = not
    house_on: int = 0                           # D7: the house hangs on AC-out1 (0.69-c)
    wp_peak: int = 0                            # D1_WP_PEAK latch (0.80-py)
    house_over_since: float = 0.0               # monotonic s since the AC-out total is above HOUSE_ON_W, 0 = not (0.81-py)
    house_under_since: float = 0.0              # monotonic s since the AC-out total is below HOUSE_OFF_W, 0 = not
    rot: int = 0                                # 0.77-py: phase of the single MultiPlus
    rot_since: float = 0.0                      # monotonic s since it is this phase, 0 = not started
    split_all: int = 0                          # 1 = above SINGLE_MAX_W: all three MultiPlus
    setpoints: list = field(default_factory=lambda: [0] * NPH)


@dataclass
class Ls:
    active: int = 0                             # waiting below the cap (block)
    cap: int = 0                                # charge only the export above EXPORT_CAP
    peak_h: int = 0
    win_end_h: int = 0
    dc: float = 0.0
    ratio: float = 0.0
    ratio_now: float = 0.0
    noon_h: float = 0.0


@dataclass
class Pi:
    y: float = 0.0
    e: float = 0.0
    applied: float = 0.0
    u: float = 0.0
    u_min: float = 0.0
    u_max: float = 0.0
    sp: list = field(default_factory=lambda: [0] * NPH)


@dataclass
class Out:
    """bm_out: the result of one cycle"""
    sp: list = field(default_factory=lambda: [0] * NPH)          # W per phase, + = charge
    why: list = field(default_factory=lambda: [""] * NPH)        # rule text per phase
    surplus: float = 0.0
    season: int = 0
    season_measured: int = 0
    bat1_first: int = 0
    wp_max_w: int = 0                           # winter heat pump rule (0.61-c): discharge ceiling W, -1 = none
    d1_id: int = 0                              # bm_d1_id: the D1 cell of this cycle, 0..255 (0.60-c)
    src_why: str = ""
    ls: Ls = field(default_factory=Ls)
    pi: Pi = field(default_factory=Pi)
    do4_pulse: int = 0
    do4_acout: int = 0                          # ... because of the AC-out feed (0.51-c), else the PCC
    acout_feed_w: float = 0.0                   # largest PV feed into one AC-out this cycle (W, >= 0)
    chg_cap: list = field(default_factory=lambda: [0.0] * NPH)
    dis_cap: list = field(default_factory=lambda: [0.0] * NPH)
    ccl_bind: list = field(default_factory=lambda: [0] * NPH)
    dcl_bind: list = field(default_factory=lambda: [0] * NPH)
    ff_done: int = 0                            # end the force_full command (0.62-c): BM_FF_HOLD, _SPREAD, _MAX, 0 = no
    house: int = 0                              # D7 this cycle: 1 = house on AC-out1, the Z1 / Z2 rule (0.69-c)
    house_w: int = 0                            # AC-out1 load W (sum of the positive phases)
    wp_peak: int = 0                            # 0.80-py: heat pump peak rule on, discharge WP_PEAK_W
    import_gross: int = 0                       # 0.80-py: grid import W without the own discharge / charge
    rot: int = 0                                # 0.77-py: phase of the single MultiPlus
    split_all: int = 0                          # 1 = all three MultiPlus this cycle


# ---- clear-sky DC model (fox2db_logic.h) ---------------------------------------------------------------------

def d2r(d):
    return d * math.pi / 180.0


def bm_sun_pos(t):
    """NOAA sun position: (elevation, azimuth from north) for unix UTC"""
    g = time.gmtime(t)
    hour = g.tm_hour + g.tm_min / 60.0 + g.tm_sec / 3600.0
    gamma = 2.0 * math.pi / 365.0 * ((g.tm_yday - 1) + (hour - 12) / 24.0)
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(gamma) - 0.032077 * math.sin(gamma)
                       - 0.014615 * math.cos(2 * gamma) - 0.040849 * math.sin(2 * gamma))
    decl = (0.006918 - 0.399912 * math.cos(gamma) + 0.070257 * math.sin(gamma)
            - 0.006758 * math.cos(2 * gamma) + 0.000907 * math.sin(2 * gamma)
            - 0.002697 * math.cos(3 * gamma) + 0.00148 * math.sin(3 * gamma))
    har = d2r((hour * 60.0 + eqtime + 4.0 * LON) / 4.0 - 180.0)
    latr = d2r(LAT)
    cosz = math.sin(latr) * math.sin(decl) + math.cos(latr) * math.cos(decl) * math.cos(har)
    cosz = fmax(-1.0, fmin(1.0, cosz))
    elev = 90.0 - math.acos(cosz) * 180.0 / math.pi
    az_s = math.atan2(math.sin(har), math.cos(har) * math.sin(latr) - math.tan(decl) * math.cos(latr))
    az = math.fmod(az_s * 180.0 / math.pi + 180.0 + 360.0, 360.0)
    return elev, az


def calc_arrays_kt(arrays, t, kt):
    elev, az = bm_sun_pos(t)
    if elev <= 0:
        return 0.0
    am = fmin(1.0 / math.sin(d2r(elev)), 37.0)
    tr = math.pow(0.7, math.pow(am, 0.678))
    shade = 1.0 if elev >= (HOR_EAST if az < HOR_AZ_SPLIT else HOR_SOUTH) else HOR_DIFFUSE
    e = d2r(elev)
    s = 0.0
    for tilt, az_s, power in arrays:
        b = d2r(tilt)
        da = d2r((az - 180.0) - az_s)
        s += power * tr * kt * fmax(0.0, math.sin(e) * math.cos(b) + math.cos(e) * math.sin(b) * math.cos(da))
    return s * shade


def calc_arrays(arrays, t, month):
    return calc_arrays_kt(arrays, t, KT_MONTH[month] if 1 <= month <= 12 else 0.60)


def bm_dc_now(t, month):
    return calc_arrays(ARRAYS, t, month) + calc_arrays(ARRAYS_EAST, t, month)


def solar_noon_utc(ref):
    midnight = (ref // 86400) * 86400
    tmid = midnight + 12 * 3600
    gamma = 2.0 * math.pi / 365.0 * (time.gmtime(tmid).tm_yday - 1)
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(gamma) - 0.032077 * math.sin(gamma)
                       - 0.014615 * math.cos(2 * gamma) - 0.040849 * math.sin(2 * gamma))
    return midnight + int((720.0 - eqtime - 4.0 * LON) / 60.0 * 3600.0)


def dc_temp_factor(elev, ambient):
    cell = ambient + (NOCT - 20.0) / 800.0 * 1000.0 * fmax(0.0, math.sin(d2r(elev)))
    return fmax(0.85, fmin(1.0, 1.0 + TEMP_COEFF * (cell - T_STC)))


# ---- ceilings and the split core -----------------------------------------------------------------------------

def bm_charge_cap(cfg, volt_ok, volt):
    """charge ceiling of one phase (W AC): charger_a x BMS voltage / charge_eff, at most CHARGE_MAX_PHASE;
    without a plausible voltage CHARGER_CAP_PHASE"""
    if not volt_ok or volt < 40 or volt > 62:
        return cfg.charger_cap_phase
    return fmin(cfg.charge_max_phase, cfg.charger_a * volt / cfg.charge_eff)


def bm_bms_cap_phase(cfg, bank, volt_ok, volt, amps, charge):
    """BMS current limit as W AC per phase: CCL x U / charge_eff, DCL x U x charge_eff, split over the bank's phases"""
    u = volt if (volt_ok and 40 <= volt <= 62) else BM_NOMINAL_V
    w = fmax(0.0, amps) * u
    return (w / cfg.charge_eff if charge else w * cfg.charge_eff) / len(BM_BANKS[bank].phases)


def water_fill(w, on, cap):
    """w (whole watts) alike over the phases with on[p], what a phase cannot take above cap[p] goes to the others
    alike (at most NPH rounds). Returns give per phase."""
    give = [0] * NPH
    n = sum(1 for p in range(NPH) if on[p])
    if not n or w <= 0:
        return give
    want = [c_div(w, n) if on[p] else 0 for p in range(NPH)]
    for _ in range(NPH):
        over = n_free = 0
        for p in range(NPH):
            if on[p]:
                if want[p] > cap[p]:
                    over += want[p] - cap[p]
                    want[p] = cap[p]
                elif want[p] < cap[p]:
                    n_free += 1
        if not over or not n_free:
            break
        for p in range(NPH):
            if on[p] and want[p] < cap[p]:
                want[p] += c_div(over, n_free)
    for p in range(NPH):
        if on[p]:
            give[p] = max(0, min(want[p], cap[p]))
    return give


def alloc(w, on, cap, rot, split_all):
    """S5 split (0.77-py, both directions, whole watts >= 0): split_all -> all phases with on[p] alike (EQ);
    else the rotating phase rot takes w alone up to its cap (1PH), only what it cannot take goes to the others alike
    (SPILL, else STBY). Returns (give per phase, tag per phase)."""
    give, tag = [0] * NPH, [""] * NPH
    if split_all or not on[rot]:
        give = water_fill(w, on, cap)
        for p in range(NPH):
            if on[p]:
                tag[p] = " EQ"
        return give, tag
    give[rot] = max(0, min(w, cap[rot]))
    rest = water_fill(w - give[rot], [on[p] and p != rot for p in range(NPH)], cap)
    for p in range(NPH):
        if p != rot and on[p]:
            give[p] = rest[p]
            tag[p] = " SPILL" if give[p] else " STBY"
    tag[rot] = " 1PH"
    return give, tag


def int_caps(caps):
    return [int(c) if c > 0 else 0 for c in caps]


# ---- decision layer: pure functions of predicate bits + state (enumerated by make check in C) ----------------

BM_SRC_DAY, BM_SRC_B1, BM_SRC_STK, BM_SRC_WPG = range(4)
BM_SRC_NAME = ["DAY", "B1", "STK", "WPG"]
(BM_M_BMS_MISSING, BM_M_FORCE, BM_M_STALE, BM_M_LADESPERRE, BM_M_CHARGE, BM_M_FULL, BM_M_PROT, BM_M_CHARGING,
 BM_M_DIS, BM_M_HOLD) = range(10)                  # HOLD appended (0.62-c), the older indices stay
BM_MODE_NAME = ["BMS_MISSING", "FORCE", "STALE", "LADESPERRE", "CHARGE", "FULL", "PROT", "CHARGING", "DIS", "HOLD"]
@dataclass(frozen=True)
class SrcRule:
    b1_signed: int      # 1: a Bat1 discharge is house load the stacks take over; 0: only its charge counts
    trickle: int        # target + SOFAR_TRICKLE: the Sofar battery charges a few W steadily
    idle_w: int         # discharge W while not PROPORTIONAL
    prop_hold: int      # IDLE -> PROPORTIONAL already above SOYO_HOLD_TH, not only on import


# discharge matrix, S3 source rows: only the night floor (STK) takes the Sofar battery over
BM_SRC_RULE = {
    BM_SRC_DAY: SrcRule(0, 0, B_DAY_IDLE, 0),
    BM_SRC_B1:  SrcRule(0, 0, B_DAY_IDLE, 0),
    BM_SRC_STK: SrcRule(1, 1, 0, 1),
    BM_SRC_WPG: SrcRule(0, 0, B_DAY_IDLE, 0),       # winter night, heat pump running, no B1FIRST
}

BM_WP_NONE, BM_WP_ZERO, BM_WP_MAX = range(3)   # wp_cap: no rule, 0 W, at most WP_WINTER_W (0.61-c)
BM_A_ZERO, BM_A_FORCE, BM_A_CHARGE, BM_A_DISCHARGE = range(4)
BM_W_NONE, BM_W_NAME, BM_W_SOC, BM_W_PEAK_H, BM_W_POWER, BM_W_CHAIN = range(6)


@dataclass(frozen=True)
class ModeRule:
    action: int         # BM_A_*: 0 W, FORCE_CHARGE_W, the charge amount, the discharge amount
    capped: int         # the setpoint is limited by the phase ceiling (chg_cap / dis_cap)
    why: int            # BM_W_*: rule text NAME, NAME(soc %), NAME(peak h), NAME(BMS W), NAME WP: SRC:, none (S4)


# mode matrix (0.48-c): what each S1 mode means for the setpoint
BM_MODE_RULE = {
    BM_M_BMS_MISSING: ModeRule(BM_A_ZERO, 0, BM_W_NAME),
    BM_M_FORCE:       ModeRule(BM_A_FORCE, 1, BM_W_SOC),
    BM_M_STALE:       ModeRule(BM_A_ZERO, 0, BM_W_NAME),
    BM_M_LADESPERRE:  ModeRule(BM_A_ZERO, 0, BM_W_PEAK_H),
    BM_M_CHARGE:      ModeRule(BM_A_CHARGE, 1, BM_W_CHAIN),
    BM_M_FULL:        ModeRule(BM_A_ZERO, 0, BM_W_NAME),
    BM_M_PROT:        ModeRule(BM_A_ZERO, 0, BM_W_SOC),
    BM_M_CHARGING:    ModeRule(BM_A_ZERO, 0, BM_W_POWER),
    BM_M_DIS:         ModeRule(BM_A_DISCHARGE, 1, BM_W_NONE),
    BM_M_HOLD:        ModeRule(BM_A_ZERO, 0, BM_W_SOC),       # force_full_from_pv without surplus (0.62-c)
}


# Decision tables (0.78-py): bm_decisions.dmn, standard DMN 1.3 (hit policy FIRST), read once at import with the
# standard library (no DMN engine on Venus; tests/check_dmn.py compares this reader with the SpiffWorkflow engine).
# A table is (columns, rows); each row holds one condition per column (1, 0, or _ = any), then the outputs.
# COLUMNS[table] = [(column, expression or None)]: the inputExpression of the column (its meaning, e.g.
# "soc < SOC_MIN"), None = a bare flag the chain code supplies under the column name.
# The site parameters stay as PARAM lines in bm_tables.h (bm_params_pull.py writes them).
_ = None
KEEP = "KEEP"
TABLES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bm_tables.h")   # 0.76-py: out of c/
DMN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bm_decisions.dmn")
DMN_NS = "{https://www.omg.org/spec/DMN/20191111/MODEL/}"

_PARAM_RE = re.compile(r'^PARAM\((\w+),\s*(-?[0-9.]+)\)\s*(/\*.*\*/)?$')

# the measured values and flags an inputExpression may use besides the parameters and constants
DMN_INPUTS = {
    "have_pv": "Sofar PV value present", "pv": "Sofar PV W", "wp_fresh": "SDM72D heat pump value younger than WP_MAX_AGE",
    "wp": "heat pump W", "have_soc_bat1": "Sofar Bat1 SoC present", "soc_bat1": "Sofar Bat1 SoC %",
    "stale": "Sofar sample older than STALE_SECONDS", "bat1": "Sofar Bat1 W, + = charge",
    "surplus": "PCC surplus W (charge point)", "soc": "bank SoC % (master BMS)", "power_ok": "bank power present",
    "power": "bank power W, + = charge", "lim_ok": "BMS sends CCL / DCL", "dcl": "BMS DCL A",
    "cell_ok": "lowest cell present", "cell_min": "lowest cell V",
    "house": "PCC + own charge W", "deficit": "discharge deficit W",
    "have_pcc": "PCC present", "pcc": "PCC W, + = export",
    "acout_total": "AC-out total W",
    "import_gross": "grid import W without the own discharge / charge (0.80-py)",
}
_EXPR_NODES = (ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not, ast.USub, ast.Compare, ast.Lt,
               ast.LtE, ast.Gt, ast.GtE, ast.BinOp, ast.Add, ast.Sub, ast.Name, ast.Load, ast.Constant)


def _expr_compile(text, where):
    """an inputExpression -> (ast, code): only and / or / not(...), one comparison per term, + -, names, numbers
    (the common subset of FEEL and Python, so a DMN tool reads the same meaning)"""
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as e:
        raise ValueError("%s: %s" % (where, e))
    for node in ast.walk(tree):
        if not isinstance(node, _EXPR_NODES):
            raise ValueError("%s: %s not allowed in %r" % (where, type(node).__name__, text))
        if isinstance(node, ast.Compare) and len(node.ops) != 1:
            raise ValueError("%s: one comparison per term in %r" % (where, text))
        if isinstance(node, ast.Constant) and (isinstance(node.value, bool) or not isinstance(node.value, (int, float))):
            raise ValueError("%s: only numbers as constants in %r" % (where, text))
        if isinstance(node, ast.Name) and node.id not in DMN_INPUTS \
                and not any(p.name == node.id for p in BM_PARAMS) and not re.fullmatch(r"[A-Z][A-Z0-9_]*", node.id):
            raise ValueError("%s: unknown name %s in %r" % (where, node.id, text))
    return tree, compile(tree, "<%s>" % where, "eval")


def _output(word, where):
    """an outputEntry -> its value: a number, "KEEP" or a BM_SRC_ / BM_WP_ / BM_M_ name"""
    if re.fullmatch(r"-?\d+", word):
        return int(word)
    if word == '"KEEP"':
        return KEEP
    if re.fullmatch(r"(BM_SRC_|BM_WP_|BM_M_)[A-Z0-9_]+", word) and isinstance(globals().get(word), int):
        return globals()[word]
    raise ValueError("%s: unknown output %r" % (where, word))


SITE_PARAMS = {}   # 0.76-py: PARAM lines of bm_tables.h, name -> value; batmonitor.py applies them (Cfg() keeps the defaults)
COLUMNS = {}       # 0.78-py: table -> [(column, (ast, code) or None)]
NOTES = {}         # 0.78-py: table -> its DMN description, rule notes: table -> [description per rule]


def _site_param(name, text, where):
    """one PARAM line: the name must be in BM_PARAMS, the value inside its range, each name once"""
    p = next((q for q in BM_PARAMS if q.name == name), None)
    if p is None:
        raise ValueError("%s: PARAM %s is not in BM_PARAMS" % (where, name))
    if name in SITE_PARAMS:
        raise ValueError("%s: PARAM %s set twice" % (where, name))
    v = float(text)
    if not (p.min <= v <= p.max):
        raise ValueError("%s: PARAM %s=%g outside %g..%g %s" % (where, name, v, p.min, p.max, p.unit))
    SITE_PARAMS[name] = v


def load_tables(path=TABLES_FILE):
    """bm_tables.h -> SITE_PARAMS (0.78-py: only PARAM lines, comments, blank lines; the tables are in the DMN file).
    Refuses every other line. Returns SITE_PARAMS (bm_params_pull.py checks a new PARAM block with it)."""
    comment = False
    SITE_PARAMS.clear()
    with open(path) as f:
        for n, line in enumerate(f, 1):
            s, where = line.strip(), "%s:%d" % (os.path.basename(path), n)
            if comment:
                comment = "*/" not in s
                continue
            if not s:
                continue
            if s.startswith("/*"):
                comment = "*/" not in s
                continue
            m = _PARAM_RE.match(s)
            if not m:
                raise ValueError("%s: neither PARAM nor a comment: %s" % (where, s))
            _site_param(m.group(1), m.group(2), where)
    return SITE_PARAMS


def load_decisions(path=DMN_FILE):
    """bm_decisions.dmn -> {name: (columns, rows)}, COLUMNS, NOTES. Checks per table: hit policy FIRST, entries
    true / false / -, conditions = inputs, outputs = output columns, last rule all "-", a column name means the same
    expression in every table that has it."""
    root = ET.parse(path).getroot()
    if root.tag != DMN_NS + "definitions":
        raise ValueError("%s: not a DMN 1.3 definitions file" % path)
    tables, meaning = {}, {}
    COLUMNS.clear()
    NOTES.clear()
    entry = {"true": 1, "false": 0, "-": _, "": _}
    for dec in root.iter(DMN_NS + "decision"):
        name = dec.get("name")
        dt = dec.find(DMN_NS + "decisionTable")
        where = "%s:%s" % (os.path.basename(path), name)
        if dt is None or dt.get("hitPolicy", "UNIQUE") != "FIRST":
            raise ValueError("%s: needs a decisionTable with hitPolicy FIRST" % where)
        if name in tables:
            raise ValueError("%s: decision defined twice" % where)
        cols = []
        for inp in dt.findall(DMN_NS + "input"):
            label = inp.get("label")
            text = (inp.findtext("%sinputExpression/%stext" % (DMN_NS, DMN_NS)) or "").strip()
            if not label or not text:
                raise ValueError("%s: input without label or inputExpression" % where)
            if meaning.setdefault(label, text) != text:
                raise ValueError("%s: column %s means %r here, %r elsewhere" % (where, label, text, meaning[label]))
            cols.append((label, None if text == label else _expr_compile(text, "%s.%s" % (where, label))))
        outs = [o.get("name") for o in dt.findall(DMN_NS + "output")]
        rows, notes = [], []
        for k, rule in enumerate(dt.findall(DMN_NS + "rule"), 1):
            ins = [(e.findtext(DMN_NS + "text") or "").strip() for e in rule.findall(DMN_NS + "inputEntry")]
            ous = [(e.findtext(DMN_NS + "text") or "").strip() for e in rule.findall(DMN_NS + "outputEntry")]
            if len(ins) != len(cols) or len(ous) != len(outs):
                raise ValueError("%s rule %d: %d / %d entries for %d inputs / %d outputs"
                                 % (where, k, len(ins), len(ous), len(cols), len(outs)))
            if any(t not in entry for t in ins):
                raise ValueError("%s rule %d: input entries must be true / false / -: %s" % (where, k, ins))
            rows.append(tuple(entry[t] for t in ins) + tuple(_output(t, "%s rule %d" % (where, k)) for t in ous))
            notes.append((rule.findtext(DMN_NS + "description") or "").strip())
        if not rows or any(c is not _ for c in rows[-1][:len(cols)]):
            raise ValueError("%s: does not end with an all \"-\" rule" % where)
        tables[name] = (tuple(c for c, _e in cols), rows)
        COLUMNS[name] = cols
        NOTES[name] = ((dec.findtext(DMN_NS + "description") or "").strip(), notes)
    return tables


def first_hit(table, bits):
    """the outputs of the first matching row (a tuple; one output -> the value itself). A column the caller does not
    provide is an error (a misspelled column name in the DMN file)."""
    cols, rows = table
    for row in rows:
        if all(c is _ or bool(bits[k]) == bool(c) for k, c in zip(cols, row)):
            out = row[len(cols):]
            return out[0] if len(out) == 1 else out
    raise LookupError("no row matches: %r" % (bits,))


def keep(v, old):
    return old if v == KEEP else v


_CONSTS = {}


def _expr_env(cfg):
    """names an inputExpression may use besides the inputs: the parameters (cfg values) and the number constants"""
    if not _CONSTS:
        _CONSTS.update({k: v for k, v in globals().items()
                        if re.fullmatch(r"[A-Z][A-Z0-9_]*", k) and isinstance(v, (int, float)) and not isinstance(v, bool)})
    env = dict(_CONSTS)
    env.update({p.name: getattr(cfg, p.field) for p in BM_PARAMS})
    return env


def table_bits(cfg, names, **inputs):
    """the column values of the expression columns of the tables `names`, computed from the inputs with their DMN
    inputExpression -> {column: bool}. Bare flag columns are not included (the caller adds them). An input an
    expression needs but the caller did not give raises NameError: no bit on a silent default."""
    env = _expr_env(cfg)
    env.update(inputs)
    out = {}
    for t in names:
        for col, ex in COLUMNS[t]:
            if ex is not None and col not in out:
                out[col] = bool(eval(ex[1], {"__builtins__": {}}, env))
    return out



load_tables()
TABLES = load_decisions()


def bm_d1(b, bat1_first):
    """D1: Sofar first latch, S3 source, winter heat pump rule (tables D1_*). b: dict of the bm_d1_in bits. Returns a
    dict like bm_d1_out."""
    o = {"bat1_first": keep(first_hit(TABLES["D1_LATCH"], dict(b, bat1_first=bat1_first)), bat1_first)}
    o["b1_first"] = first_hit(TABLES["D1_B1_FIRST"], dict(b, bat1_first=o["bat1_first"]))
    src = first_hit(TABLES["D1_SRC"], dict(b, b1_first=o["b1_first"]))
    o["night_floor"] = src == BM_SRC_STK
    o["src_reason"] = src
    o["wp_cap"] = first_hit(TABLES["D1_WP_CAP"], b)
    return o


def bm_d1_peak(b, wp_peak):
    """D1_WP_PEAK (0.80-py): heat pump on and grid import above WP_PEAK_ON_W -> discharge WP_PEAK_W (latch)"""
    return keep(first_hit(TABLES["D1_WP_PEAK"], dict(b, wp_peak=wp_peak)), wp_peak)


def bm_d1_id(b, bat1_first):
    """D1 cell as one byte (0.60-c): bit 0 night, 1 wp_on, 2 b1_above_min, 3 b1_ge_release, 4 winter, 5 b1_dis,
    6 bat1_first (the state before the cycle); bit 7 is 0 since 0.61-c (was wp_low); state.json d1_id,
    pv_victron.bm_d1_id"""
    bits = (b["night"], b["wp_on"], b["b1_above_min"], b["b1_ge_release"], b["winter"], b["b1_dis"], bat1_first)
    return sum(1 << i for i, x in enumerate(bits) if x)


def bm_d2(b, prot, force, low, chg_prev):
    """D2: charge mode, the latches prot / force / low per bank and the mode per bank (tables D2_*). ff / top missing
    in a bank's bits count as 0. Returns (prot, force, low, chg_prev, charge_mode, mode per bank)."""
    charge_mode = bool(first_hit(TABLES["D2_CHARGE_MODE"], dict(b, chg_prev=chg_prev)))
    prot, force, low, mode = list(prot), list(force), list(low), [0] * NBANK
    for k in range(NBANK):
        x = dict(b["bank"][k])
        x.setdefault("ff", 0)
        prot[k] = keep(first_hit(TABLES["D2_PROT"], x), prot[k])
        force[k] = keep(first_hit(TABLES["D2_FORCE"], x), force[k])
        low[k] = keep(first_hit(TABLES["D2_LOW"], x), low[k])
        mode[k] = first_hit(TABLES["D2_MODE"], dict(x, force=force[k], low=low[k], prot=prot[k], stale=b["stale"],
                                                    block=b["block"], charge_mode=charge_mode))
    return prot, force, low, int(BM_M_CHARGE in mode), charge_mode, mode


def bm_d3(any_dis, house_import, deficit_gt_hold, prop_hold, prop_prev):
    """D3 PROPORTIONAL / IDLE (table D3); the result is also the next state"""
    return first_hit(TABLES["D3"], {"any_dis": any_dis, "house_import": house_import, "prop_prev": prop_prev,
                                    "prop_hold": prop_hold, "deficit_gt_hold": deficit_gt_hold})


def bm_d4(b, peak_today):
    """D4 summer peak window (tables D4, D4_PEAK). Returns (block = wait below the cap, cap_active, peak_today)."""
    block, cap = first_hit(TABLES["D4"], b)
    return block, cap, keep(first_hit(TABLES["D4_PEAK"], b), peak_today)


def bm_d5(pcc_over, grace_over, pcc_hard, lockout_over, acout_over):
    """D5 DO4 pulse (table D5)"""
    return first_hit(TABLES["D5"], {"lockout_over": lockout_over, "acout_over": acout_over, "pcc_over": pcc_over,
                                    "grace_over": grace_over, "pcc_hard": pcc_hard})


# 0.81-py (user 2026-11-09): idle AC-out reads 20..50 W with minutes up to 137 W while the rotary switch is on the grid,
# so the old 99 W / 30 W pair never released; on after 60 s above 200 W, off after 120 s below 100 W
HOUSE_ON_W = 200.0
HOUSE_OFF_W = 100.0
HOUSE_ON_S = 60.0
HOUSE_OFF_S = 120.0


def bm_d7(mode_off, mode_on, over_on, over_long, under_off, under_long, on):
    """D7 house on AC-out1 (table D7). Returns (on_next, under_next, over_next)"""
    on_next, under, over = first_hit(TABLES["D7"], {"mode_off": mode_off, "mode_on": mode_on, "over_on": over_on,
                                                    "over_long": over_long, "under_off": under_off,
                                                    "under_long": under_long, "on": on})
    return keep(on_next, on), under, over


# ---- the rule chain: one function per stage, run in this order by bm_step ------------------------------------
# Rule text per phase: <S1/S4 mode> [SRC:<S3>] [<S5 split>] [<limit>], see bm_logic.c

class Chain:
    """what one cycle derives and the later stages read (struct chain)"""

    def __init__(self):
        self.stale = self.wp_on = self.wp_fresh = self.night = self.winter = 0
        self.own_charge = self.own_discharge = self.multis = 0.0
        self.b1_first = self.night_floor = 0
        self.src = 0
        self.dis_max = 0                        # S4 discharge ceiling W: W_MAX, or the winter heat pump rule (0.61-c)
        self.wp_max = -1                        # that rule's ceiling W, -1 = none
        self.bat1_eff = self.chg_ref = self.surplus = 0.0
        self.block = self.charge_mode = 0
        self.cap_active = 0                     # D4: charge only the export above EXPORT_CAP
        self.ff = -1                            # force_full_from_pv bank, -1 = off (0.62-c)
        self.discharge = [0] * NPH
        self.charge = [0] * NPH
        self.n_dis = self.n_chg = 0
        self.house = self.house_w = 0           # D7: house on AC-out1, its load W (0.69-c)
        self.house_extra = 0                    # 0.81-py: W given above the house load for the heat pump
        self.wp_peak = 0                        # D1_WP_PEAK this cycle (0.80-py)


def chain_inputs(inp, out, c):
    """derived facts: data age, heat pump on, season (display), night, own AC-in"""
    month, _, _ = localtime(inp.t)
    c.stale = inp.inv_time == 0 or inp.now - inp.inv_time > STALE_SECONDS
    c.wp_fresh = inp.wp_time > 0 and inp.now - inp.wp_time < WP_MAX_AGE
    c.winter = month >= WINTER_FROM or month <= WINTER_TO
    c.ff = 0 if inp.force_full in (1, 2) else -1     # 0.77-py: S1 or S2 = the one bank
    # season from the measured energy (season.py), the months only when that is missing or older than 2 days
    season_ok = bool(inp.season and inp.t - inp.season_ts < SEASON_MAX_AGE)
    out.season = inp.season if season_ok else (BM_SUMMER if SUMMER_FROM <= month <= SUMMER_TO else BM_WINTER)
    out.season_measured = int(season_ok)
    # the meter already includes our own charging: use the measured AC-in, not the setpoints
    for p in range(NPH):
        if inp.ac_ok[p]:
            a = inp.ac_in[p]
            c.multis += a
            if a > 0:
                c.own_charge += a
            else:
                c.own_discharge -= a


def chain_house(cfg, inp, st, out, c):
    """D7 house on AC-out1 (0.69-c): the AC-out total of the phases that report it, the load = its positive phases"""
    total = load = 0.0
    for p in range(NPH):
        if inp.ac_out_ok[p]:
            total += inp.ac_out[p]
            load += inp.ac_out[p] if inp.ac_out[p] > 0 else 0
    mode = lround(cfg.house_acout)
    under = int(st.house_under_since > 0)
    over = int(st.house_over_since > 0)
    hb = table_bits(cfg, ("D7",), acout_total=total)
    on, under_next, over_next = bm_d7(mode == 0, mode == 1, hb["over_on"],
                                      over and inp.now - st.house_over_since >= HOUSE_ON_S, hb["under_off"],
                                      under and inp.now - st.house_under_since >= HOUSE_OFF_S, st.house_on)
    st.house_on = on
    if not over_next:
        st.house_over_since = 0.0
    elif not over:
        st.house_over_since = inp.now
    if not under_next:
        st.house_under_since = 0.0
    elif not under:
        st.house_under_since = inp.now
    c.house = on
    c.house_w = watt(load)
    out.house = c.house
    out.house_w = c.house_w


def chain_d1(cfg, inp, st, out, c):
    """S3 source and the winter heat pump rule"""
    # 0.78-py: the bits from the inputExpressions of bm_decisions.dmn
    b = table_bits(cfg, ("D1_LATCH", "D1_B1_FIRST", "D1_SRC", "D1_WP_CAP"), have_pv=inp.have_pv, pv=inp.pv,
                   wp_fresh=c.wp_fresh, wp=inp.wp, have_soc_bat1=inp.have_soc_bat1, soc_bat1=inp.soc_bat1,
                   stale=c.stale, bat1=inp.bat1)
    b["winter"] = c.winter
    c.night, c.wp_on = b["night"], b["wp_on"]
    out.d1_id = bm_d1_id(b, st.bat1_first)
    o = bm_d1(b, st.bat1_first)
    st.bat1_first = o["bat1_first"]
    # 0.80-py (user 2026-11-08 "wenn wp = on und pcc kauft mehr als 5000W, dann akkus entladen, aber nur mit 1000W"):
    # the import without the own discharge, so our 1000 W do not switch the rule off again
    imp = watt(-(inp.pcc + c.own_charge - c.own_discharge)) if inp.have_pcc else 0
    pb = table_bits(cfg, ("D1_WP_PEAK",), wp_fresh=c.wp_fresh, wp=inp.wp, have_pcc=inp.have_pcc, stale=c.stale,
                    import_gross=imp)
    st.wp_peak = bm_d1_peak(pb, st.wp_peak)
    c.wp_peak = out.wp_peak = st.wp_peak
    out.import_gross = imp
    c.wp_max = 0 if o["wp_cap"] == BM_WP_ZERO else int(cfg.wp_winter_w) if o["wp_cap"] == BM_WP_MAX else -1
    c.dis_max = c.wp_max if 0 <= c.wp_max < int(cfg.w_max) else int(cfg.w_max)
    out.wp_max_w = c.wp_max
    out.bat1_first = o["b1_first"]
    c.b1_first, c.night_floor, c.src = o["b1_first"], o["night_floor"], o["src_reason"]
    out.src_why = BM_SRC_NAME[o["src_reason"]]
    if c.house:                    # D7 (0.69-c): Z1 / Z2 rule, no source, night floor or heat pump cap
        c.wp_max = out.wp_max_w = -1
        c.dis_max = int(cfg.w_max)
        c.b1_first = out.bat1_first = 0
        c.night_floor = 0
        c.src = BM_SRC_DAY
        out.src_why = "Z2" if c.winter else "Z1"


def chain_gates(cfg, inp, st, out):
    """S0: the BMS SoC, per phase ceilings: charger, DISCHARGE_MAX_PHASE, the BMS's own CCL / DCL"""
    for b, bank in enumerate(BM_BANKS):
        st.soc_ok[b] = inp.bms_ok[b]
        if st.soc_ok[b]:
            st.soc[b] = inp.soc[b]
        for p in bank.phases:
            ch = bm_charge_cap(cfg, inp.volt_ok[b], inp.volt[b])
            d = cfg.discharge_max_phase
            out.ccl_bind[p] = out.dcl_bind[p] = 0
            if inp.lim_ok[b]:
                bc = bm_bms_cap_phase(cfg, b, inp.volt_ok[b], inp.volt[b], inp.ccl[b], 1)
                bd = bm_bms_cap_phase(cfg, b, inp.volt_ok[b], inp.volt[b], inp.dcl[b], 0)
                out.ccl_bind[p] = int(bc < fmin(ch, cfg.charger_cap_phase))
                out.dcl_bind[p] = int(bd < d)
                ch = fmin(ch, bc)
                d = fmin(d, bd)
            out.chg_cap[p] = ch
            out.dis_cap[p] = d


def d2_bank_bits(cfg, soc_ok, soc, power_ok, power, lim_ok, dcl, cell_ok, cell_min):
    """the bank columns of D2 from the inputExpressions of bm_decisions.dmn (0.78-py)"""
    b = table_bits(cfg, ("D2_PROT", "D2_FORCE", "D2_LOW", "D2_MODE"), soc=soc, power_ok=power_ok, power=power,
                   lim_ok=lim_ok, dcl=dcl, cell_ok=cell_ok, cell_min=cell_min)
    b["bms_ok"] = soc_ok
    return b


CAP_STEP = 900                 # s: forecast scan step of the peak window
CAP_DAY_END_H = 21             # local hour: the scan ends here


def ladesperre(cfg, inp, st, out, stale, multis, surplus, c):
    """the model part of the summer peak window and D4 on its bits. Forecast scan: expected PV of both inverters from
    now to CAP_DAY_END_H (clear sky x OWM slot x correction, anchored to the measured Sofar PV of now, as
    bm_full_forecast), minus HOUSE_EST = expected export. Sets c.cap_active, returns block (wait below the cap)"""
    now = inp.t
    lt = time.localtime(now)
    month, yday = lt.tm_mon, lt.tm_yday - 1
    new_day = yday != st.ls_yday
    st.ls_yday = yday
    dc = bm_dc_now(now, month)
    if dc > 0 and inp.aussen_time > 0 and inp.now - inp.aussen_time < AUSSEN_MAX_AGE:
        elev, _ = bm_sun_pos(now)
        dc *= dc_temp_factor(elev, inp.aussen)
    midnight = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))
    noon = solar_noon_utc(now)
    # measured PV minus house load against the model (display, r290_boost reads ratio_now)
    ratio = ratio_now = -1.0
    if dc > DC_RATIO_MIN and not stale and inp.have_pcc:
        ratio = (dc - (inp.pcc_avg5 + multis + inp.bat1_avg5)) / dc
        ratio_now = (dc - (inp.pcc + multis + inp.bat1)) / dc
    # forecast scan
    fi = FcIn(t=now, corr=inp.fc_corr, slots=list(inp.fc_slots[:BM_FC_SLOTS]))
    kt0, _ = fc_kt(fi, now)
    clear_sofar = calc_arrays_kt(ARRAYS, now, 1.0) * kt0
    anchor = 1.0
    if inp.have_pv and not stale and clear_sofar >= FC_ANCHOR_MIN_W:
        anchor = fmax(0.05, fmin(3.0, inp.pv / clear_sofar))
    best, peak_h, end_h, ahead = 0.0, -1, -1, False
    t = now
    while t <= midnight + CAP_DAY_END_H * 3600:
        pv, _ = fc_pv(fi, t, anchor)
        hour = time.localtime(t).tm_hour
        if pv > best:
            best, peak_h = pv, hour
        if pv - cfg.house_est > cfg.export_cap:
            ahead, end_h = True, hour
        t += CAP_STEP
    bits = {
        "new_day": new_day,
        "enabled": bool(cfg.ladesperre and cfg.export_cap > 0),
        "season": LADESPERRE_FROM <= month <= LADESPERRE_TO,
        "fc_cap_ahead": ahead,
        **table_bits(cfg, ("D4", "D4_PEAK"), have_pcc=inp.have_pcc, stale=stale, pcc=inp.pcc, surplus=surplus),
        "chg_prev": st.soyo_chg_prev,
    }
    block, c.cap_active, st.peak_today = bm_d4(bits, st.peak_today)
    out.ls = Ls(block, c.cap_active, peak_h if ahead else -1, end_h, dc, ratio, ratio_now,
                (noon - midnight) / 3600.0)
    return block


def chain_mode(cfg, inp, st, out, c):
    """S1: the surplus, D4 charge block, D2 mode per bank, then the mode matrix row per phase"""
    # a Sofar Bat1 discharge is no surplus; its charging counts with BAT1_CHARGE_FACTOR. The own discharge is
    # subtracted: else the night floor, partly going into the Sofar battery, counted as PV surplus
    c.bat1_eff = inp.bat1 if inp.bat1 < 0 else inp.bat1 * BAT1_CHARGE_FACTOR
    # the charge point is the real PCC (0.59-c; 0.30-c..0.58-c: PCC + the heat pump part the grid should pay)
    c.chg_ref = inp.pcc if inp.have_pcc else 0.0
    c.surplus = c.chg_ref + c.bat1_eff + c.own_charge - c.own_discharge
    c.block = ladesperre(cfg, inp, st, out, c.stale, c.multis, c.surplus, c)
    d = {
        "stale": c.stale,
        "block": c.block,
        **table_bits(cfg, ("D2_CHARGE_MODE",), surplus=c.surplus),
        "bank": [d2_bank_bits(cfg, st.soc_ok[b], st.soc[b], inp.power_ok[b], inp.power[b], inp.lim_ok[b], inp.dcl[b],
                              inp.cell_ok[b], inp.cell_min[b]) for b in range(NBANK)],
    }
    for b in range(NBANK):
        d["bank"][b]["ff"] = b == c.ff
    st.prot, st.force, st.low, _, c.charge_mode, mode = bm_d2(d, st.prot, st.force, st.low, st.soyo_chg_prev)
    for b, bank in enumerate(BM_BANKS):
        r = BM_MODE_RULE[mode[b]]
        name = BM_MODE_NAME[mode[b]]
        for p in bank.phases:
            if r.action == BM_A_FORCE:
                out.sp[p] = int(fmin(cfg.force_charge_w, out.chg_cap[p]) if r.capped else cfg.force_charge_w)
            else:
                out.sp[p] = 0
            if r.action == BM_A_CHARGE:
                c.charge[p] = 1
                c.n_chg += 1
            elif r.action == BM_A_DISCHARGE:
                c.discharge[p] = 1
                c.n_dis += 1
            if r.why == BM_W_NAME:
                out.why[p] = name
            elif r.why == BM_W_SOC:
                out.why[p] = "%s(%.1f%%)" % (name, st.soc[b])
            elif r.why == BM_W_PEAK_H:
                out.why[p] = "%s(%dh)" % (name, out.ls.peak_h)
            elif r.why == BM_W_POWER:
                out.why[p] = "%s(%.0fW)" % (name, inp.power[b])
            elif r.why == BM_W_CHAIN:
                out.why[p] = "%s SRC:%s" % (name, out.src_why)
            if mode[b] == BM_M_FORCE and st.low[b]:
                out.why[p] += " LOW"


def dis_amount(prop, deficit, idle_w, w_max):
    """S4 amount, the one formula: w = min(PROP ? max(deficit, 0) : idle_w, W_MAX)"""
    w = (deficit if deficit > 0 else 0) if prop else idle_w
    return w_max if w > w_max else w


def chain_rotate(cfg, inp, st, out):
    """0.77-py: the single MultiPlus moves on to the next phase every ROTATE_S (charge and discharge alike)"""
    if st.rot_since <= 0:
        st.rot_since = inp.now
    elif inp.now - st.rot_since >= cfg.rotate_s:
        st.rot = (st.rot + 1) % NPH
        st.rot_since = inp.now
    out.rot = st.rot


def chain_split(cfg, st, out, w):
    """0.77-py: all three MultiPlus above SINGLE_MAX_W, one again below SINGLE_MAX_W - SINGLE_HYST_W"""
    if cfg.single_max_w <= 0 or w > cfg.single_max_w:
        st.split_all = 1
    elif w < cfg.single_max_w - cfg.single_hyst_w:
        st.split_all = 0
    out.split_all = st.split_all


def chain_discharge(cfg, inp, st, out, c):
    """S4 + S5 discharge: deficit = what the Multis already give (minus what goes into the Sofar battery) + the import
    still left; the own charging (force charge of the other stack) is no house load"""
    r = BM_SRC_RULE[c.src]
    house = watt(inp.pcc + c.own_charge)
    bat1 = watt(inp.bat1)
    # b1_signed (night floor): the Sofar Bat1 counts signed, so both regulators do not fight and the PCC stays near 0
    b1_def = bat1 if r.b1_signed else (bat1 if bat1 > 0 else 0)
    # trickle: at night aim at pcc + bat1 = +SOFAR_TRICKLE, the Sofar battery charges a few W steadily
    target = watt(cfg.soyo_target + (cfg.sofar_trickle if r.trickle else 0.0))
    deficit = watt(c.own_discharge) - b1_def - c_div(KP_PM * (house - target), 1000)
    if c.house:
        # D7 (0.69-c): winter Z2 near 0 = exactly the AC-out1 load (feed forward, no loop); summer Z1 near 0 = the PCC
        # deficit (SRC DAY row: a Bat1 charge is our overshoot), at least the AC-out1 load. No PROP / IDLE state
        # Winter export cap (0.74-c, user 2026-10-29: "nur max 100 w ins grid"): the stacks give at most what keeps the
        # PCC export at CHARGE_RESERVE, so the PV covers the house first; at 0 W the surplus rule (CHARGE) takes over
        pcc_w = deficit if deficit > 0 else 0
        prop = int(not c.winter and pcc_w > c.house_w)
        exp = 0
        # 0.81-py (user 2026-11-09): the whole house load plus a small share for the running heat pump
        extra = int(cfg.house_wp_extra_w) if c.winter and c.wp_on else 0
        w = pcc_w if prop else c.house_w + extra
        if c.winter:
            cap = watt(c.own_discharge) - c_div(KP_PM * (house - watt(cfg.charge_reserve)), 1000)
            if cap < 0:
                cap = 0
            if cap < w:
                w = cap
                exp = 1
        if w > c.dis_max:
            w = c.dis_max
        c.house_extra = 0 if exp or prop else extra
        st.soyo_prop_prev = 0
        rule = "%s SRC:%s" % ("PROP" if prop else "EXPCAP" if exp else "HOUSE", out.src_why)
    else:
        d3 = table_bits(cfg, ("D3",), house=house, deficit=deficit)
        prop = bm_d3(1, d3["house_import"], d3["deficit_gt_hold"], r.prop_hold, st.soyo_prop_prev)
        st.soyo_prop_prev = prop
        w = dis_amount(prop, deficit, r.idle_w, c.dis_max)
        rule = "%s SRC:%s" % ("PROP" if prop else "IDLE", out.src_why)
    if c.wp_peak:                  # D1_WP_PEAK (0.80-py): above the heat pump cap and PROP / IDLE
        # user 2026-11-08 "besser statt fix 1000w pcc auf 5000w regeln": give what the import is above WP_PEAK_ON_W
        # (feed forward on the import without the own discharge, so the PCC sits at -WP_PEAK_ON_W), at most
        # WP_PEAK_W; never less than the other rules give
        peak = min(max(out.import_gross - int(cfg.wp_peak_on_w), 0), int(cfg.wp_peak_w), int(cfg.w_max))
        if peak >= w:
            w = peak
            c.house_extra = 0
            rule = "WPPEAK SRC:%s" % out.src_why
    dcap = int_caps(out.dis_cap)
    w = min(w, sum(dcap[p] for p in range(NPH) if c.discharge[p]))
    chain_split(cfg, st, out, w)
    give, tag = alloc(w, c.discharge, dcap, st.rot, st.split_all)
    for p in range(NPH):
        if c.discharge[p]:
            out.sp[p] = -give[p]
            out.why[p] = (rule + tag[p])[:WHY_LEN - 1]
    if c.wp_max >= 0 and not c.wp_peak:
        for p in range(NPH):
            if c.discharge[p]:
                out.why[p] = (out.why[p] + " WPMAX(%d)" % c.wp_max)[:WHY_LEN - 1]
    if c.house_extra:
        for p in range(NPH):
            if c.discharge[p]:
                out.why[p] = (out.why[p] + " WP+%d" % c.house_extra)[:WHY_LEN - 1]


def chain_charge(cfg, st, out, c):
    """S4 + S5 charge: own charge + KP x (PCC surplus incl. the Sofar charge share), at most the phase ceilings, never
    below 0"""
    cap = int_caps(out.chg_cap)
    cap_sum = sum(cap[p] for p in range(NPH) if c.charge[p])
    # D4 cap (0.49-c): aim at PCC = +EXPORT_CAP, so only the export above the cap goes into the stacks;
    # CHARGE_RESERVE (0.72-c): always this much less than the surplus
    target = watt(cfg.soyo_target + cfg.charge_reserve + (cfg.export_cap if c.cap_active else 0.0))
    want = watt(c.own_charge) + c_div(KP_PM * (watt(c.chg_ref + c.bat1_eff) - target), 1000)
    if want < 0:
        want = 0
    w = min(want, cap_sum)
    chain_split(cfg, st, out, w)
    give, tag = alloc(w, c.charge, [min(cap[p], int(cfg.charge_max_phase)) for p in range(NPH)], st.rot, st.split_all)
    for p in range(NPH):
        if c.charge[p]:
            out.sp[p] = give[p]
            out.why[p] = (out.why[p] + tag[p] + (" FF" if c.ff >= 0 else ""))[:WHY_LEN - 1]
            if c.cap_active:
                out.why[p] = (out.why[p] + " CAP")[:WHY_LEN - 1]


def chain_pi(cfg, inp, st, out, c):
    """PI prototype (shadow unless BATMONITOR_PI=1) on y = PCC + Sofar Bat1, same gates and split"""
    pi_ok, pi_chg, pi_dis = [0] * NPH, [0] * NPH, [0] * NPH
    n_pichg = n_pidis = 0
    for b, bank in enumerate(BM_BANKS):
        for p in bank.phases:
            if not st.soc_ok[b] or st.force[b] or st.low[b] or c.stale:
                continue
            pi_ok[p] = 1
            if not c.block and (st.soc[b] < 100.0 or b == c.ff):
                pi_chg[p] = 1
                n_pichg += 1
            if not st.prot[b] and b != c.ff:          # force_full_from_pv: that stack never discharges
                pi_dis[p] = 1
                n_pidis += 1
    applied = 0.0
    for p in range(NPH):
        if pi_ok[p]:
            applied += st.setpoints[p]
    b1 = inp.bat1 * BAT1_CHARGE_FACTOR if inp.bat1 > 0 else (inp.bat1 if (applied > 0 or c.night_floor) else 0.0)
    y = (inp.pcc if inp.have_pcc else 0.0) + b1
    e = y - PI_TARGET
    if abs(e) < PI_DEADBAND:
        e = 0
    dt = fmin(fmax(inp.now - st.pi_t_prev, 1.0), 30.0) if st.pi_t_prev > 0 else CYCLE_SECONDS
    u = applied + (PI_KP * (e - st.pi_e_prev) if st.pi_t_prev > 0 else 0) + PI_KI * dt * e
    dis_cap = float(c.dis_max)               # W_MAX or the winter heat pump rule (0.61-c)
    u_max = 0.0
    u_min = -dis_cap if n_pidis and dis_cap > 0 else 0     # no -0.0 under WPMAX(0)
    for p in range(NPH):
        if pi_chg[p]:
            u_max += out.chg_cap[p]
    if applied <= 0 and u > 0:               # crossing into charging: never more than the surplus at the Z2 point
        u = fmax(0.0, fmin(u, (inp.pcc if inp.have_pcc else 0.0) + b1 - PI_TARGET))
    u = fmax(u_min, fmin(u_max, u))
    st.pi_e_prev = e
    st.pi_t_prev = inp.now
    psp = [0] * NPH
    pwhy = [w[:95] for w in out.why]
    for p in range(NPH):
        if pi_ok[p]:
            pwhy[p] = "PI(%+.0f)" % u
    if u > 0:
        give, _ = alloc(int(u), pi_chg, int_caps(out.chg_cap), st.rot, 1)
        psp = list(give)
    elif u < 0:
        give, _ = alloc(int(-u), pi_dis, int_caps(out.dis_cap), st.rot, 1)
        psp = [-g for g in give]
    for p in range(NPH):
        if not pi_ok[p]:
            psp[p] = out.sp[p]               # force charge, BMS missing, stale: as soyo
    out.pi = Pi(y, e, applied, u, u_min, u_max, list(psp))
    if cfg.pi_armed:
        out.sp = list(psp)
        out.why = list(pwhy)


def bm_step(cfg, inp, st):
    """one cycle: the rule chain above. Changes st, returns Out"""
    out = Out()
    c = Chain()
    chain_inputs(inp, out, c)
    chain_house(cfg, inp, st, out, c)
    chain_d1(cfg, inp, st, out, c)
    chain_gates(cfg, inp, st, out)
    chain_rotate(cfg, inp, st, out)
    chain_mode(cfg, inp, st, out, c)
    if c.n_dis:
        chain_discharge(cfg, inp, st, out, c)
    if c.n_chg:
        chain_charge(cfg, st, out, c)
    for p in range(NPH):                     # the BMS limit cut this phase's setpoint
        if ((out.sp[p] > 0 and out.ccl_bind[p] and out.sp[p] >= int(out.chg_cap[p]) - 1)
                or (out.sp[p] < 0 and out.dcl_bind[p] and -out.sp[p] >= int(out.dis_cap[p]) - 1)):
            out.why[p] = (out.why[p] + (" CCL" if out.sp[p] > 0 else " DCL"))[:WHY_LEN - 1]
    st.soyo_chg_prev = int(c.n_chg > 0)
    # force_full_from_pv (0.62-c): done once its stack held 100 % for FORCE_FULL_HOLD_MIN; a missing BMS keeps the clock
    if c.ff >= 0 and st.soc_ok[c.ff] and st.soc[c.ff] >= 100.0:
        if st.ff_full_since <= 0:
            st.ff_full_since = inp.now
        if inp.now - st.ff_full_since >= cfg.force_full_hold_min * 60.0:
            out.ff_done = BM_FF_HOLD
    elif c.ff < 0 or st.soc_ok[c.ff]:
        st.ff_full_since = 0.0
    # 0.67-c (user 2026-10-27: "sobald die Zellspreizung niedrig genug ist default mode, oder spaetestens nach timeout"):
    # also done once the cell spread stayed at or below FORCE_FULL_SPREAD_MV for FF_SPREAD_STABLE_S with the highest
    # cell at the top, or FORCE_FULL_MAX_MIN after the command came. Missing cell values stop the spread clock.
    if c.ff < 0:
        st.ff_since = st.ff_spread_since = 0.0
    else:
        if st.ff_since <= 0:
            st.ff_since = inp.now
        b = c.ff
        if (cfg.force_full_spread_mv > 0 and inp.cell_ok[b] and inp.cell_max_ok[b]
                and inp.cell_max[b] >= cfg.force_full_top_v
                and (inp.cell_max[b] - inp.cell_min[b]) * 1000.0 <= cfg.force_full_spread_mv):
            if st.ff_spread_since <= 0:
                st.ff_spread_since = inp.now
        else:
            st.ff_spread_since = 0.0
        if not out.ff_done and st.ff_spread_since > 0 and inp.now - st.ff_spread_since >= FF_SPREAD_STABLE_S:
            out.ff_done = BM_FF_SPREAD
        if not out.ff_done and cfg.force_full_max_min > 0 and inp.now - st.ff_since >= cfg.force_full_max_min * 60.0:
            out.ff_done = BM_FF_MAX
    if not c.n_dis:
        st.soyo_prop_prev = 0                # D3 with no discharging phase: reset
    if not c.n_dis and not c.n_chg:
        st.split_all = 0                     # 0.77-py: idle -> the next amount starts on one MultiPlus
    out.split_all = st.split_all
    pcc_over = bool(inp.have_pcc and inp.now - inp.inv_time < DO4_PCC_MAX_AGE and inp.pcc > PCC_PEAK_TH)
    if not pcc_over:
        st.do4_over_since = 0.0
    elif st.do4_over_since <= 0:
        st.do4_over_since = inp.now
    feed = 0.0                               # AC-out: + = loads, - = PV feeding back
    for p in range(NPH):
        if inp.ac_out_ok[p] and -inp.ac_out[p] > feed:
            feed = -inp.ac_out[p]
    out.acout_feed_w = feed
    # off-grid only (user 2026-10-10): with the grid there everything passes the transfer relay (bypass)
    acout_now = bool(not inp.grid_ok and cfg.acout_feed_max_w > 0 and feed > cfg.acout_feed_max_w)
    if not acout_now:
        st.acout_over_since = 0.0
    elif st.acout_over_since <= 0:
        st.acout_over_since = inp.now
    acout_over = acout_now and inp.now - st.acout_over_since >= cfg.acout_feed_s
    out.do4_pulse = bm_d5(pcc_over, pcc_over and inp.now - st.do4_over_since >= cfg.do4_grace_s,
                          pcc_over and inp.pcc > cfg.do4_hard_w,
                          st.do4_last <= 0 or inp.now - st.do4_last >= DO4_LOCKOUT, acout_over)
    out.do4_acout = int(bool(out.do4_pulse and acout_over))
    if out.do4_pulse:
        st.do4_last = inp.now
    chain_pi(cfg, inp, st, out, c)
    out.surplus = c.surplus
    st.setpoints = list(out.sp)
    return out


# ---- full_at forecast (0.28-c) -------------------------------------------------------------------------------

@dataclass
class FcIn:
    t: int = 0
    soc_ok: list = field(default_factory=lambda: [0] * NBANK)
    soc: list = field(default_factory=lambda: [0.0] * NBANK)
    has_avg: list = field(default_factory=lambda: [0] * NBANK)
    volt_ok: list = field(default_factory=lambda: [0] * NBANK)
    volt: list = field(default_factory=lambda: [0.0] * NBANK)
    avg: list = field(default_factory=lambda: [0.0] * NBANK)      # 5 min mean BMS /Dc/0/Power, + = charge
    have_pv: int = 0
    pv_sofar: float = 0.0
    slots: list = field(default_factory=list)                     # [(unix t, kt)] of forecast.py kt_slots
    corr: float = 0.0
    force_full: int = 0                                           # bm_in.force_full (0.62-c)


@dataclass
class FcOut:
    full_at: list = field(default_factory=lambda: [0] * NBANK)    # 0 = not full before sunset
    soc_sunset: list = field(default_factory=lambda: [-1.0] * NBANK)
    anchor: float = 1.0
    kt_now: float = 0.0
    pv_now_w: float = 0.0
    rest_kwh: float = 0.0
    weather: int = 0


def fc_kt(fi, t):
    """weather kt at t: the OWM slot within FC_SLOT_REACH, else the monthly kt. Returns (kt, weather)"""
    best, dbest = -1, FC_SLOT_REACH + 1
    for i, (st_, _) in enumerate(fi.slots):
        d = abs(t - st_)
        if d < dbest:
            dbest, best = d, i
    if best >= 0 and fi.corr > 0:
        return fi.slots[best][1] * fi.corr, 1
    month, _, _ = localtime(t)
    return KT_MONTH[month], 0


def fc_pv(fi, t, anchor):
    clear = calc_arrays_kt(ARRAYS, t, 1.0) + calc_arrays_kt(ARRAYS_EAST, t, 1.0)
    f = 1.0 + (anchor - 1.0) * math.exp(-(t - fi.t) / FC_ANCHOR_TAU)
    kt, w = fc_kt(fi, t)
    return clear * kt * f, w


def bm_full_forecast(cfg, fi):
    """steps from now to sunset in FC_STEP: the bank gets its measured charge power plus the change of the expected
    PV against now, up to its charger ceiling (0.77-py: one bank)"""
    out = FcOut()
    out.kt_now, out.weather = fc_kt(fi, fi.t)
    clear_sofar = calc_arrays_kt(ARRAYS, fi.t, 1.0) * out.kt_now
    if clear_sofar <= 0:
        return out                                              # night: no forecast
    if fi.have_pv and clear_sofar >= FC_ANCHOR_MIN_W:
        out.anchor = fmax(0.05, fmin(3.0, fi.pv_sofar / clear_sofar))
    pv0, _ = fc_pv(fi, fi.t, out.anchor)
    out.pv_now_w = pv0
    soc = list(fi.soc)
    pb, cap, active = [0.0] * NBANK, [0.0] * NBANK, [0] * NBANK
    any_ = False
    for b, bank in enumerate(BM_BANKS):
        active[b] = fi.soc_ok[b] and fi.has_avg[b]
        cap[b] = fmin(fmin(cfg.charger_cap_phase, bm_charge_cap(cfg, fi.volt_ok[b], fi.volt[b])) * FC_CHARGE_EFF,
                      cfg.charger_dc_w) * len(bank.phases)
        if not active[b]:
            continue
        any_ = True
        pb[b] = fmin(fmax(0.0, fi.avg[b]), cap[b])
        if soc[b] >= 100:
            out.full_at[b] = fi.t
    if not any_:
        return out
    p0 = sum(pb[b] for b in range(NBANK) if active[b] and soc[b] < 100)
    t = fi.t + FC_STEP
    while t < fi.t + 86400:
        pv, _ = fc_pv(fi, t - FC_STEP // 2, out.anchor)
        out.rest_kwh += pv * FC_STEP / 3600000.0
        rest = fmax(0.0, p0 + FC_CHARGE_EFF * (pv - pv0))
        for b in range(NBANK):
            pb[b] = 0
            if not active[b] or soc[b] >= 100:
                continue
            pb[b] = fmin(rest, cap[b])
            rest -= pb[b]
        for b, bank in enumerate(BM_BANKS):
            if not active[b] or soc[b] >= 100:
                continue
            soc[b] += pb[b] * FC_STEP / 3600.0 / bank.capacity_wh * 100.0
            if soc[b] >= 100:
                soc[b] = 100
                out.full_at[b] = t
        elev, _ = bm_sun_pos(t)
        if elev <= 0:
            break                                               # sunset
        t += FC_STEP
    for b in range(NBANK):
        if active[b]:
            out.soc_sunset[b] = soc[b]
    return out


def _check_tables():
    """at import: call every decision once with all bits 0, so a misspelled column in bm_tables.h fails at start,
    before the first setpoint (first_hit raises KeyError on a column no caller provides)"""
    z = dict.fromkeys
    bm_d1(z(["night", "wp_on", "b1_above_min", "b1_ge_release", "winter", "b1_dis"], 0), 0)
    bm_d1_peak(z(["wp_on", "peak_on", "peak_off"], 0), 0)
    bank = dict(d2_bank_bits(Cfg(), 0, 0.0, 0, 0.0, 0, 0.0, 0, 0.0), ff=0)
    bm_d2({"stale": 0, "block": 0, "surplus_gt_on": 0, "surplus_gt_hold": 0, "bank": [bank] * NBANK},
          [0] * NBANK, [0] * NBANK, [0] * NBANK, 0)
    bm_d3(0, 0, 0, 0, 0)
    bm_d4(z(["new_day", "enabled", "season", "fc_cap_ahead", "pcc_peak", "surplus_cap_on", "surplus_cap_hold",
             "chg_prev"], 0), 0)
    bm_d5(0, 0, 0, 0, 0)
    bm_d7(0, 0, 0, 0, 0, 0, 0)


_check_tables()
