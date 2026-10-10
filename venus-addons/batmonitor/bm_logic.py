"""
bm_logic.py - the batmonitor control logic in Python, a 1:1 port of c/bm_logic.c (0.50-c).

One cycle = bm_step(cfg, inp, st) -> Out. Pure: no D-Bus, MQTT, clock or files; batmonitor.py collects the inputs and
writes the outputs. The names, stages and tables are the ones of the C file, so both can be read side by side:

    inputs -> D1 (forecast, Sofar first; S2 heat pump part, S3 source) -> S0 caps -> D2 mode per stack
           -> D-lead -> mode matrix BM_MODE_RULE -> charge path (D4 charge block, FORCE, charge amount, split)
                                                -> discharge branch (BM_SRC_RULE, D3, discharge amount, split)
           -> D5 DO4 pulse -> PI shadow

The C version runs on Venus; this port is its readable reference and the fallback (BATMONITOR_PYTHON=1).
`make py-compare` (c/Makefile) proves both give byte-identical setpoints and rule texts on 240000 random cycles,
so every change must go into both files. The few C details that matter for that are in the helpers below
(truncating int casts and divisions, lround, fmin/fmax with NaN, 0-based tm_yday).
"""
import math
import time
from dataclasses import dataclass, field

NPH = 3
NBANK = 2
CYCLE_SECONDS = 5
WHY_LEN = 96
BM_SUMMER, BM_WINTER, BM_TRANSITION = 1, 2, 3
BM_NOMINAL_V = 51.2            # 16S LFP, for BMS current limits without a voltage

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
WP_MAX_AGE = 150.0             # s, older: no Z2 correction, WP_CAP as before
WP_ON_TH = 300.0               # W: heat pump counts as running (no night floor)
SEASON_MAX_AGE = 2 * 86400     # s: older batmonitor/season -> month rule
FC_MAX_AGE = 3 * 3600          # s: older batmonitor/forecast is ignored
BAT1_FIRST_HYST = 2.0          # %: Sofar-first back on above BAT1_SOC_MIN + this
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


# Stack1 MUST feeds the middle unit = Devices/0 = L1 (user photo 2026-10-08)
BM_BANKS = [
    Bank("STACK1_MUST", "com.victronenergy.battery.socketcan_can0", (0,), 300 * 51.2),
    Bank("STACK2_PYTES", "com.victronenergy.battery.ebox", (1, 2), 300 * 51.2),
]
CHARGE_PRIORITY = [0, 1]       # Stack1 first (one 70 A charger), then Stack2

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
    Param("WP_BAT_MAX_TRANSITION", "wp_bat_max_transition", 1900, 0, 10000, "W", "heat pump share from the batteries in the transition"),
    Param("WP_CAP", "wp_cap", 1000, 0, 10000, "W", "total discharge with R290 running if em0/power is stale (winter)"),
    Param("SOC_MIN", "soc_min", 5, 0, 50, "%", "no discharge below"),
    Param("SOC_MIN_RELEASE", "soc_min_release", 7, 0, 60, "%", "discharge again from"),
    Param("SOC_FORCE", "soc_force", 3, 0, 30, "%", "grid force charge below"),
    Param("SOC_FORCE_RELEASE", "soc_force_release", 5, 0, 40, "%", "force charge until"),
    Param("FORCE_CHARGE_W", "force_charge_w", 500, 0, 4000, "W", "force charge per phase"),
    Param("SOC_BALANCE_ON", "soc_balance_on", 3, 0.5, 50, "%", "SoC difference that starts balancing"),
    Param("SOC_BALANCE_OFF", "soc_balance_off", 1, 0, 50, "%", "balancing ends below"),
    Param("SOYO_TARGET", "soyo_target", 0, -500, 500, "W", "PCC target (+ = export)"),
    Param("FC_HYST", "fc_hyst", 2, 0, 20, "%", "forecast rule back on above target + this"),
    Param("SOFAR_TRICKLE", "sofar_trickle", 30, 0, 500, "W", "night: stacks give this much more than the house needs"),
    Param("CHARGER_A", "charger_a", 70, 0, 70, "A", "MultiPlus-II charger current per unit (DC), ceiling = this x BMS voltage"),
    Param("CHARGE_EFF", "charge_eff", 0.93, 0.7, 1.0, "", "AC-in per DC at the charger limit"),
    Param("BAT1_SOC_MIN", "bat1_soc_min", 5, 0, 101, "%", "night: the Sofar Bat1 serves house + heat pump first down to this SoC; 101 = off"),
    Param("FC_BAD_TARGET", "fc_bad_target", 100, 0, 101, "%", "bad forecast when target_soc >= this; 101 = off"),
    Param("WP_BAT_SHARE_BAD", "wp_bat_share_bad", 50, 0, 100, "%", "bad forecast: the stacks cover at most this share of the heat pump"),
    Param("EXPORT_CAP", "export_cap", 18000, 0, 30000, "W", "summer peak window: while the forecast expects export above this, the stacks charge only the export above it (0 = off)"),
    Param("HOUSE_EST", "house_est", 1000, 0, 5000, "W", "summer peak window: house load assumed when turning the PV forecast into export"),
    Param("DO4_GRACE_S", "do4_grace_s", 120, 0, 600, "s", "DO4 (FoxESS shedding) only after the PCC stays above 20 kW this long: R290 boost and air conditioning first"),
    Param("DO4_HARD_W", "do4_hard_w", 23000, 20000, 40000, "W", "DO4 at once above this PCC export"),
    Param("CHARGER_DC_W", "charger_dc_w", 3600, 500, 5000, "W", "full_at forecast: DC at the BMS per MultiPlus at its limit"),
]


