/*
 * bm_logic: one batmonitor cycle as a pure function (see bm_logic.h). The rules and their history are described in
 * batmonitor.c; this file holds the arithmetic only, unchanged from calc() of 0.19-c.
 */
#define _GNU_SOURCE
#include "bm_logic.h"
#include <math.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* soyo, 1:1 from sofar_waveshare.yaml, power values doubled (POWER_SCALE) */
#define POWER_SCALE 2
#define KP_PM 1010             /* proportional gain in per mille: the discharge path is whole watts (0.45-c) */
#define KP (KP_PM / 1000.0)
#define B_DAY_IDLE (10 * POWER_SCALE)
#define PCC_IMPORT_TH -100.0
#define PCC_SURPLUS_TH 200.0
#define SOYO_HOLD_TH 20.0       /* W: charging / proportional discharging continues while surplus / deficit is above */
#define CHARGING_TH 200.0
#define NIGHT_PV_TH 100.0
#define SUMMER_FROM 5           /* SUMMER_MONTHS = range(5, 10) */
#define SUMMER_TO 9
#define STALE_SECONDS 180.0
#define WP_MAX_AGE 150.0        /* s, older: no Z2 correction, WP_CAP as before */
#define WP_ON_TH 300.0          /* W: heat pump counts as running (no night floor) */
#define SEASON_MAX_AGE (2 * 86400)   /* s: older batmonitor/season -> month rule */
#define FC_MAX_AGE (3 * 3600)        /* s: older batmonitor/forecast is ignored */
#define BAT1_FIRST_HYST 2.0          /* %: Sofar-first back on above BAT1_SOC_MIN + this */
/* batmonitor */
#define BAT1_CHARGE_FACTOR 0.5
/* charge block, 1:1 from waveshare/fox2db_logic.h (fox2db v2.9) and sofar_waveshare.yaml */
#define LADESPERRE_FROM 5
#define LADESPERRE_TO 8
#define LADESPERRE_RATIO 0.50
#define LADESPERRE_HYST 0.25
#define LADESPERRE_NOW_RATIO 0.20
#define PCC_PEAK_TH 20000.0
#define DC_RATIO_MIN 5000.0
#define AUSSEN_MAX_AGE 900.0
#define LAT 47.6811
#define LON 11.5732
#define HOR_AZ_SPLIT 120.0
#define HOR_EAST 23.0
#define HOR_SOUTH 15.0
#define HOR_DIFFUSE 0.25
#define TEMP_COEFF -0.0030
#define NOCT 45.0
#define T_STC 25.0
/* PI prototype: u(k) = u_applied(k-1) + KP*(e(k) - e(k-1)) + KI*dt*e(k), e = y - PI_TARGET, clamped to the
   phases that may charge / discharge (velocity form: no integrator to wind up, bumpless when switched on) */
#define PI_TARGET 0.0           /* W at the PCC (user 2026-10-08: PCC near 0) */
#define PI_DEADBAND 75.0        /* |e| below this counts as 0 (PCC resolution 10 W, 4 s samples) */
#define PI_KP 0.2               /* on the change of e */
#define PI_KI 0.06              /* 1/s: KI * 5 s = 0.3 of the error per cycle (plant gain ~1, one cycle delay) */
/* full_at forecast (0.28-c) */
#define FC_STEP 300             /* s */
#define FC_ANCHOR_TAU 5400.0    /* s: the measured/model ratio of now fades into the weather forecast */
#define FC_ANCHOR_MIN_W 500.0   /* modelled Sofar PV below this: no anchor (dawn, dusk) */
#define FC_SLOT_REACH 5400      /* s: an OWM slot stands for its time +- 1.5 h */
#define FC_CHARGE_EFF 0.90      /* AC-in -> stack DC at the BMS (measured 2026-10-09: 0.90-0.92) */

const char *BM_PH[NPH] = {"L1", "L2", "L3"};
/* Stack1 MUST feeds the middle unit = HQ2606P4NCH = Devices/0 = L1 (user photo 2026-10-08) */
const struct bm_bank BM_BANKS[NBANK] = {
	{"STACK1_MUST", "com.victronenergy.battery.socketcan_can0", 1, {0}, 300 * 51.2},
	{"STACK2_PYTES", "com.victronenergy.battery.ebox", 2, {1, 2}, 300 * 51.2},
};
static const int CHARGE_PRIORITY[NBANK] = {0, 1};   /* Stack1 first (one 70 A charger), then Stack2 */

struct arr { double tilt, az_s, power; };
/* fitted per string (gen_pv_strings.py, 14 clear days): Sofar PV1 west / PV2 south, FoxESS pv2 east / pv1 west */
static const struct arr ARRAYS[2] = {{60, 33, 19430}, {68, -12, 7690}};
static const struct arr ARRAYS_EAST[2] = {{59, -29, 22036}, {67, 32, 2781}};
static const double KT_MONTH[13] = {0, .331, .402, .563, .838, .909, .880, .840, .820, .760, .600, .350, .134};

#define P(field) (unsigned)offsetof(struct bm_cfg, field)
const struct bm_param BM_PARAMS[] = {
	/* name                   field                         default  min      max      unit */
	{"W_MAX",                 P(w_max),                     10000,   0,       12000,   "W", "total discharge (soyo was 1800 W)"},
	{"DISCHARGE_MAX_PHASE",   P(discharge_max_phase),       3800,    0,       5000,    "W", "discharge per phase (0.34: below the MP2 4 kW limit, better efficiency; SoC balancing catches up)"},
	{"CHARGE_MAX_PHASE",      P(charge_max_phase),          4900,    0,       5000,    "W", "PV charge per phase (5000 VA peaks)"},
	{"CHARGER_CAP_PHASE",     P(charger_cap_phase),         4200,    0,       5000,    "W", "real charger capacity per phase, priority fill"},
	{"WP_BAT_MAX_TRANSITION", P(wp_bat_max_transition),     1900,    0,       10000,   "W", "heat pump share from the batteries in the transition"},
	{"WP_CAP",                P(wp_cap),                    1000,    0,       10000,   "W", "total discharge with R290 running if em0/power is stale (winter)"},
	{"SOC_MIN",               P(soc_min),                   5,       0,       50,      "%", "no discharge below"},
	{"SOC_MIN_RELEASE",       P(soc_min_release),           7,       0,       60,      "%", "discharge again from"},
	{"SOC_FORCE",             P(soc_force),                 3,       0,       30,      "%", "grid force charge below"},
	{"SOC_FORCE_RELEASE",     P(soc_force_release),         5,       0,       40,      "%", "force charge until"},
	{"FORCE_CHARGE_W",        P(force_charge_w),            500,     0,       4000,    "W", "force charge per phase"},
	{"SOC_BALANCE_ON",        P(soc_balance_on),            3,       0.5,     50,      "%", "SoC difference that starts balancing"},
	{"SOC_BALANCE_OFF",       P(soc_balance_off),           1,       0,       50,      "%", "balancing ends below"},
	{"SOYO_TARGET",           P(soyo_target),               0,       -500,    500,     "W", "PCC target (+ = export)"},
	{"FC_HYST",               P(fc_hyst),                   2,       0,       20,      "%", "forecast rule back on above target + this"},
	{"SOFAR_TRICKLE",         P(sofar_trickle),             30,      0,       500,     "W", "night: stacks give this much more than the house needs, the Sofar battery charges gently instead of swinging"},
	{"CHARGER_A",             P(charger_a),                 70,      0,       70,      "A", "MultiPlus-II 48/5000 charger current per unit (DC), ceiling = this x BMS voltage"},
	{"CHARGE_EFF",            P(charge_eff),                0.93,    0.7,     1.0,     "",  "AC-in per DC at the charger limit (measured 2026-10-09: 4050 W AC at 70 A x 53.7 V)"},
	{"BAT1_SOC_MIN",          P(bat1_soc_min),              5,       0,       101,     "%", "night: the Sofar Bat1 serves house + heat pump first down to this SoC, only then the stacks (Sofar DOD 95 %); 101 = off (stacks take over the Sofar as 0.25)"},
	{"FC_BAD_TARGET",         P(fc_bad_target),             100,     0,       101,     "%", "bad forecast when target_soc >= this (100 = the day's PV does not exceed the day load, free_kwh 0); 101 = off"},
	{"WP_BAT_SHARE_BAD",      P(wp_bat_share_bad),          50,      0,       100,     "%", "bad forecast: the stacks cover at most this share of the heat pump (summer/transition, winter stays 0)"},
	{"EXPORT_CAP",            P(export_cap),                18000,   0,       30000,   "W", "summer peak window: while the forecast expects export above this, the stacks charge only the export above it (0 = off)"},
	{"HOUSE_EST",             P(house_est),                 1000,    0,       5000,    "W", "summer peak window: house load assumed when turning the PV forecast into export"},
	{"DO4_GRACE_S",           P(do4_grace_s),               120,     0,       600,     "s", "DO4 (FoxESS shedding) only after the PCC stays above 20 kW this long: R290 boost and air conditioning first"},
	{"DO4_HARD_W",            P(do4_hard_w),                23000,   20000,   40000,   "W", "DO4 at once above this PCC export"},
	{"ACOUT_FEED_MAX_W",      P(acout_feed_max_w),          5000,    0,       10000,   "W", "grid offline: DO4 when PV feeds more than this into one MultiPlus AC-out (factor 1.0 rule, 5000 VA; 0 = off)"},
	{"ACOUT_FEED_S",          P(acout_feed_s),              10,      0,       120,     "s", "... for this long"},
	{"CHARGER_DC_W",          P(charger_dc_w),              3600,    500,     5000,    "W", "full_at forecast: DC at the BMS per MultiPlus at its limit (measured 2026-10-09: ~3.6 kW, 65-67 A, flat over 53.6-54.6 V)"},
};
const int BM_NPARAMS = sizeof(BM_PARAMS) / sizeof(BM_PARAMS[0]);