class Cfg:
    """bm_cfg: the flags and every BM_PARAMS value as an attribute of the same name (cfg.w_max, ...)"""

    def __init__(self):
        self.ladesperre = 1    # BATMONITOR_LADESPERRE (default on)
        self.pi_armed = 0      # BATMONITOR_PI=1: the PI replaces the soyo amounts
        self.forecast = 1      # BATMONITOR_FORECAST (default on)
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


@dataclass
class State:
    """bm_state: what survives from cycle to cycle"""
    prot: list = field(default_factory=lambda: [0] * NBANK)
    force: list = field(default_factory=lambda: [0] * NBANK)
    soc_ok: list = field(default_factory=lambda: [0] * NBANK)
    soc: list = field(default_factory=lambda: [0.0] * NBANK)
    lead: int = -1                              # bank more than SOC_BALANCE_ON ahead, -1 = none
    soyo_chg_prev: int = 0
    soyo_prop_prev: int = 0
    ls_yday: int = -1
    peak_today: int = 0                         # PCC > 20 kW seen today (display)
    fc_active: int = 0
    bat1_first: int = 0
    do4_last: float = 0.0
    do4_over_since: float = 0.0                 # monotonic s since the PCC is above PCC_PEAK_TH, 0 = not (0.50-c)
    pi_e_prev: float = 0.0
    pi_t_prev: float = 0.0
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
    wp_eff: float = 0.0
    season: int = 0
    season_measured: int = 0
    fc_active: int = 0
    fc_bad: int = 0
    bat1_first: int = 0
    wp_why: str = ""
    src_why: str = ""
    fc_target: float = 0.0
    fc_min_soc: float = 0.0
    ls: Ls = field(default_factory=Ls)
    pi: Pi = field(default_factory=Pi)
    lead_event: int = 0
    do4_pulse: int = 0
    lead_diff: float = 0.0
    chg_cap: list = field(default_factory=lambda: [0.0] * NPH)
    dis_cap: list = field(default_factory=lambda: [0.0] * NPH)
    ccl_bind: list = field(default_factory=lambda: [0] * NPH)
    dcl_bind: list = field(default_factory=lambda: [0] * NPH)


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


def alloc_levels(w, lvl, n_lvl, cap):
    """S5 split core (both directions, whole watts): w over the phases with lvl[p] >= 0, level by level. Within a level
    every bank the same share, split over its phases; what a phase cannot take above cap[p] goes to the level's other
    phases alike (water filling, at most NPH rounds). Only what the caps cut goes on to the next level, not the
    division remainder. Returns (give per phase, what the caps cut in the last level)."""
    rest = w
    give = [0] * NPH
    for lv in range(n_lvl):
        nb = [0] * NBANK
        n_banks = 0
        for p in range(NPH):
            if lvl[p] == lv:
                if not nb[bank_of(p)]:
                    n_banks += 1
                nb[bank_of(p)] += 1
        if not n_banks:
            continue
        want = [0] * NPH
        cut = 0
        for p in range(NPH):
            if lvl[p] == lv:
                want[p] = c_div(c_div(rest, n_banks), nb[bank_of(p)])
                cut += want[p]
        for _ in range(NPH):                                    # water filling
            over = n_free = 0
            for p in range(NPH):
                if lvl[p] == lv:
                    if want[p] > cap[p]:
                        over += want[p] - cap[p]
                        want[p] = cap[p]
                    elif want[p] < cap[p]:
                        n_free += 1
            if not over or not n_free:
                break
            for p in range(NPH):
                if lvl[p] == lv and want[p] < cap[p]:
                    want[p] += c_div(over, n_free)
        for p in range(NPH):
            if lvl[p] == lv:
                give[p] = min(want[p], cap[p])
                cut -= give[p]
        rest = cut                                              # what the caps cut goes on
    return give, rest


def alloc_discharge(lead, w, dis, dcap, rule, sp, why):
    """with a lead the lead bank gives first (BAL), the other only what the lead cannot (SPILL, else WAIT); without a
    lead every discharging bank the same share (EQ), so both 300 Ah stacks drain alike"""
    n_lead = sum(1 for p in range(NPH) if dis[p] and bank_of(p) == lead)
    lvl = [(1 if n_lead and bank_of(p) != lead else 0) if dis[p] else -1 for p in range(NPH)]
    give, _ = alloc_levels(w, lvl, 2, dcap)
    for p in range(NPH):
        if dis[p]:
            sp[p] = -give[p]
            tag = " EQ" if not n_lead else " BAL" if not lvl[p] else " SPILL" if sp[p] else " WAIT"
            why[p] = (rule + tag)[:WHY_LEN - 1]


def charge_order(lead):
    """banks in charge order: CHARGE_PRIORITY, the lead bank (SoC ahead) last"""
    order = [b for b in CHARGE_PRIORITY if b != lead]
    if lead >= 0:
        order.append(lead)
    return order


def alloc_charge(cfg, lead, rest, chg, n_chg, cap, sp):
    """one level per bank in charge order, each up to min(cap, CHARGER_CAP_PHASE); what the caps cut goes up to
    CHARGE_MAX_PHASE on all charging phases alike"""
    order = charge_order(lead)
    lvl = [order.index(bank_of(p)) if chg[p] and bank_of(p) in order else -1 for p in range(NPH)]
    cap1 = [min(cap[p], int(cfg.charger_cap_phase)) for p in range(NPH)]
    give, rest = alloc_levels(rest, lvl, len(order), cap1)
    for p in range(NPH):
        if chg[p]:
            head = min(cap[p], int(cfg.charge_max_phase)) - give[p]
            more = c_div(rest, n_chg) if rest > 0 else 0
            sp[p] = give[p] + (more if more < head else head if head > 0 else 0)


def int_caps(caps):
    return [int(c) if c > 0 else 0 for c in caps]


# ---- decision layer: pure functions of predicate bits + state (enumerated by make check in C) ----------------

BM_WP_NA, BM_WP_OFF, BM_WP_FC, BM_WP_FCBAD, BM_WP_Z2, BM_WP_T1900, BM_WP_ALL = range(7)
BM_WP_NAME = ["NA", "OFF", "FC", "FCBAD", "Z2", "T1900", "ALL"]
BM_SRC_DAY, BM_SRC_B1, BM_SRC_STK, BM_SRC_B1W = range(4)
BM_SRC_NAME = ["DAY", "B1", "STK", "B1W"]
(BM_M_BMS_MISSING, BM_M_FORCE, BM_M_STALE, BM_M_LADESPERRE, BM_M_CHARGE, BM_M_FULL, BM_M_PROT, BM_M_CHARGING,
 BM_M_DIS) = range(9)
BM_MODE_NAME = ["BMS_MISSING", "FORCE", "STALE", "LADESPERRE", "CHARGE", "FULL", "PROT", "CHARGING", "DIS"]
BM_WPK_NONE, BM_WPK_CAP, BM_WPK_ALL, BM_WPK_BAD = range(4)
BM_WPK_NAME = ["NONE", "CAP", "ALL", "BAD"]


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
    BM_SRC_B1W: SrcRule(0, 0, B_DAY_IDLE, 0),
}

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
}