double *bm_param_ptr(struct bm_cfg *cfg, int i)
{
	return (double *)((char *)cfg + BM_PARAMS[i].off);
}

int bm_param_set(struct bm_cfg *cfg, int i, double v)
{
	if (i < 0 || i >= BM_NPARAMS || !(v >= BM_PARAMS[i].min && v <= BM_PARAMS[i].max))
		return -1;
	*bm_param_ptr(cfg, i) = v;
	return 0;
}

void bm_cfg_default(struct bm_cfg *cfg)
{
	memset(cfg, 0, sizeof(*cfg));
	cfg->ladesperre = 1;
	cfg->forecast = 1;
	for (int i = 0; i < BM_NPARAMS; i++)
		*bm_param_ptr(cfg, i) = BM_PARAMS[i].def;
}

void bm_init(struct bm_state *st)
{
	memset(st, 0, sizeof(*st));
	st->lead = -1;
	st->ls_yday = -1;
}

/* ---- clear-sky DC model (fox2db_logic.h) -------------------------------------------------------- */

static double d2r(double d) { return d * M_PI / 180.0; }

/* NOAA sun position: elevation + azimuth from north for unix UTC */
void bm_sun_pos(time_t t, double *elev, double *az)
{
	struct tm g;
	gmtime_r(&t, &g);
	double hour = g.tm_hour + g.tm_min / 60.0 + g.tm_sec / 3600.0;
	double gamma = 2.0 * M_PI / 365.0 * (g.tm_yday + (hour - 12) / 24.0);
	double eqtime = 229.18 * (0.000075 + 0.001868 * cos(gamma) - 0.032077 * sin(gamma)
							  - 0.014615 * cos(2 * gamma) - 0.040849 * sin(2 * gamma));
	double decl = 0.006918 - 0.399912 * cos(gamma) + 0.070257 * sin(gamma)
				- 0.006758 * cos(2 * gamma) + 0.000907 * sin(2 * gamma)
				- 0.002697 * cos(3 * gamma) + 0.00148 * sin(3 * gamma);
	double har = d2r((hour * 60.0 + eqtime + 4.0 * LON) / 4.0 - 180.0), latr = d2r(LAT);
	double cosz = sin(latr) * sin(decl) + cos(latr) * cos(decl) * cos(har);
	cosz = fmax(-1.0, fmin(1.0, cosz));
	*elev = 90.0 - acos(cosz) * 180.0 / M_PI;
	double az_s = atan2(sin(har), cos(har) * sin(latr) - tan(decl) * cos(latr));
	*az = fmod(az_s * 180.0 / M_PI + 180.0 + 360.0, 360.0);
}

static double calc_arrays_kt(const struct arr *a, int n, time_t t, double kt)
{
	double elev, az;
	bm_sun_pos(t, &elev, &az);
	if (elev <= 0)
		return 0.0;
	double am = fmin(1.0 / sin(d2r(elev)), 37.0);
	double tr = pow(0.7, pow(am, 0.678));
	double shade = (elev >= ((az < HOR_AZ_SPLIT) ? HOR_EAST : HOR_SOUTH)) ? 1.0 : HOR_DIFFUSE;
	double e = d2r(elev), sum = 0;
	for (int i = 0; i < n; i++) {
		double b = d2r(a[i].tilt), da = d2r((az - 180.0) - a[i].az_s);
		sum += a[i].power * tr * kt * fmax(0.0, sin(e) * cos(b) + cos(e) * sin(b) * cos(da));
	}
	return sum * shade;
}

static double calc_arrays(const struct arr *a, int n, time_t t, int month)
{
	return calc_arrays_kt(a, n, t, (month >= 1 && month <= 12) ? KT_MONTH[month] : 0.60);
}

double bm_dc_now(time_t t, int month)
{
	return calc_arrays(ARRAYS, 2, t, month) + calc_arrays(ARRAYS_EAST, 2, t, month);
}

static time_t solar_noon_utc(time_t ref)
{
	time_t midnight = (ref / 86400) * 86400, tmid = midnight + 12 * 3600;
	struct tm g;
	gmtime_r(&tmid, &g);
	double gamma = 2.0 * M_PI / 365.0 * g.tm_yday;
	double eqtime = 229.18 * (0.000075 + 0.001868 * cos(gamma) - 0.032077 * sin(gamma)
							  - 0.014615 * cos(2 * gamma) - 0.040849 * sin(2 * gamma));
	return midnight + (time_t)((720.0 - eqtime - 4.0 * LON) / 60.0 * 3600.0);
}

static double dc_temp_factor(double elev, double ambient)
{
	double cell = ambient + (NOCT - 20.0) / 800.0 * 1000.0 * fmax(0.0, sin(d2r(elev)));
	return fmax(0.85, fmin(1.0, 1.0 + TEMP_COEFF * (cell - T_STC)));
}

/* ---- control ------------------------------------------------------------------------------------ */

static double fc_kt(const struct bm_fc_in *in, time_t t, int *weather);
static double fc_pv(const struct bm_fc_in *in, time_t t, double anchor, int *weather);

#define CAP_STEP 900               /* s: forecast scan step of the peak window */
#define CAP_DAY_END_H 21           /* local hour: the scan ends here */

/* the model part of the summer peak window (0.49-c) and D4 on its bits. The forecast scan: expected PV of both
   inverters from now to CAP_DAY_END_H (clear sky x OWM slot x correction, the measured/modelled Sofar PV of now as
   anchor fading over FC_ANCHOR_TAU, as bm_full_forecast), minus HOUSE_EST = expected export. Returns block (wait below
   the cap); *cap_active = charge only the export above EXPORT_CAP. multis = signed AC-in sum of the Multis */