def bm_d1(b, fc_active, bat1_first):
    """D1. Forecast: while the lowest stack is above the SoC the next day's PV refills, the stacks serve the heat pump
    as in summer (on above target + FC_HYST, off below target). Bad forecast: the stacks cover at most WP_BAT_SHARE_BAD %
    of the heat pump (S2). Sofar first: Bat1 above BAT1_SOC_MIN and the heat pump running -> the Sofar delivers first
    at night (S3). b: dict of the bm_d1_in bits. Returns a dict like bm_d1_out."""
    o = {}
    o["fc_ok"] = b["fc_fresh"] and b["fc_minsoc_ok"]
    if not o["fc_ok"]:
        fc_active = 0
    elif not fc_active and b["fc_above_on"]:
        fc_active = 1
    elif fc_active and b["fc_below_off"]:
        fc_active = 0
    o["fc_active"] = fc_active
    o["fc_bad"] = int(bool(b["fc_fresh"] and not fc_active and b["fc_target_bad"]))
    o["season_wp"] = BM_SUMMER if fc_active else b["season"]
    o["winter"] = o["season_wp"] != BM_SUMMER       # transition counts as winter for the WP_CAP fallback
    if not b["b1_above_min"]:
        bat1_first = 0
    elif not bat1_first and b["b1_ge_release"]:
        bat1_first = 1
    o["bat1_first"] = bat1_first
    o["b1_first"] = int(bool(bat1_first and b["wp_on"]))
    # S3 reads the measured season, not season_wp (0.45-c): the forecast only widens the heat pump scope (S2)
    o["night_floor"] = bool(b["night"] and not o["b1_first"] and (b["season"] != BM_WINTER or not b["wp_on"]))
    o["wp_fc_capped"] = bool(o["fc_bad"] and b["wp_pos"] and b["fc_share_lt_share"])
    o["wp_cap_armed"] = bool(not b["wp_fresh"] and o["winter"] and b["wp_running"])
    if not b["wp_fresh"]:
        o["wp_reason"] = BM_WP_NA
    elif not b["wp_on"]:
        o["wp_reason"] = BM_WP_OFF
    elif fc_active:
        o["wp_reason"] = BM_WP_FC
    elif o["wp_fc_capped"]:
        o["wp_reason"] = BM_WP_FCBAD
    elif o["season_wp"] == BM_WINTER:
        o["wp_reason"] = BM_WP_Z2
    elif o["season_wp"] == BM_TRANSITION and b["wp_gt_trans"]:
        o["wp_reason"] = BM_WP_T1900
    else:
        o["wp_reason"] = BM_WP_ALL
    o["src_reason"] = (BM_SRC_DAY if not b["night"] else BM_SRC_B1 if o["b1_first"]
                       else BM_SRC_STK if o["night_floor"] else BM_SRC_B1W)
    o["wp_kind"] = (BM_WPK_BAD if o["wp_fc_capped"] else BM_WPK_NONE if o["season_wp"] == BM_WINTER
                    else BM_WPK_CAP if o["season_wp"] == BM_TRANSITION else BM_WPK_ALL)
    return o


def bm_d2(b, prot, force, chg_prev):
    """D2. Per bank: SoC protection and force charge with hysteresis (only while the BMS reports), then the mode, first
    match: BMS_MISSING > FORCE > STALE > LADESPERRE > CHARGE / FULL > PROT > CHARGING > DIS. Charge mode on the Z2
    surplus, held down to SOYO_HOLD_TH once charging. Returns (prot, force, chg_prev, charge_mode, mode per bank)."""
    charge_mode = bool(b["surplus_gt_on"] or (chg_prev and b["surplus_gt_hold"]))
    prot, force, mode, chg_next = list(prot), list(force), [0] * NBANK, 0
    for k in range(NBANK):
        x = b["bank"][k]
        if x["bms_ok"]:
            if x["soc_lt_min"]:
                prot[k] = 1
            elif x["soc_ge_release"]:
                prot[k] = 0
            if x["soc_lt_force"]:
                force[k] = 1
            elif x["soc_ge_force_release"]:
                force[k] = 0
        if not x["bms_ok"]:
            mode[k] = BM_M_BMS_MISSING
        elif force[k]:
            mode[k] = BM_M_FORCE
        elif b["stale"]:
            mode[k] = BM_M_STALE
        elif charge_mode and b["block"]:
            mode[k] = BM_M_LADESPERRE
        elif charge_mode:
            mode[k] = BM_M_CHARGE if x["soc_lt_100"] else BM_M_FULL
        elif prot[k]:
            mode[k] = BM_M_PROT
        elif x["charging"]:
            mode[k] = BM_M_CHARGING
        else:
            mode[k] = BM_M_DIS
        if mode[k] == BM_M_CHARGE:
            chg_next = 1
    return prot, force, chg_next, charge_mode, mode


def bm_dlead(both_ok, diff_gt_on, diff_lt_off, first_ahead, lead):
    """SoC balancing: the bank more than SOC_BALANCE_ON ahead takes the lead; cleared below SOC_BALANCE_OFF or without
    both BMS. Returns (lead, event: 1 new lead, -1 cleared, 0)"""
    if not both_ok:
        return -1, 0
    if lead < 0 and diff_gt_on:
        return (0 if first_ahead else 1), 1
    if lead >= 0 and diff_lt_off:
        return -1, -1
    return lead, 0


def bm_d3(any_dis, house_import, deficit_gt_hold, prop_hold, prop_prev):
    """D3. PROPORTIONAL while importing, or (already proportional / night floor) while the deficit is above
    SOYO_HOLD_TH; else IDLE. Returns the new prop (it is also the next state)"""
    return int(bool(any_dis and (house_import or ((prop_prev or prop_hold) and deficit_gt_hold))))


def bm_d4(b, peak_today):
    """D4 summer peak window as a forecast export cap (0.49-c). The cap holds while enabled, in the season and the
    forecast still expects export above EXPORT_CAP today; then the stacks charge only the surplus above the cap (charge
    mode on that surplus, held down to SOYO_HOLD_TH) and wait below it. peak_today: PCC above PCC_PEAK_TH seen today.
    Returns (block = wait below the cap, cap_active, peak_today)."""
    if b["new_day"]:
        peak_today = 0
    if b["pcc_peak"]:
        peak_today = 1
    cap = bool(b["enabled"] and b["season"] and b["fc_cap_ahead"])
    cap_charge = b["surplus_cap_on"] or (b["chg_prev"] and b["surplus_cap_hold"])
    return int(cap and not cap_charge), int(cap), peak_today


def bm_d5(pcc_over, grace_over, pcc_hard, lockout_over):
    """D5 (grace 0.50-c). A fresh PCC above PCC_PEAK_TH for DO4_GRACE_S (R290 boost and air conditioning first), or
    above DO4_HARD_W, and no pulse within DO4_LOCKOUT -> DO4 pulse"""
    return int(bool(pcc_over and (grace_over or pcc_hard) and lockout_over))


# ---- the rule chain: one function per stage, run in this order by bm_step ------------------------------------
# Rule text per phase: <S1/S4 mode> [WP:<S2>] [SRC:<S3>] [<S5 split>] [<limit>], see bm_logic.c

class Chain:
    """what one cycle derives and the later stages read (struct chain)"""

    def __init__(self):
        self.stale = self.wp_running = self.wp_fresh = self.wp_on = self.night = 0
        self.season_wp = self.winter = 0
        self.own_charge = self.own_discharge = self.multis = 0.0
        self.wp_eff = 0.0
        self.wp_fc_capped = 0
        self.z2 = 0.0
        self.b1_first = self.night_floor = 0
        self.src = self.wp_kind = 0
        self.wp_cap_armed = 0
        self.bat1_eff = self.chg_ref = self.surplus = 0.0
        self.block = self.charge_mode = 0
        self.cap_active = 0                     # D4: charge only the export above EXPORT_CAP
        self.discharge = [0] * NPH
        self.charge = [0] * NPH
        self.n_dis = self.n_chg = 0


def chain_inputs(inp, out, c):
    """derived facts: data age, heat pump running, season, night, own AC-in"""
    month, _, _ = localtime(inp.t)
    c.stale = inp.inv_time == 0 or inp.now - inp.inv_time > STALE_SECONDS
    c.wp_running = inp.r290_time > 0 and inp.now - inp.r290_time < STALE_SECONDS and inp.r290_hz > 0
    c.wp_fresh = inp.wp_time > 0 and inp.now - inp.wp_time < WP_MAX_AGE
    c.wp_on = c.wp_fresh and inp.wp >= WP_ON_TH
    c.night = bool(inp.have_pv and inp.pv < NIGHT_PV_TH)
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


def lowest_soc(inp):
    m = 101
    for k in range(NBANK):
        if inp.bms_ok[k] and inp.soc[k] < m:
            m = inp.soc[k]
    return m


def d1_bits(cfg, inp, out, c):
    """the predicates of bm_d1 from the inputs"""
    min_soc = lowest_soc(inp)
    base_share = (0.0 if out.season == BM_WINTER
                  else fmin(inp.wp, cfg.wp_bat_max_transition) if out.season == BM_TRANSITION else inp.wp)
    return {
        "season": out.season,
        "night": c.night,
        "wp_fresh": c.wp_fresh,
        "wp_pos": c.wp_fresh and inp.wp > 0,
        "wp_on": c.wp_on,
        "wp_running": c.wp_running,
        "wp_gt_trans": inp.wp > cfg.wp_bat_max_transition,
        "fc_share_lt_share": inp.wp * cfg.wp_bat_share_bad / 100.0 < base_share,
        "fc_fresh": bool(cfg.forecast and inp.fc_target >= 0 and inp.fc_ts > 0 and inp.t - inp.fc_ts < FC_MAX_AGE),
        "fc_minsoc_ok": min_soc <= 100,
        "fc_above_on": min_soc > inp.fc_target + cfg.fc_hyst,
        "fc_below_off": min_soc < inp.fc_target,
        "fc_target_bad": inp.fc_target >= cfg.fc_bad_target,
        "b1_above_min": bool(inp.have_soc_bat1 and inp.soc_bat1 > cfg.bat1_soc_min),
        "b1_ge_release": inp.soc_bat1 >= cfg.bat1_soc_min + BAT1_FIRST_HYST,
    }