static int ladesperre(const struct bm_cfg *cfg, const struct bm_in *in, struct bm_state *st, struct bm_out *out,
					  int stale, double multis, double surplus, int *cap_active)
{
	time_t now = in->t;
	struct tm loc, mid;
	localtime_r(&now, &loc);
	int month = loc.tm_mon + 1;
	int new_day = loc.tm_yday != st->ls_yday;
	st->ls_yday = loc.tm_yday;
	double dc = bm_dc_now(now, month);
	if (dc > 0 && in->aussen_time > 0 && in->now - in->aussen_time < AUSSEN_MAX_AGE) {
		double elev, az;
		bm_sun_pos(now, &elev, &az);
		dc *= dc_temp_factor(elev, in->aussen);
	}
	mid = loc;
	mid.tm_hour = mid.tm_min = mid.tm_sec = 0;
	mid.tm_isdst = -1;
	time_t midnight = mktime(&mid);
	time_t noon = solar_noon_utc(now);
	/* measured PV minus house load against the model (display, r290_boost reads ratio_now) */
	double ratio = -1.0, ratio_now = -1.0;
	if (dc > DC_RATIO_MIN && !stale && in->have_pcc) {
		ratio = (dc - (in->pcc_avg5 + multis + in->bat1_avg5)) / dc;
		ratio_now = (dc - (in->pcc + multis + in->bat1)) / dc;
	}
	/* forecast scan */
	struct bm_fc_in fi;
	memset(&fi, 0, sizeof(fi));
	fi.t = now;
	fi.corr = in->fc_corr;
	fi.n_slots = in->fc_n < BM_FC_SLOTS ? in->fc_n : BM_FC_SLOTS;
	memcpy(fi.slot_t, in->fc_slot_t, sizeof(fi.slot_t));
	memcpy(fi.slot_kt, in->fc_slot_kt, sizeof(fi.slot_kt));
	int w;
	double clear_sofar = calc_arrays_kt(ARRAYS, 2, now, 1.0) * fc_kt(&fi, now, &w), anchor = 1.0;
	if (in->have_pv && !stale && clear_sofar >= FC_ANCHOR_MIN_W)
		anchor = fmax(0.05, fmin(3.0, in->pv / clear_sofar));
	double best = 0;
	int peak_h = -1, end_h = -1, ahead = 0;
	for (time_t t = now; t <= midnight + CAP_DAY_END_H * 3600; t += CAP_STEP) {
		double pv = fc_pv(&fi, t, anchor, &w);
		struct tm lt;
		localtime_r(&t, &lt);
		if (pv > best) {
			best = pv;
			peak_h = lt.tm_hour;
		}
		if (pv - cfg->house_est > cfg->export_cap) {
			ahead = 1;
			end_h = lt.tm_hour;
		}
	}
	struct bm_d4_in d = {new_day, cfg->ladesperre && cfg->export_cap > 0, month >= LADESPERRE_FROM && month <= LADESPERRE_TO,
						 ahead, in->have_pcc && !stale && in->pcc > PCC_PEAK_TH,
						 surplus - cfg->export_cap > PCC_SURPLUS_TH, surplus - cfg->export_cap > SOYO_HOLD_TH,
						 st->soyo_chg_prev};
	struct bm_d4_state s0 = {st->peak_today}, s1;
	int block = bm_d4(&d, &s0, &s1, cap_active);
	st->peak_today = s1.peak_today;
	out->ls.active = block;
	out->ls.cap = *cap_active;
	out->ls.dc = dc;
	out->ls.ratio = ratio;
	out->ls.ratio_now = ratio_now;
	out->ls.peak_h = ahead ? peak_h : -1;              /* r290_boost: a danger window today, until win_end_h */
	out->ls.win_end_h = end_h;
	out->ls.noon_h = (double)(noon - midnight) / 3600.0;
	return block;
}

static int bank_of(int p)
{
	for (int b = 0; b < NBANK; b++)
		for (int i = 0; i < BM_BANKS[b].nph; i++)
			if (BM_BANKS[b].ph[i] == p)
				return b;
	return -1;
}

/* S5 split core (0.48-c, both directions, whole watts): w over the phases with lvl[p] >= 0, level by level. Within a
   level every bank the same share, split over its phases; what a phase cannot take above cap[p] goes to the level's
   other phases alike (water filling, at most NPH rounds). Only what the caps cut goes on to the next level, not the
   division remainder; returns what the caps cut in the last level. give[p] in 0..cap[p], the total never above w */
static int alloc_levels(int w, const int *lvl, int n_lvl, const int *cap, int *give)
{
	int rest = w;
	for (int p = 0; p < NPH; p++)
		give[p] = 0;
	for (int l = 0; l < n_lvl; l++) {
		int nb[NBANK] = {0}, n_banks = 0, want[NPH] = {0}, cut = 0;
		for (int p = 0; p < NPH; p++)
			if (lvl[p] == l && !nb[bank_of(p)]++)
				n_banks++;
		if (!n_banks)
			continue;
		for (int p = 0; p < NPH; p++)
			if (lvl[p] == l) {
				want[p] = rest / n_banks / nb[bank_of(p)];
				cut += want[p];
			}
		for (int k = 0; k < NPH; k++) {                    /* water filling, at most NPH rounds */
			int over = 0, n_free = 0;
			for (int p = 0; p < NPH; p++)
				if (lvl[p] == l) {
					if (want[p] > cap[p]) {
						over += want[p] - cap[p];
						want[p] = cap[p];
					} else if (want[p] < cap[p])
						n_free++;
				}
			if (!over || !n_free)
				break;
			for (int p = 0; p < NPH; p++)
				if (lvl[p] == l && want[p] < cap[p])
					want[p] += over / n_free;
		}
		for (int p = 0; p < NPH; p++)
			if (lvl[p] == l) {
				give[p] = want[p] < cap[p] ? want[p] : cap[p];
				cut -= give[p];
			}
		rest = cut;                                     /* what the caps cut goes on, not the division remainder */
	}
	return rest;
}

/* S5 discharge split: with a lead (SoC ahead) the lead bank is level 0 and gives first (BAL); the other bank is level
   1 and gives only what the lead cannot (SPILL, else WAIT). Without a lead (0.26, user 2026-10-08) one level: every
   discharging bank the same share, so the two 300 Ah stacks drain alike (Stack1 alone on L1 gives as much as Stack2
   on L2+L3) */
static void alloc_discharge(int lead, int w, const int *dis, const int *dcap, const char *rule, int *sp,
							char why[][WHY_LEN])
{
	int lvl[NPH], give[NPH], n_lead = 0;
	for (int p = 0; p < NPH; p++) {
		lvl[p] = dis[p] ? 0 : -1;
		n_lead += dis[p] && bank_of(p) == lead;
	}
	for (int p = 0; p < NPH; p++)
		if (n_lead && dis[p] && bank_of(p) != lead)
			lvl[p] = 1;
	alloc_levels(w, lvl, 2, dcap, give);
	for (int p = 0; p < NPH; p++)
		if (dis[p]) {
			sp[p] = -give[p];
			snprintf(why[p], WHY_LEN, "%s%s", rule, !n_lead ? " EQ" : !lvl[p] ? " BAL" : sp[p] ? " SPILL" : " WAIT");
		}
}

/* whole watts from a measurement or ceiling: NaN -> 0, clamped to +-1 MW */
static int watt(double v)
{
	if (isnan(v))
		return 0;
	return v > 1e6 ? 1000000 : v < -1e6 ? -1000000 : (int)lround(v);
}

static void dis_caps(const struct bm_out *out, int *dcap)
{
	for (int p = 0; p < NPH; p++)
		dcap[p] = out->dis_cap[p] > 0 ? (int)out->dis_cap[p] : 0;
}

double bm_charge_cap(const struct bm_cfg *cfg, int volt_ok, double volt)
{
	if (!volt_ok || volt < 40 || volt > 62)
		return cfg->charger_cap_phase;
	return fmin(cfg->charge_max_phase, cfg->charger_a * volt / cfg->charge_eff);
}

double bm_bms_cap_phase(const struct bm_cfg *cfg, int bank, int volt_ok, double volt, double amps, int charge)
{
	double u = (volt_ok && volt >= 40 && volt <= 62) ? volt : BM_NOMINAL_V;
	double w = fmax(0.0, amps) * u;
	return (charge ? w / cfg->charge_eff : w * cfg->charge_eff) / BM_BANKS[bank].nph;
}

/* banks in charge order: CHARGE_PRIORITY, the lead bank (SoC ahead) last; returns the count */
static int charge_order(int lead, int *order)
{
	int k = 0;
	for (int i = 0; i < NBANK; i++)
		if (CHARGE_PRIORITY[i] != lead)
			order[k++] = CHARGE_PRIORITY[i];
	if (lead >= 0)
		order[k++] = lead;
	return k;
}

/* S5 charge split (0.48-c, the same core, whole watts): one level per bank in CHARGE_PRIORITY order, the lead bank
   (SoC ahead) last, each up to its real charger capacity (CHARGER_CAP_PHASE, 0.29-c: cap[] = charger 70 A x BMS
   voltage, BMS CCL); what the caps cut goes up to CHARGE_MAX_PHASE on all charging phases alike */
static void alloc_charge(const struct bm_cfg *cfg, int lead, int rest, const int *chg, int n_chg, const int *cap,
						 int *sp)
{
	int order[NBANK], k = charge_order(lead, order), lvl[NPH], cap1[NPH], give[NPH];
	for (int p = 0; p < NPH; p++) {
		lvl[p] = -1;
		cap1[p] = cap[p] < (int)cfg->charger_cap_phase ? cap[p] : (int)cfg->charger_cap_phase;
		for (int i = 0; i < k && chg[p]; i++)
			if (order[i] == bank_of(p))
				lvl[p] = i;
	}
	rest = alloc_levels(rest, lvl, k, cap1, give);
	for (int p = 0; p < NPH; p++)
		if (chg[p]) {
			int head = (cap[p] < (int)cfg->charge_max_phase ? cap[p] : (int)cfg->charge_max_phase) - give[p];
			int more = rest > 0 ? rest / n_chg : 0;
			sp[p] = give[p] + (more < head ? more : head > 0 ? head : 0);
		}
}