def wp_share(cfg, wp_kind, wp):
    """S2 row of the discharge matrix: the part of the heat pump W the stacks cover"""
    if wp_kind == BM_WPK_NONE:
        return 0.0
    if wp_kind == BM_WPK_CAP:
        return fmin(wp, cfg.wp_bat_max_transition)
    if wp_kind == BM_WPK_BAD:
        return wp * cfg.wp_bat_share_bad / 100.0
    return wp


def chain_d1(cfg, inp, st, out, c):
    """forecast, S3 source, S2 scope; then z2 = PCC + the heat pump part the stacks must NOT cover"""
    o = bm_d1(d1_bits(cfg, inp, out, c), st.fc_active, st.bat1_first)
    st.fc_active = o["fc_active"]
    st.bat1_first = o["bat1_first"]
    out.fc_active = o["fc_active"]
    out.fc_target = inp.fc_target if o["fc_ok"] else -1
    out.fc_min_soc = lowest_soc(inp)
    out.fc_bad = o["fc_bad"]
    out.bat1_first = o["b1_first"]
    c.season_wp, c.winter = o["season_wp"], o["winter"]
    c.b1_first, c.night_floor = o["b1_first"], o["night_floor"]
    c.src, c.wp_kind = o["src_reason"], o["wp_kind"]
    c.wp_fc_capped, c.wp_cap_armed = o["wp_fc_capped"], o["wp_cap_armed"]
    out.wp_why = BM_WP_NAME[o["wp_reason"]]
    out.src_why = BM_SRC_NAME[o["src_reason"]]
    if c.wp_fresh and inp.wp > 0:
        c.wp_eff = inp.wp - wp_share(cfg, c.wp_kind, inp.wp)
    c.z2 = inp.pcc + c.wp_eff


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


def d2_bank_bits(cfg, soc_ok, soc, power_ok, power):
    return {
        "bms_ok": soc_ok,
        "soc_lt_min": soc < cfg.soc_min,
        "soc_ge_release": soc >= cfg.soc_min_release,
        "soc_lt_force": soc < cfg.soc_force,
        "soc_ge_force_release": soc >= cfg.soc_force_release,
        "soc_lt_100": soc < 100.0,
        "charging": bool(power_ok and power > CHARGING_TH),
    }


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
        "pcc_peak": bool(inp.have_pcc and not stale and inp.pcc > PCC_PEAK_TH),
        "surplus_cap_on": surplus - cfg.export_cap > PCC_SURPLUS_TH,
        "surplus_cap_hold": surplus - cfg.export_cap > SOYO_HOLD_TH,
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
    # charging on the Z2 point too (0.30-c): in winter the PV goes into the stacks, the heat pump takes the Z1 grid
    c.chg_ref = c.z2 if inp.have_pcc else 0.0
    c.surplus = c.chg_ref + c.bat1_eff + c.own_charge - c.own_discharge
    c.block = ladesperre(cfg, inp, st, out, c.stale, c.multis, c.surplus, c)
    d = {
        "stale": c.stale,
        "block": c.block,
        "surplus_gt_on": c.surplus > PCC_SURPLUS_TH,
        "surplus_gt_hold": c.surplus > SOYO_HOLD_TH,
        "bank": [d2_bank_bits(cfg, st.soc_ok[b], st.soc[b], inp.power_ok[b], inp.power[b]) for b in range(NBANK)],
    }
    st.prot, st.force, _, c.charge_mode, mode = bm_d2(d, st.prot, st.force, st.soyo_chg_prev)
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
                out.why[p] = "%s WP:%s SRC:%s" % (name, out.wp_why, out.src_why)


def chain_lead(cfg, st, out):
    diff = st.soc[0] - st.soc[1]
    st.lead, out.lead_event = bm_dlead(st.soc_ok[0] and st.soc_ok[1], abs(diff) > cfg.soc_balance_on,
                                       abs(diff) < cfg.soc_balance_off, diff > 0, st.lead)
    if out.lead_event:
        out.lead_diff = abs(diff)


def dis_amount(prop, deficit, idle_w, w_max, wp_cap_armed, wp_cap):
    """S4 amount, the one formula: w = min(PROP ? max(deficit, 0) : idle_w, W_MAX, WP_CAP while armed).
    Returns (w, capped by WP_CAP)"""
    w = (deficit if deficit > 0 else 0) if prop else idle_w
    if w > w_max:
        w = w_max
    capped = bool(wp_cap_armed and w > wp_cap)
    return (wp_cap if capped else w), capped


def chain_discharge(cfg, inp, st, out, c):
    """S4 + S5 discharge: deficit = what the Multis already give (minus what goes into the Sofar battery) + the import
    still left; the own charging (force charge of the other stack) is no house load"""
    r = BM_SRC_RULE[c.src]
    house = watt(c.z2 + c.own_charge)
    bat1 = watt(inp.bat1)
    # b1_signed (night floor): the Sofar Bat1 counts signed, so both regulators do not fight and the PCC stays near 0
    b1_def = bat1 if r.b1_signed else (bat1 if bat1 > 0 else 0)
    # trickle: at night aim at pcc + bat1 = +SOFAR_TRICKLE, the Sofar battery charges a few W steadily
    target = watt(cfg.soyo_target + (cfg.sofar_trickle if r.trickle else 0.0))
    deficit = watt(c.own_discharge) - b1_def - c_div(KP_PM * (house - target), 1000)
    prop = bm_d3(1, house < PCC_IMPORT_TH, deficit > SOYO_HOLD_TH, r.prop_hold, st.soyo_prop_prev)
    st.soyo_prop_prev = prop
    w, wp_cap = dis_amount(prop, deficit, r.idle_w, int(cfg.w_max), c.wp_cap_armed, int(cfg.wp_cap))
    rule = "%s WP:%s SRC:%s" % ("PROP" if prop else "IDLE", out.wp_why, out.src_why)
    alloc_discharge(st.lead, w, c.discharge, int_caps(out.dis_cap), rule, out.sp, out.why)
    if wp_cap:
        for p in range(NPH):
            if c.discharge[p]:
                out.why[p] = (out.why[p] + " WPCAP")[:WHY_LEN - 1]


def chain_charge(cfg, st, out, c):
    """S4 + S5 charge: own charge + KP x (Z2 surplus incl. the Sofar charge share), at most the phase ceilings, never
    below 0"""
    cap = int_caps(out.chg_cap)
    cap_sum = sum(cap[p] for p in range(NPH) if c.charge[p])
    # D4 cap (0.49-c): aim at PCC = +EXPORT_CAP, so only the export above the cap goes into the stacks
    target = watt(cfg.soyo_target + (cfg.export_cap if c.cap_active else 0.0))
    want = watt(c.own_charge) + c_div(KP_PM * (watt(c.chg_ref + c.bat1_eff) - target), 1000)
    if want < 0:
        want = 0
    alloc_charge(cfg, st.lead, min(want, cap_sum), c.charge, c.n_chg, cap, out.sp)
    for p in range(NPH):
        if c.charge[p]:
            out.why[p] = (out.why[p] + (" BAL" if st.lead >= 0 else " EQ"))[:WHY_LEN - 1]
            if c.cap_active:
                out.why[p] = (out.why[p] + " CAP")[:WHY_LEN - 1]