static void chg_caps(const struct bm_out *out, int *cap)
{
	for (int p = 0; p < NPH; p++)
		cap[p] = out->chg_cap[p] > 0 ? (int)out->chg_cap[p] : 0;
}

/* ---- decision layer (0.43-c): pure functions of bits + state, see bm_logic.h -------------------------------- */

const char *BM_WP_NAME[BM_WP_N] = {"NA", "OFF", "FC", "FCBAD", "Z2", "T1900", "ALL"};
const char *BM_SRC_NAME[BM_SRC_N] = {"DAY", "B1", "STK", "B1W"};
const char *BM_MODE_NAME[BM_M_N] = {"BMS_MISSING", "FORCE", "STALE", "LADESPERRE", "CHARGE", "FULL", "PROT", "CHARGING",
									"DIS"};

/* D1. Forecast (0.24): while the lowest stack is above the SoC the next day's PV refills, the stacks serve the heat
   pump as in summer; on above target + FC_HYST, off below target. Bad forecast (0.37-c): target_soc >= FC_BAD_TARGET
   -> the stacks cover at most WP_BAT_SHARE_BAD % of the heat pump (S2). Sofar first (0.38-c): Bat1 above BAT1_SOC_MIN
   (on again from + BAT1_FIRST_HYST) and the heat pump running -> the Sofar delivers first at night (S3) */
/* the discharge matrix rows (bm_logic.h): only the night floor (SRC STK) takes the Sofar battery over */
const struct bm_src_rule BM_SRC_RULE[BM_SRC_N] = {
	/*               b1_signed trickle idle_w      prop_hold */
	[BM_SRC_DAY] = {0,        0,      B_DAY_IDLE, 0},
	[BM_SRC_B1]  = {0,        0,      B_DAY_IDLE, 0},
	[BM_SRC_STK] = {1,        1,      0,          1},
	[BM_SRC_B1W] = {0,        0,      B_DAY_IDLE, 0},
};
/* the mode matrix rows (bm_logic.h) */
const struct bm_mode_rule BM_MODE_RULE[BM_M_N] = {
	/*                      action          capped  why */
	[BM_M_BMS_MISSING] = {BM_A_ZERO,      0,      BM_W_NAME},
	[BM_M_FORCE]       = {BM_A_FORCE,     1,      BM_W_SOC},   /* capped since 0.48-c (was 0: CCL 0 still got 500 W) */
	[BM_M_STALE]       = {BM_A_ZERO,      0,      BM_W_NAME},
	[BM_M_LADESPERRE]  = {BM_A_ZERO,      0,      BM_W_PEAK_H},
	[BM_M_CHARGE]      = {BM_A_CHARGE,    1,      BM_W_CHAIN},
	[BM_M_FULL]        = {BM_A_ZERO,      0,      BM_W_NAME},
	[BM_M_PROT]        = {BM_A_ZERO,      0,      BM_W_SOC},
	[BM_M_CHARGING]    = {BM_A_ZERO,      0,      BM_W_POWER},
	[BM_M_DIS]         = {BM_A_DISCHARGE, 1,      BM_W_NONE},
};
const char *BM_WPK_NAME[BM_WPK_N] = {"NONE", "CAP", "ALL", "BAD"};

void bm_d1(const struct bm_d1_in *b, int fc_active, int bat1_first, struct bm_d1_out *o)
{
	memset(o, 0, sizeof(*o));
	o->fc_ok = b->fc_fresh && b->fc_minsoc_ok;
	if (!o->fc_ok)
		fc_active = 0;
	else if (!fc_active && b->fc_above_on)
		fc_active = 1;
	else if (fc_active && b->fc_below_off)
		fc_active = 0;
	o->fc_active = fc_active;
	o->fc_bad = b->fc_fresh && !fc_active && b->fc_target_bad;
	o->season_wp = fc_active ? BM_SUMMER : b->season;
	o->winter = o->season_wp != BM_SUMMER;           /* transition counts as winter for the WP_CAP fallback */
	if (!b->b1_above_min)
		bat1_first = 0;
	else if (!bat1_first && b->b1_ge_release)
		bat1_first = 1;
	o->bat1_first = bat1_first;
	o->b1_first = bat1_first && b->wp_on;
	/* S3 reads the measured season, not season_wp (0.45-c): the forecast only widens the heat pump scope (S2) */
	o->night_floor = b->night && !o->b1_first && (b->season != BM_WINTER || !b->wp_on);
	o->wp_fc_capped = o->fc_bad && b->wp_pos && b->fc_share_lt_share;
	o->wp_cap_armed = !b->wp_fresh && o->winter && b->wp_running;
	o->wp_reason = !b->wp_fresh ? BM_WP_NA : !b->wp_on ? BM_WP_OFF : fc_active ? BM_WP_FC : o->wp_fc_capped ? BM_WP_FCBAD
				   : o->season_wp == BM_WINTER ? BM_WP_Z2
				   : o->season_wp == BM_TRANSITION && b->wp_gt_trans ? BM_WP_T1900 : BM_WP_ALL;
	o->src_reason = !b->night ? BM_SRC_DAY : o->b1_first ? BM_SRC_B1 : o->night_floor ? BM_SRC_STK : BM_SRC_B1W;
	o->wp_kind = o->wp_fc_capped ? BM_WPK_BAD : o->season_wp == BM_WINTER ? BM_WPK_NONE
				 : o->season_wp == BM_TRANSITION ? BM_WPK_CAP : BM_WPK_ALL;
}

/* D2. Per bank: SoC protection and force charge with hysteresis (only while the BMS reports), then the mode, first
   match: BMS_MISSING > FORCE > STALE > LADESPERRE > CHARGE / FULL > PROT > CHARGING > DIS. Charge mode on the Z2
   surplus, held down to SOYO_HOLD_TH once charging */
void bm_d2(const struct bm_d2_in *b, const int *prot, const int *force, int chg_prev, struct bm_d2_out *o)
{
	memset(o, 0, sizeof(*o));
	o->charge_mode = b->surplus_gt_on || (chg_prev && b->surplus_gt_hold);
	for (int k = 0; k < NBANK; k++) {
		const struct bm_d2_bank *x = &b->bank[k];
		o->prot[k] = prot[k];
		o->force[k] = force[k];
		if (x->bms_ok) {
			if (x->soc_lt_min)
				o->prot[k] = 1;
			else if (x->soc_ge_release)
				o->prot[k] = 0;
			if (x->soc_lt_force)
				o->force[k] = 1;
			else if (x->soc_ge_force_release)
				o->force[k] = 0;
		}
		o->mode[k] = !x->bms_ok ? BM_M_BMS_MISSING : o->force[k] ? BM_M_FORCE : b->stale ? BM_M_STALE
					 : o->charge_mode && b->block ? BM_M_LADESPERRE
					 : o->charge_mode ? (x->soc_lt_100 ? BM_M_CHARGE : BM_M_FULL)
					 : o->prot[k] ? BM_M_PROT : x->charging ? BM_M_CHARGING : BM_M_DIS;
		if (o->mode[k] == BM_M_CHARGE)
			o->chg_prev = 1;
	}
}

/* SoC balancing: the bank more than SOC_BALANCE_ON ahead takes the lead; cleared below SOC_BALANCE_OFF or without
   both BMS */
int bm_dlead(const struct bm_dl_in *b, int lead, int *event)
{
	*event = 0;
	if (!b->both_ok)
		return -1;
	if (lead < 0 && b->diff_gt_on) {
		*event = 1;
		return b->first_ahead ? 0 : 1;
	}
	if (lead >= 0 && b->diff_lt_off) {
		*event = -1;
		return -1;
	}
	return lead;
}

/* D3. PROPORTIONAL while importing, or (already proportional / night floor) while the deficit is above
   SOYO_HOLD_TH; else IDLE. No discharging phase: reset */
int bm_d3(const struct bm_d3_in *b, int prop_prev, int *prop_next)
{
	int prop = b->any_dis && (b->house_import || ((prop_prev || b->prop_hold) && b->deficit_gt_hold));
	*prop_next = prop;
	return prop;
}

/* D4. The cap holds while enabled, in the season and the forecast still expects export above EXPORT_CAP today; then
   the stacks charge only the surplus above the cap (charge mode on that surplus, held down to SOYO_HOLD_TH) and wait
   below it. peak_today: PCC above PCC_PEAK_TH seen today (display), cleared at midnight */