def chain_pi(cfg, inp, st, out, c):
    """PI prototype (shadow unless BATMONITOR_PI=1) on y = PCC + Sofar Bat1, same gates and split"""
    pi_ok, pi_chg, pi_dis = [0] * NPH, [0] * NPH, [0] * NPH
    n_pichg = n_pidis = 0
    for b, bank in enumerate(BM_BANKS):
        for p in bank.phases:
            if not st.soc_ok[b] or st.force[b] or c.stale:
                continue
            pi_ok[p] = 1
            if not c.block and st.soc[b] < 100.0:
                pi_chg[p] = 1
                n_pichg += 1
            if not st.prot[b]:
                pi_dis[p] = 1
                n_pidis += 1
    applied = 0.0
    for p in range(NPH):
        if pi_ok[p]:
            applied += st.setpoints[p]
    b1 = inp.bat1 * BAT1_CHARGE_FACTOR if inp.bat1 > 0 else (inp.bat1 if (applied > 0 or c.night_floor) else 0.0)
    y = (c.z2 if inp.have_pcc else 0.0) + b1
    e = y - PI_TARGET
    if abs(e) < PI_DEADBAND:
        e = 0
    dt = fmin(fmax(inp.now - st.pi_t_prev, 1.0), 30.0) if st.pi_t_prev > 0 else CYCLE_SECONDS
    u = applied + (PI_KP * (e - st.pi_e_prev) if st.pi_t_prev > 0 else 0) + PI_KI * dt * e
    dis_cap = cfg.wp_cap if (not c.wp_fresh and c.winter and c.wp_running) else cfg.w_max
    u_max = 0.0
    u_min = -dis_cap if n_pidis else 0
    for p in range(NPH):
        if pi_chg[p]:
            u_max += out.chg_cap[p]
    if applied <= 0 and u > 0:               # crossing into charging: never more than the surplus at the Z2 point
        u = fmax(0.0, fmin(u, (c.z2 if inp.have_pcc else 0.0) + b1 - PI_TARGET))
    u = fmax(u_min, fmin(u_max, u))
    st.pi_e_prev = e
    st.pi_t_prev = inp.now
    psp = [0] * NPH
    pwhy = [w[:95] for w in out.why]
    for p in range(NPH):
        if pi_ok[p]:
            pwhy[p] = "PI(%+.0f)" % u
    if u > 0:
        alloc_charge(cfg, st.lead, int(u), pi_chg, n_pichg, int_caps(out.chg_cap), psp)
    elif u < 0:
        alloc_discharge(st.lead, int(-u), pi_dis, int_caps(out.dis_cap), "PI(%+.0f)" % u, psp, pwhy)
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
    chain_d1(cfg, inp, st, out, c)
    chain_gates(cfg, inp, st, out)
    chain_mode(cfg, inp, st, out, c)
    chain_lead(cfg, st, out)
    if c.n_dis:
        chain_discharge(cfg, inp, st, out, c)
    if c.n_chg:
        chain_charge(cfg, st, out, c)
    for p in range(NPH):                     # the BMS limit cut this phase's setpoint
        if ((out.sp[p] > 0 and out.ccl_bind[p] and out.sp[p] >= int(out.chg_cap[p]) - 1)
                or (out.sp[p] < 0 and out.dcl_bind[p] and -out.sp[p] >= int(out.dis_cap[p]) - 1)):
            out.why[p] = (out.why[p] + (" CCL" if out.sp[p] > 0 else " DCL"))[:WHY_LEN - 1]
    st.soyo_chg_prev = int(c.n_chg > 0)
    if not c.n_dis:
        st.soyo_prop_prev = 0                # D3 with no discharging phase: reset
    pcc_over = bool(inp.have_pcc and inp.now - inp.inv_time < DO4_PCC_MAX_AGE and inp.pcc > PCC_PEAK_TH)
    if not pcc_over:
        st.do4_over_since = 0.0
    elif st.do4_over_since <= 0:
        st.do4_over_since = inp.now
    out.do4_pulse = bm_d5(pcc_over, pcc_over and inp.now - st.do4_over_since >= cfg.do4_grace_s,
                          pcc_over and inp.pcc > cfg.do4_hard_w,
                          st.do4_last <= 0 or inp.now - st.do4_last >= DO4_LOCKOUT)
    if out.do4_pulse:
        st.do4_last = inp.now
    chain_pi(cfg, inp, st, out, c)
    out.surplus = c.surplus
    out.wp_eff = c.wp_eff
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
    lead: int = -1


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
    """steps from now to sunset in FC_STEP: the stacks get their measured charge power plus the change of the expected
    PV against now, split like alloc_charge (priority order, lead last, each up to its charger ceiling); the lead follows
    the simulated SoC; a full stack's power moves to the other one"""
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
    lead = fi.lead
    t = fi.t + FC_STEP
    while t < fi.t + 86400:
        pv, _ = fc_pv(fi, t - FC_STEP // 2, out.anchor)
        out.rest_kwh += pv * FC_STEP / 3600000.0
        if active[0] and active[1]:                             # update_lead on the simulated SoC
            d = soc[0] - soc[1]
            if lead < 0 and abs(d) > cfg.soc_balance_on:
                lead = 0 if d > 0 else 1
            elif lead >= 0 and (d if lead == 0 else -d) < cfg.soc_balance_off:
                lead = -1
        rest = fmax(0.0, p0 + FC_CHARGE_EFF * (pv - pv0))
        for b in charge_order(lead):                            # alloc_charge: priority order up to the charger
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