int bm_d4(const struct bm_d4_in *b, const struct bm_d4_state *s, struct bm_d4_state *next, int *cap_active)
{
	*next = *s;
	if (b->new_day)
		next->peak_today = 0;
	if (b->pcc_peak)
		next->peak_today = 1;
	*cap_active = b->enabled && b->season && b->fc_cap_ahead;
	int cap_charge = b->surplus_cap_on || (b->chg_prev && b->surplus_cap_hold);
	return *cap_active && !cap_charge;
}

/* D5. A fresh PCC above PCC_PEAK_TH for DO4_GRACE_S or above DO4_HARD_W, or an AC-out feed above ACOUT_FEED_MAX_W for
   ACOUT_FEED_S (protects the MultiPlus, no grace), and no pulse within DO4_LOCKOUT -> pulse */
int bm_d5(const struct bm_d5_in *b)
{
	return ((b->pcc_over && (b->grace_over || b->pcc_hard)) || b->acout_over) && b->lockout_over;
}

/* ---- the rule chain (0.41-c): one function per stage, run in this order by bm_step ---------------------------------
   inputs     derived facts: data age, heat pump running, season, night, own AC-in
   forecast   FC_ACTIVE / FC_BAD from batmonitor/forecast
   S0 gates   per bank: SoC protection / force charge state, charge and discharge ceilings (charger, BMS CCL/DCL)
   S2 scope   what the stacks may cover of the heat pump: wp_eff = the part they must NOT cover
   S3 source  who delivers first at night: the Sofar Bat1 (B1FIRST) or the stacks (night floor)
   S1 mode    per phase, first match: BMS_MISSING > FORCE_CHARGE > STALE > LADESPERRE > PV_SURPLUS >
              DISCHARGE_PROTECTION > CHARGING > discharge
   S4 amount  discharge W (PROPORTIONAL / IDLE + tags), charge W
   S5 split   alloc_discharge / alloc_charge over the phases, BMS limit tags
   D5         DO4 pulse "curtail WR2" (pv_relay/DO4) when the PCC export passes 20 kW, at most every 5 min
   PI         shadow prototype (not armed), reads the same stages
   A stage reads the results of the stages before it and changes none of them. 0.43-c: the decisions (which rule)
   are bm_d1 / bm_d2 / bm_dlead / bm_d3 above; the stages compute their bits and the amounts.
   Rule text per phase (0.42-c, one reason per stage, at most 48 characters for pv_victron.bm_lX_rule):
     <S1/S4 mode> [WP:<S2>] [SRC:<S3>] [<S5 split>] [<limit>]
     mode   BMS_MISSING  FORCE(soc)  STALE  LADESPERRE(peak h)  CHARGE  FULL  PROT(soc)  CHARGING(W)  PROP  IDLE
     WP     OFF (< WP_ON_TH)  NA (em0/power stale)  FC (forecast: all)  FCBAD (bad forecast cap)  Z2 (winter: none)
            T1900 (transition cap binds)  ALL
     SRC    DAY  STK (night floor: stacks take over the Sofar)  B1 (Sofar first, B1FIRST)  B1W (winter, WP running)
     split  EQ (same power per stack)  BAL (lead stack alone / charged last)  SPILL (lead at its cap, rest to the
            others)  WAIT (the other stack while the lead discharges)
     limit  WPCAP  CCL  DCL
     PI(+u) when the PI prototype is armed */
struct chain {
	int stale, wp_running, wp_fresh, wp_on, night;
	int season_wp;                     /* season for the heat pump rules: FC_ACTIVE serves it as in summer (0.24) */
	int winter;                        /* season_wp != summer: WP_CAP fallback with stale em0/power */
	double own_charge, own_discharge, multis;
	double wp_eff;                     /* S2: heat pump W the stacks must NOT cover */
	int wp_fc_capped;                  /* S2: the bad-forecast cap binds */
	double z2;                         /* PCC + wp_eff: the point the stacks regulate on */
	int b1_first, night_floor;         /* S3 */
	int src, wp_kind;                  /* the discharge matrix rows: enum bm_src_reason, enum bm_wp_kind */
	int wp_cap_armed;                  /* WP_CAP fallback: em0/power stale, R290 running, not summer */
	double bat1_eff, chg_ref, surplus;
	int block, charge_mode;
	int cap_active;                    /* D4: charge only the export above EXPORT_CAP */
	int discharge[NPH], charge[NPH], n_dis, n_chg;
};

static void chain_inputs(const struct bm_in *in, struct bm_out *out, struct chain *c)
{
	struct tm loc;
	localtime_r(&in->t, &loc);
	int month = loc.tm_mon + 1;
	c->stale = in->inv_time == 0 || in->now - in->inv_time > STALE_SECONDS;
	c->wp_running = in->r290_time > 0 && in->now - in->r290_time < STALE_SECONDS && in->r290_hz > 0;
	c->wp_fresh = in->wp_time > 0 && in->now - in->wp_time < WP_MAX_AGE;
	c->wp_on = c->wp_fresh && in->wp >= WP_ON_TH;
	c->night = in->have_pv && in->pv < NIGHT_PV_TH;
	/* Winter/summer from the measured energy (season.py on .218: summer when export > 2 x heat pump over 7 days,
	   winter below 1 x, 0.21), the months only when that is missing or older than 2 days */
	int season_ok = in->season && in->t - in->season_ts < SEASON_MAX_AGE;
	out->season = season_ok ? in->season : (month >= SUMMER_FROM && month <= SUMMER_TO) ? BM_SUMMER : BM_WINTER;
	out->season_measured = season_ok;
	/* the meter already includes our own charging: use the measured AC-in, not the setpoints */
	for (int p = 0; p < NPH; p++)
		if (in->ac_ok[p]) {
			double a = in->ac_in[p];
			c->multis += a;
			if (a > 0)
				c->own_charge += a;
			else
				c->own_discharge -= a;
		}
}

/* forecast, S3 source, S2 scope: bits for bm_d1, then the heat pump power the stacks must NOT cover (Z2 point =
   Z1 PCC + that, 0.19): all of it in winter, the part above WP_BAT_MAX_TRANSITION in the transition (0.22), none in
   summer, with a bad forecast all but WP_BAT_SHARE_BAD % (0.37-c) */
static double lowest_soc(const struct bm_in *in)
{
	double min_soc = 101;
	for (int k = 0; k < NBANK; k++)
		if (in->bms_ok[k] && in->soc[k] < min_soc)
			min_soc = in->soc[k];
	return min_soc;
}

/* the predicates of bm_d1 from the inputs (tests/cbmc_harness.c proves the constraints check_matrix assumes) */
static void d1_bits(const struct bm_cfg *cfg, const struct bm_in *in, const struct bm_out *out, const struct chain *c,
					struct bm_d1_in *b)
{
	double min_soc = lowest_soc(in);
	double base_share = out->season == BM_WINTER ? 0.0
						: out->season == BM_TRANSITION ? fmin(in->wp, cfg->wp_bat_max_transition) : in->wp;
	memset(b, 0, sizeof(*b));
	b->season = out->season;
	b->night = c->night;
	b->wp_fresh = c->wp_fresh;
	b->wp_pos = c->wp_fresh && in->wp > 0;
	b->wp_on = c->wp_on;
	b->wp_running = c->wp_running;
	b->wp_gt_trans = in->wp > cfg->wp_bat_max_transition;
	b->fc_share_lt_share = in->wp * cfg->wp_bat_share_bad / 100.0 < base_share;
	b->fc_fresh = cfg->forecast && in->fc_target >= 0 && in->fc_ts > 0 && in->t - in->fc_ts < FC_MAX_AGE;
	b->fc_minsoc_ok = min_soc <= 100;
	b->fc_above_on = min_soc > in->fc_target + cfg->fc_hyst;
	b->fc_below_off = min_soc < in->fc_target;
	b->fc_target_bad = in->fc_target >= cfg->fc_bad_target;
	b->b1_above_min = in->have_soc_bat1 && in->soc_bat1 > cfg->bat1_soc_min;
	b->b1_ge_release = in->soc_bat1 >= cfg->bat1_soc_min + BAT1_FIRST_HYST;
}

/* S2 row of the discharge matrix: the part of the heat pump W the stacks cover */
static double wp_share(const struct bm_cfg *cfg, int wp_kind, double wp)
{
	switch (wp_kind) {
	case BM_WPK_NONE:
		return 0.0;
	case BM_WPK_CAP:
		return fmin(wp, cfg->wp_bat_max_transition);
	case BM_WPK_BAD:
		return wp * cfg->wp_bat_share_bad / 100.0;
	default:
		return wp;
	}
}

static void chain_d1(const struct bm_cfg *cfg, const struct bm_in *in, struct bm_state *st, struct bm_out *out,
					 struct chain *c)
{
	struct bm_d1_in b;
	struct bm_d1_out o;
	double min_soc = lowest_soc(in);
	d1_bits(cfg, in, out, c, &b);
	bm_d1(&b, st->fc_active, st->bat1_first, &o);
	st->fc_active = o.fc_active;
	st->bat1_first = o.bat1_first;
	out->fc_active = o.fc_active;
	out->fc_target = o.fc_ok ? in->fc_target : -1;
	out->fc_min_soc = min_soc;
	out->fc_bad = o.fc_bad;
	out->bat1_first = o.b1_first;
	c->season_wp = o.season_wp;
	c->winter = o.winter;
	c->b1_first = o.b1_first;
	c->night_floor = o.night_floor;
	c->src = o.src_reason;
	c->wp_kind = o.wp_kind;
	c->wp_fc_capped = o.wp_fc_capped;
	c->wp_cap_armed = o.wp_cap_armed;
	snprintf(out->wp_why, sizeof(out->wp_why), "%s", BM_WP_NAME[o.wp_reason]);
	snprintf(out->src_why, sizeof(out->src_why), "%s", BM_SRC_NAME[o.src_reason]);
	if (c->wp_fresh && in->wp > 0)
		c->wp_eff = in->wp - wp_share(cfg, c->wp_kind, in->wp);
	c->z2 = in->pcc + c->wp_eff;
}

/* S0: the BMS SoC, per phase ceilings: charger (bm_charge_cap), DISCHARGE_MAX_PHASE, the BMS's own CCL / DCL (0.35-c) */
static void chain_gates(const struct bm_cfg *cfg, const struct bm_in *in, struct bm_state *st, struct bm_out *out)
{
	for (int b = 0; b < NBANK; b++) {
		st->soc_ok[b] = in->bms_ok[b];
		if (st->soc_ok[b])
			st->soc[b] = in->soc[b];
		for (int i = 0; i < BM_BANKS[b].nph; i++) {
			int p = BM_BANKS[b].ph[i];
			double ch = bm_charge_cap(cfg, in->volt_ok[b], in->volt[b]), d = cfg->discharge_max_phase;
			out->ccl_bind[p] = out->dcl_bind[p] = 0;
			if (in->lim_ok[b]) {
				double bc = bm_bms_cap_phase(cfg, b, in->volt_ok[b], in->volt[b], in->ccl[b], 1);
				double bd = bm_bms_cap_phase(cfg, b, in->volt_ok[b], in->volt[b], in->dcl[b], 0);
				out->ccl_bind[p] = bc < fmin(ch, cfg->charger_cap_phase);
				out->dcl_bind[p] = bd < d;
				ch = fmin(ch, bc);
				d = fmin(d, bd);
			}
			out->chg_cap[p] = ch;
			out->dis_cap[p] = d;
		}
	}
}

/* the per-bank predicates of bm_d2 (tests/cbmc_harness.c proves the constraints check_matrix assumes) */
static void d2_bank_bits(const struct bm_cfg *cfg, int soc_ok, double soc, int power_ok, double power,
						 struct bm_d2_bank *x)
{
	x->bms_ok = soc_ok;
	x->soc_lt_min = soc < cfg->soc_min;
	x->soc_ge_release = soc >= cfg->soc_min_release;
	x->soc_lt_force = soc < cfg->soc_force;
	x->soc_ge_force_release = soc >= cfg->soc_force_release;
	x->soc_lt_100 = soc < 100.0;
	x->charging = power_ok && power > CHARGING_TH;
}

/* S1: charge or discharge, then per phase the first matching state */
static void chain_mode(const struct bm_cfg *cfg, const struct bm_in *in, struct bm_state *st, struct bm_out *out,
					   struct chain *c)
{
	int *sp = out->sp;
	char (*why)[WHY_LEN] = out->why;
	/* a Sofar Bat1 discharge is no surplus; its charging counts with BAT1_CHARGE_FACTOR. The own discharge is
	   subtracted (0.20, found by tests/test_logic.c): else the 936 W night floor, partly going into the Sofar
	   battery, counted as PV surplus and the Multis switched to charging at night / one stack charged the other */
	c->bat1_eff = in->bat1 < 0 ? in->bat1 : in->bat1 * BAT1_CHARGE_FACTOR;
	/* charging on the Z2 point too (0.30-c, user 2026-10-09: Z2 power costs 30 ct): in winter the PV goes into the
	   stacks and the heat pump takes the cheap Z1 grid (in the transition its part above 1900 W, as the discharge).
	   While the Sofar can, it covers the heat pump from Bat1 (own PCC regulation at Z1): Bat1 -> WP, PV -> stacks */
	c->chg_ref = in->have_pcc ? c->z2 : 0.0;
	c->surplus = c->chg_ref + c->bat1_eff + c->own_charge - c->own_discharge;
	c->block = ladesperre(cfg, in, st, out, c->stale, c->multis, c->surplus, &c->cap_active);
	struct bm_d2_in d;
	struct bm_d2_out o;
	memset(&d, 0, sizeof(d));
	d.stale = c->stale;
	d.block = c->block;
	d.surplus_gt_on = c->surplus > PCC_SURPLUS_TH;
	d.surplus_gt_hold = c->surplus > SOYO_HOLD_TH;
	for (int b = 0; b < NBANK; b++)
		d2_bank_bits(cfg, st->soc_ok[b], st->soc[b], in->power_ok[b], in->power[b], &d.bank[b]);
	bm_d2(&d, st->prot, st->force, st->soyo_chg_prev, &o);
	memcpy(st->prot, o.prot, sizeof(st->prot));
	memcpy(st->force, o.force, sizeof(st->force));
	c->charge_mode = o.charge_mode;
	for (int b = 0; b < NBANK; b++)
		for (int i = 0; i < BM_BANKS[b].nph; i++) {
			int p = BM_BANKS[b].ph[i];
			const struct bm_mode_rule *r = &BM_MODE_RULE[o.mode[b]];
			const char *name = BM_MODE_NAME[o.mode[b]];
			sp[p] = r->action == BM_A_FORCE ? (int)(r->capped ? fmin(cfg->force_charge_w, out->chg_cap[p])
															 : cfg->force_charge_w) : 0;
			if (r->action == BM_A_CHARGE) {
				c->charge[p] = 1;
				c->n_chg++;
			} else if (r->action == BM_A_DISCHARGE) {
				c->discharge[p] = 1;
				c->n_dis++;
			}
			switch (r->why) {
			case BM_W_NAME:
				snprintf(why[p], WHY_LEN, "%s", name);
				break;
			case BM_W_SOC:
				snprintf(why[p], WHY_LEN, "%s(%.1f%%)", name, st->soc[b]);
				break;
			case BM_W_PEAK_H:
				snprintf(why[p], WHY_LEN, "%s(%dh)", name, out->ls.peak_h);
				break;
			case BM_W_POWER:
				snprintf(why[p], WHY_LEN, "%s(%.0fW)", name, in->power[b]);
				break;
			case BM_W_CHAIN:
				snprintf(why[p], WHY_LEN, "%s WP:%s SRC:%s", name, out->wp_why, out->src_why);
				break;
			}
		}
}

/* SoC balancing lead (bm_dlead) */
static void chain_lead(const struct bm_cfg *cfg, struct bm_state *st, struct bm_out *out)
{
	struct bm_dl_in d;
	double diff = st->soc[0] - st->soc[1];
	d.both_ok = st->soc_ok[0] && st->soc_ok[1];
	d.diff_gt_on = fabs(diff) > cfg->soc_balance_on;
	d.diff_lt_off = fabs(diff) < cfg->soc_balance_off;
	d.first_ahead = diff > 0;
	st->lead = bm_dlead(&d, st->lead, &out->lead_event);
	if (out->lead_event)
		out->lead_diff = fabs(diff);
}

/* S4 amount, the one formula: w = min(PROPORTIONAL ? max(deficit, 0) : idle_w, W_MAX, WP_CAP while armed).
   Never below 0: a discharge never turns into charging (Sofar TOU charge) */
static int dis_amount(int prop, int deficit, int idle_w, int w_max, int wp_cap_armed, int wp_cap, int *capped)
{
	int w = prop ? (deficit > 0 ? deficit : 0) : idle_w;
	if (w > w_max)
		w = w_max;
	*capped = wp_cap_armed && w > wp_cap;
	return *capped ? wp_cap : w;
}

/* S4 + S5 discharge: deficit = what the Multis already give (minus what of it goes into the Sofar battery) + the
   import still left (0.18); the own charging (force charge of the other stack) is no house load (0.20) */
static void chain_discharge(const struct bm_cfg *cfg, const struct bm_in *in, struct bm_state *st, struct bm_out *out,
							const struct chain *c)
{
	const struct bm_src_rule *r = &BM_SRC_RULE[c->src];
	char rule[64];
	int house = watt(c->z2 + c->own_charge), bat1 = watt(in->bat1), dcap[NPH];
	/* b1_signed, the night floor (0.25, user 2026-10-08: PCC near 0): the Sofar Bat1 counts signed. Its discharge is
	   house load the stacks take over, its charge is our own overshoot. pcc + bat1 is the load behind the Sofar however
	   the Sofar splits it, so both regulators do not fight; the Sofar battery idles, the PCC stays near 0 */
	int b1_def = r->b1_signed ? bat1 : (bat1 > 0 ? bat1 : 0);
	/* trickle (0.27, user 2026-10-08): at night aim at pcc + bat1 = +SOFAR_TRICKLE, so the Sofar battery charges a
	   few W steadily instead of swinging between charge and discharge around 0 */
	int target = watt(cfg->soyo_target + (r->trickle ? cfg->sofar_trickle : 0.0));
	int deficit = watt(c->own_discharge) - b1_def - (int)((long)KP_PM * (house - target) / 1000);
	struct bm_d3_in d = {1, house < PCC_IMPORT_TH, deficit > SOYO_HOLD_TH, r->prop_hold};
	int prop = bm_d3(&d, st->soyo_prop_prev, &st->soyo_prop_prev), wp_cap;
	int w = dis_amount(prop, deficit, r->idle_w, (int)cfg->w_max, c->wp_cap_armed, (int)cfg->wp_cap, &wp_cap);
	snprintf(rule, sizeof(rule), "%s WP:%s SRC:%s", prop ? "PROP" : "IDLE", out->wp_why, out->src_why);
	dis_caps(out, dcap);
	alloc_discharge(st->lead, w, c->discharge, dcap, rule, out->sp, out->why);
	if (wp_cap)
		for (int p = 0; p < NPH; p++)
			if (c->discharge[p])
				strncat(out->why[p], " WPCAP", WHY_LEN - 1 - strlen(out->why[p]));
}

/* S4 + S5 charge: own charge + KP x (Z2 surplus incl. the Sofar charge share), at most the phase ceilings, never
   below 0 (0.44-c, found by make check: when the PV collapses while charge mode holds, KP 1.01 overshot into a
   discharge of up to 1 % of the own charge, e.g. PCC -4175 W, own charge 4200 W -> -16 W on a CHARGE phase) */
static void chain_charge(const struct bm_cfg *cfg, const struct bm_state *st, struct bm_out *out, const struct chain *c)
{
	int cap[NPH], cap_sum = 0;
	chg_caps(out, cap);
	for (int p = 0; p < NPH; p++)
		if (c->charge[p])
			cap_sum += cap[p];
	/* D4 cap (0.49-c): aim at PCC = +EXPORT_CAP, so only the export above the cap goes into the stacks */
	int target = watt(cfg->soyo_target + (c->cap_active ? cfg->export_cap : 0.0));
	int want = watt(c->own_charge) + (int)((long)KP_PM * (watt(c->chg_ref + c->bat1_eff) - target) / 1000);
	if (want < 0)
		want = 0;
	alloc_charge(cfg, st->lead, want < cap_sum ? want : cap_sum, c->charge, c->n_chg, cap, out->sp);
	for (int p = 0; p < NPH; p++)                       /* CHARGE_PRIORITY order; with a lead the other stack first */
		if (c->charge[p]) {
			strncat(out->why[p], st->lead >= 0 ? " BAL" : " EQ", WHY_LEN - 1 - strlen(out->why[p]));
			if (c->cap_active)
				strncat(out->why[p], " CAP", WHY_LEN - 1 - strlen(out->why[p]));
		}
}

/* PI prototype (shadow unless BATMONITOR_PI=1). y = PCC + Sofar Bat1: Bat1 charging counts with BAT1_CHARGE_FACTOR
   (as the soyo surplus); a Bat1 discharge counts while the Multis charge (the Sofar must not feed them) and with the
   night floor (S3); by day while they discharge it is ignored (the Sofar battery covers the house first) */
static void chain_pi(const struct bm_cfg *cfg, const struct bm_in *in, struct bm_state *st, struct bm_out *out,
					 const struct chain *c)
{
	int *sp = out->sp;
	int pi_ok[NPH] = {0}, pi_chg[NPH] = {0}, pi_dis[NPH] = {0}, n_pichg = 0, n_pidis = 0;
	for (int b = 0; b < NBANK; b++)                    /* own gates (the soyo ones depend on its fixed thresholds) */
		for (int i = 0; i < BM_BANKS[b].nph; i++) {
			int p = BM_BANKS[b].ph[i];
			if (!st->soc_ok[b] || st->force[b] || c->stale)
				continue;
			pi_ok[p] = 1;
			if (!c->block && st->soc[b] < 100.0) {
				pi_chg[p] = 1;
				n_pichg++;
			}
			if (!st->prot[b]) {
				pi_dis[p] = 1;
				n_pidis++;
			}
		}
	double applied = 0;
	for (int p = 0; p < NPH; p++)
		if (pi_ok[p])
			applied += st->setpoints[p];
	double b1 = in->bat1 > 0 ? in->bat1 * BAT1_CHARGE_FACTOR : (applied > 0 || c->night_floor) ? in->bat1 : 0.0;
	/* both sides on the Z2 point (discharge 0.19, charge 0.30-c), as soyo */
	double y = (in->have_pcc ? c->z2 : 0.0) + b1, e = y - PI_TARGET;
	if (fabs(e) < PI_DEADBAND)
		e = 0;
	double dt = st->pi_t_prev > 0 ? fmin(fmax(in->now - st->pi_t_prev, 1.0), 30.0) : CYCLE_SECONDS;
	double u = applied + (st->pi_t_prev > 0 ? PI_KP * (e - st->pi_e_prev) : 0) + PI_KI * dt * e;
	double dis_cap = (!c->wp_fresh && c->winter && c->wp_running) ? cfg->wp_cap : cfg->w_max;
	double u_max = 0, u_min = n_pidis ? -dis_cap : 0;
	for (int p = 0; p < NPH; p++)
		if (pi_chg[p])
			u_max += out->chg_cap[p];
	/* crossing from discharge into charging: never more than the surplus at the Z2 point */
	if (applied <= 0 && u > 0)
		u = fmax(0.0, fmin(u, (in->have_pcc ? c->z2 : 0.0) + b1 - PI_TARGET));
	u = fmax(u_min, fmin(u_max, u));
	st->pi_e_prev = e;
	st->pi_t_prev = in->now;
	int psp[NPH] = {0};
	char pwhy[NPH][WHY_LEN];
	for (int p = 0; p < NPH; p++)
		snprintf(pwhy[p], WHY_LEN, "%.95s", out->why[p]);
	for (int p = 0; p < NPH; p++)
		if (pi_ok[p])
			snprintf(pwhy[p], WHY_LEN, "PI(%+.0f)", u);
	if (u > 0) {
		int cap[NPH];
		chg_caps(out, cap);
		alloc_charge(cfg, st->lead, (int)u, pi_chg, n_pichg, cap, psp);
	} else if (u < 0) {
		char rule[32];
		snprintf(rule, sizeof(rule), "PI(%+.0f)", u);
		int dcap[NPH];
		dis_caps(out, dcap);
		alloc_discharge(st->lead, (int)-u, pi_dis, dcap, rule, psp, pwhy);
	}
	for (int p = 0; p < NPH; p++)
		if (!pi_ok[p])
			psp[p] = sp[p];          /* force charge, BMS missing, stale: as soyo */
	out->pi.y = y;
	out->pi.e = e;
	out->pi.applied = applied;
	out->pi.u = u;
	out->pi.u_min = u_min;
	out->pi.u_max = u_max;
	memcpy(out->pi.sp, psp, sizeof(psp));
	if (cfg->pi_armed) {
		memcpy(sp, psp, sizeof(psp));
		memcpy(out->why, pwhy, sizeof(pwhy));
	}
}

/* one cycle: the rule chain above */
void bm_step(const struct bm_cfg *cfg, const struct bm_in *in, struct bm_state *st, struct bm_out *out)
{
	struct chain c;
	memset(out, 0, sizeof(*out));
	memset(&c, 0, sizeof(c));
	chain_inputs(in, out, &c);
	chain_d1(cfg, in, st, out, &c);
	chain_gates(cfg, in, st, out);
	chain_mode(cfg, in, st, out, &c);
	chain_lead(cfg, st, out);
	if (c.n_dis)
		chain_discharge(cfg, in, st, out, &c);
	if (c.n_chg)
		chain_charge(cfg, st, out, &c);
	for (int p = 0; p < NPH; p++)                       /* the BMS limit cut this phase's setpoint */
		if ((out->sp[p] > 0 && out->ccl_bind[p] && out->sp[p] >= (int)out->chg_cap[p] - 1) ||
			(out->sp[p] < 0 && out->dcl_bind[p] && -out->sp[p] >= (int)out->dis_cap[p] - 1))
			strncat(out->why[p], out->sp[p] > 0 ? " CCL" : " DCL", WHY_LEN - 1 - strlen(out->why[p]));
	st->soyo_chg_prev = c.n_chg > 0;
	if (!c.n_dis) {
		struct bm_d3_in d = {0, 0, 0, 0};
		bm_d3(&d, st->soyo_prop_prev, &st->soyo_prop_prev);
	}
	int pcc_over = in->have_pcc && in->now - in->inv_time < DO4_PCC_MAX_AGE && in->pcc > PCC_PEAK_TH;
	if (!pcc_over)
		st->do4_over_since = 0;
	else if (st->do4_over_since <= 0)
		st->do4_over_since = in->now;
	double feed = 0;                                    /* AC-out: + = loads, - = PV feeding back */
	for (int p = 0; p < NPH; p++)
		if (in->ac_out_ok[p] && -in->ac_out[p] > feed)
			feed = -in->ac_out[p];
	out->acout_feed_w = feed;
	/* off-grid only (user 2026-10-10): with the grid there everything passes the 50 A transfer relay (bypass) */
	int acout_now = !in->grid_ok && cfg->acout_feed_max_w > 0 && feed > cfg->acout_feed_max_w;
	if (!acout_now)
		st->acout_over_since = 0;
	else if (st->acout_over_since <= 0)
		st->acout_over_since = in->now;
	struct bm_d5_in d5 = {pcc_over, pcc_over && in->now - st->do4_over_since >= cfg->do4_grace_s,
						  pcc_over && in->pcc > cfg->do4_hard_w,
						  st->do4_last <= 0 || in->now - st->do4_last >= DO4_LOCKOUT,
						  acout_now && in->now - st->acout_over_since >= cfg->acout_feed_s};
	out->do4_pulse = bm_d5(&d5);
	out->do4_acout = out->do4_pulse && d5.acout_over;
	if (out->do4_pulse)
		st->do4_last = in->now;
	chain_pi(cfg, in, st, out, &c);
	out->surplus = c.surplus;
	out->wp_eff = c.wp_eff;
	memcpy(st->setpoints, out->sp, sizeof(st->setpoints));
}

/* ---- full_at forecast (0.28-c) ------------------------------------------------------------------ */

/* weather kt at t: the OWM slot within FC_SLOT_REACH, else the monthly kt (weather = 0) */
static double fc_kt(const struct bm_fc_in *in, time_t t, int *weather)
{
	int best = -1;
	long dbest = FC_SLOT_REACH + 1;
	for (int i = 0; i < in->n_slots; i++) {
		long d = labs((long)(t - in->slot_t[i]));
		if (d < dbest) {
			dbest = d;
			best = i;
		}
	}
	if (best >= 0 && in->corr > 0) {
		*weather = 1;
		return in->slot_kt[best] * in->corr;
	}
	struct tm loc;
	localtime_r(&t, &loc);
	*weather = 0;
	return KT_MONTH[loc.tm_mon + 1];
}

/* modelled PV of both inverters (W) at t, the anchor fading from now on */
static double fc_pv(const struct bm_fc_in *in, time_t t, double anchor, int *weather)
{
	double clear = calc_arrays_kt(ARRAYS, 2, t, 1.0) + calc_arrays_kt(ARRAYS_EAST, 2, t, 1.0);
	double f = 1.0 + (anchor - 1.0) * exp(-(double)(t - in->t) / FC_ANCHOR_TAU);
	return clear * fc_kt(in, t, weather) * f;
}

/* Steps from now to sunset in FC_STEP: the stacks together get their measured charge power plus the change of the
   expected PV against now (x FC_CHARGE_EFF; house load and the Sofar battery's share taken as they are now). Each
   step that total is split like alloc_charge: CHARGE_PRIORITY order, the lead stack last, each up to the real charger
   ceiling (bm_charge_cap x FC_CHARGE_EFF per unit, at most CHARGER_DC_W: the MultiPlus deliver ~3.6 kW DC at the BMS,
   not 70 A x U, measured 2026-10-09). The lead follows the simulated SoC with SOC_BALANCE_ON/OFF like update_lead
   (0.32-c: a paused stack catches up instead of waiting until the other one is full). A full stack's power moves to
   the other one. Only while the sun is up. */
void bm_full_forecast(const struct bm_cfg *cfg, const struct bm_fc_in *in, struct bm_fc_out *out)
{
	memset(out, 0, sizeof(*out));
	for (int b = 0; b < NBANK; b++)
		out->soc_sunset[b] = -1;
	out->anchor = 1.0;
	int w0;
	out->kt_now = fc_kt(in, in->t, &w0);
	out->weather = w0;
	double clear_sofar = calc_arrays_kt(ARRAYS, 2, in->t, 1.0) * out->kt_now;
	if (clear_sofar <= 0)
		return;                                     /* night: no forecast */
	if (in->have_pv && clear_sofar >= FC_ANCHOR_MIN_W)
		out->anchor = fmax(0.05, fmin(3.0, in->pv_sofar / clear_sofar));
	double pv0 = fc_pv(in, in->t, out->anchor, &w0);
	out->pv_now_w = pv0;
	double soc[NBANK], pb[NBANK] = {0}, cap[NBANK];
	int active[NBANK], any = 0;
	for (int b = 0; b < NBANK; b++) {
		active[b] = in->soc_ok[b] && in->has_avg[b];
		soc[b] = in->soc[b];
		cap[b] = fmin(fmin(cfg->charger_cap_phase, bm_charge_cap(cfg, in->volt_ok[b], in->volt[b])) * FC_CHARGE_EFF,
					  cfg->charger_dc_w) * BM_BANKS[b].nph;
		if (!active[b])
			continue;
		any = 1;
		pb[b] = fmin(fmax(0.0, in->avg[b]), cap[b]);
		if (soc[b] >= 100)
			out->full_at[b] = in->t;
	}
	if (!any)
		return;
	double p0 = 0;                                  /* what the stacks take now, at most their chargers */
	for (int b = 0; b < NBANK; b++)
		if (active[b] && soc[b] < 100)
			p0 += pb[b];
	int lead = in->lead;
	for (time_t t = in->t + FC_STEP; t < in->t + 86400; t += FC_STEP) {
		int w;
		double pv = fc_pv(in, t - FC_STEP / 2, out->anchor, &w);
		out->rest_kwh += pv * FC_STEP / 3600000.0;
		if (active[0] && active[1]) {                  /* update_lead on the simulated SoC */
			double d = soc[0] - soc[1];
			if (lead < 0 && fabs(d) > cfg->soc_balance_on)
				lead = d > 0 ? 0 : 1;
			else if (lead >= 0 && (lead == 0 ? d : -d) < cfg->soc_balance_off)
				lead = -1;          /* no longer ahead: a 5 min step can jump over the +-1 % window of update_lead */
		}
		int order[NBANK], k = charge_order(lead, order);
		double rest = fmax(0.0, p0 + FC_CHARGE_EFF * (pv - pv0));
		for (int i = 0; i < k; i++) {                  /* alloc_charge: priority order up to the charger */
			int b = order[i];
			pb[b] = 0;
			if (!active[b] || soc[b] >= 100)
				continue;
			pb[b] = fmin(rest, cap[b]);
			rest -= pb[b];
		}
		for (int b = 0; b < NBANK; b++) {
			if (!active[b] || soc[b] >= 100)
				continue;
			soc[b] += pb[b] * FC_STEP / 3600.0 / BM_BANKS[b].capacity_wh * 100.0;
			if (soc[b] >= 100) {
				soc[b] = 100;                       /* its power goes to the other stack next step */
				out->full_at[b] = t;
			}
		}
		double elev, az;
		bm_sun_pos(t, &elev, &az);
		if (elev <= 0)
			break;                                  /* sunset */
	}
	for (int b = 0; b < NBANK; b++)
		if (active[b])
			out->soc_sunset[b] = soc[b];
}
