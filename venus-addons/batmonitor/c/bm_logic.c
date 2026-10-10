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
#define KP 1.01
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

/* bank whose SoC is more than SOC_BALANCE_ON above the other one; cleared below SOC_BALANCE_OFF */
static void update_lead(const struct bm_cfg *cfg, struct bm_state *st, struct bm_out *out)
{
	if (!st->soc_ok[0] || !st->soc_ok[1]) {
		st->lead = -1;
		return;
	}
	double diff = st->soc[0] - st->soc[1];
	if (st->lead < 0 && fabs(diff) > cfg->soc_balance_on) {
		st->lead = diff > 0 ? 0 : 1;
		out->lead_event = 1;
		out->lead_diff = fabs(diff);
	} else if (st->lead >= 0 && fabs(diff) < cfg->soc_balance_off) {
		st->lead = -1;
		out->lead_event = -1;
		out->lead_diff = fabs(diff);
	}
}

/* fox2db_logic.h step(): charge block until the PCC peak window, summer only. 1 = do not charge from PV.
   multis = signed AC-in sum of the Multis (+ = they take from the AC side) */
static int ladesperre(const struct bm_cfg *cfg, const struct bm_in *in, struct bm_state *st, struct bm_out *out,
					  int stale, double multis)
{
	time_t now = in->t;
	struct tm loc, mid;
	localtime_r(&now, &loc);
	int month = loc.tm_mon + 1;
	if (loc.tm_yday != st->ls_yday) {      /* midnight reset */
		st->ls_yday = loc.tm_yday;
		st->peak_today = st->ls_latched = st->badweather_today = 0;
	}
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
	double best_w = 0;
	int peak_h = -1, win_end = -1;
	for (int h = 5; h <= 20; h++) {
		double w = bm_dc_now(midnight + (time_t)h * 3600, month);
		if (w > best_w) {
			best_w = w;
			peak_h = h;
		}
		if (w > PCC_PEAK_TH)
			win_end = h;
	}
	time_t noon = solar_noon_utc(now);
	if (in->have_pcc && !stale && in->pcc > PCC_PEAK_TH)
		st->peak_today = 1;
	/* measured PV minus house load: export + Sofar Bat1 + what the Multis take (ESP: + EBox charger) */
	double ratio = -1.0, ratio_now = -1.0;
	if (dc > DC_RATIO_MIN && !stale && in->have_pcc) {
		ratio = (dc - (in->pcc_avg5 + multis + in->bat1_avg5)) / dc;
		ratio_now = (dc - (in->pcc + multis + in->bat1)) / dc;
	}
	int block = 0;
	if (cfg->ladesperre) {
		int in_window = month >= LADESPERRE_FROM && month <= LADESPERRE_TO && best_w > PCC_PEAK_TH && win_end >= 0
						&& !st->peak_today && loc.tm_hour <= peak_h && now < noon;
		if (in_window && ratio_now >= LADESPERRE_NOW_RATIO)
			st->badweather_today = 1;
		if (!in_window)
			st->ls_latched = 0;
		else if (ratio >= 0.0) {
			if (!st->ls_latched && ratio <= LADESPERRE_RATIO)
				st->ls_latched = 1;
			else if (st->ls_latched && ratio >= LADESPERRE_RATIO + LADESPERRE_HYST)
				st->ls_latched = 0;
		}
		block = in_window && st->ls_latched && !st->badweather_today;
	}
	out->ls.active = block;
	out->ls.dc = dc;
	out->ls.ratio = ratio;
	out->ls.ratio_now = ratio_now;
	out->ls.peak_h = best_w > PCC_PEAK_TH ? peak_h : -1;
	out->ls.win_end_h = win_end;
	out->ls.noon_h = (double)(noon - midnight) / 3600.0;
	return block;
}

/* discharge w (W, > 0) over the phases in dis[], at most DISCHARGE_MAX_PHASE each, equal power per bank (0.26); SoC balancing: the stack more
   than SOC_BALANCE_ON ahead delivers alone, what its phases cannot give goes to the other phases (0.22) */
static void alloc_discharge(int lead, int w, const int *dis, const double *dcap, const char *rule, int *sp,
							char why[][WHY_LEN])
{
	int give[NPH] = {0}, n_give = 0, n_dis = 0;
	for (int p = 0; p < NPH; p++)
		n_dis += dis[p];
	if (lead >= 0)
		for (int i = 0; i < BM_BANKS[lead].nph; i++)
			if (dis[BM_BANKS[lead].ph[i]]) {
				give[BM_BANKS[lead].ph[i]] = 1;
				n_give++;
			}
	if (!n_give) {
		/* no lead (0.26, user 2026-10-08): every bank delivers the same power, so the two 300 Ah stacks drain alike:
		   Stack1 alone on L1 gives as much as Stack2 on L2+L3 (L1 50 %, L2/L3 25 % each). What a phase cannot give
		   above DISCHARGE_MAX_PHASE goes to the other discharging phases alike */
		double want[NPH] = {0};
		int n_banks = 0;
		for (int b = 0; b < NBANK; b++)
			for (int i = 0; i < BM_BANKS[b].nph; i++)
				if (dis[BM_BANKS[b].ph[i]]) {
					n_banks++;
					break;
				}
		for (int b = 0; b < NBANK; b++) {
			int nb = 0;
			for (int i = 0; i < BM_BANKS[b].nph; i++)
				nb += dis[BM_BANKS[b].ph[i]];
			for (int i = 0; i < BM_BANKS[b].nph; i++)
				if (dis[BM_BANKS[b].ph[i]])
					want[BM_BANKS[b].ph[i]] = (double)w / n_banks / nb;
		}
		for (int k = 0; k < NPH; k++) {                    /* spill above dmax, at most NPH rounds */
			double over = 0;
			int n_free = 0;
			for (int p = 0; p < NPH; p++)
				if (dis[p]) {
					if (want[p] > dcap[p]) {
						over += want[p] - dcap[p];
						want[p] = dcap[p];
					} else if (want[p] < dcap[p])
						n_free++;
				}
			if (over <= 0 || !n_free)
				break;
			for (int p = 0; p < NPH; p++)
				if (dis[p] && want[p] < dcap[p])
					want[p] += over / n_free;
		}
		for (int p = 0; p < NPH; p++)
			if (dis[p]) {
				sp[p] = -(int)want[p];
				snprintf(why[p], WHY_LEN, "%s EQ", rule);
			}
		return;
	}
	double give_cap = 1e9, rest_cap = 1e9;              /* phases of one bank share one cap */
	for (int p = 0; p < NPH; p++)
		if (dis[p]) {
			if (give[p])
				give_cap = fmin(give_cap, dcap[p]);
			else
				rest_cap = fmin(rest_cap, dcap[p]);
		}
	int share = n_give ? (int)((double)w / n_give) : 0, spill = 0;
	if (share > give_cap) {
		spill = (share - (int)give_cap) * n_give;
		share = (int)give_cap;
	}
	int n_rest = n_dis - n_give, extra = n_rest ? spill / n_rest : 0;
	if (extra > rest_cap)
		extra = (int)rest_cap;
	for (int p = 0; p < NPH; p++) {
		if (!dis[p])
			continue;
		sp[p] = give[p] ? -share : -extra;
		snprintf(why[p], WHY_LEN, "%s%s", rule, give[p] ? " BAL" : extra ? " SPILL" : " WAIT");
	}
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

/* charge rest (W) over the phases in chg[]: the banks in CHARGE_PRIORITY order up to their real charger capacity,
   the stack behind first; what is left up to CHARGE_MAX_PHASE on all phases alike. cap[] (0.29-c): the real ceiling
   per phase (charger 70 A x BMS voltage); with a measured voltage nothing spills above it */
static void alloc_charge(const struct bm_cfg *cfg, int lead, double rest, const int *chg, int n_chg, const double *cap,
						 int *sp)
{
	int order[NBANK], k = charge_order(lead, order);
	for (int i = 0; i < k; i++) {
		int b = order[i], ph[NPH], n = 0;
		for (int j = 0; j < BM_BANKS[b].nph; j++)
			if (chg[BM_BANKS[b].ph[j]])
				ph[n++] = BM_BANKS[b].ph[j];
		if (!n)
			continue;
		double bank_cap = 0;
		for (int j = 0; j < n; j++)
			bank_cap += fmin(cfg->charger_cap_phase, cap[ph[j]]);
		double share = fmin(rest, bank_cap);
		for (int j = 0; j < n; j++)
			sp[ph[j]] = (int)(share / n);
		rest -= share;
	}
	if (rest > 0)
		for (int p = 0; p < NPH; p++)
			if (chg[p])
				sp[p] += (int)fmax(0.0, fmin(rest / n_chg, fmin(cfg->charge_max_phase, cap[p]) - sp[p]));
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
   PI         shadow prototype (not armed), reads the same stages
   A stage reads the results of the stages before it and changes none of them.
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
	double bat1_eff, chg_ref, surplus;
	int block, charge_mode;
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

/* forecast (0.24, readers/forecast.py): while both stacks are above the SoC that the next day's PV surplus will
   refill, they serve everything, the heat pump included, in any season (sell less, use more); below the target the
   season rules apply again, back on above target + FC_HYST. Bad forecast (0.37-c, user 2026-10-10: rain day): the next
   day's PV will not refill the stacks (target_soc >= FC_BAD_TARGET, i.e. free_kwh 0) -> WP share capped in S2 */
static void chain_forecast(const struct bm_cfg *cfg, const struct bm_in *in, struct bm_state *st, struct bm_out *out,
						   struct chain *c)
{
	double min_soc = 101;
	for (int b = 0; b < NBANK; b++)
		if (in->bms_ok[b] && in->soc[b] < min_soc)
			min_soc = in->soc[b];
	int fc_fresh = cfg->forecast && in->fc_target >= 0 && in->fc_ts > 0 && in->t - in->fc_ts < FC_MAX_AGE;
	int fc_ok = fc_fresh && min_soc <= 100;
	if (!fc_ok)
		st->fc_active = 0;
	else if (!st->fc_active && min_soc > in->fc_target + cfg->fc_hyst)
		st->fc_active = 1;
	else if (st->fc_active && min_soc < in->fc_target)
		st->fc_active = 0;
	out->fc_active = st->fc_active;
	out->fc_target = fc_ok ? in->fc_target : -1;
	out->fc_min_soc = min_soc;
	out->fc_bad = fc_fresh && !st->fc_active && in->fc_target >= cfg->fc_bad_target;
	c->season_wp = st->fc_active ? BM_SUMMER : out->season;
	c->winter = c->season_wp != BM_SUMMER;      /* transition counts as winter for the WP_CAP fallback */
}

/* S0: per bank SoC protection (stop < SOC_MIN until SOC_MIN_RELEASE) and force charge (< SOC_FORCE until
   SOC_FORCE_RELEASE); per phase ceilings: charger (bm_charge_cap), DISCHARGE_MAX_PHASE, the BMS's own CCL / DCL (0.35-c) */
static void chain_gates(const struct bm_cfg *cfg, const struct bm_in *in, struct bm_state *st, struct bm_out *out)
{
	for (int b = 0; b < NBANK; b++) {
		st->soc_ok[b] = in->bms_ok[b];
		if (st->soc_ok[b]) {
			st->soc[b] = in->soc[b];
			if (st->soc[b] < cfg->soc_min)
				st->prot[b] = 1;
			else if (st->soc[b] >= cfg->soc_min_release)
				st->prot[b] = 0;
			if (st->soc[b] < cfg->soc_force)
				st->force[b] = 1;
			else if (st->soc[b] >= cfg->soc_force_release)
				st->force[b] = 0;
		}
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

/* S2: the heat pump power the stacks must NOT cover (Z2 point = Z1 PCC + that, 0.19): all of it in winter, the part
   above WP_BAT_MAX_TRANSITION in the transition (0.22), none in summer; with a bad forecast the stacks cover at most
   WP_BAT_SHARE_BAD % (0.37-c). The smallest cap wins */
static void chain_scope(const struct bm_cfg *cfg, const struct bm_in *in, struct bm_out *out, struct chain *c)
{
	if (c->wp_fresh && in->wp > 0) {
		double share = c->season_wp == BM_WINTER ? 0.0
					   : c->season_wp == BM_TRANSITION ? fmin(in->wp, cfg->wp_bat_max_transition) : in->wp;
		double fc_share = in->wp * cfg->wp_bat_share_bad / 100.0;
		if (out->fc_bad && fc_share < share) {
			share = fc_share;
			c->wp_fc_capped = 1;
		}
		c->wp_eff = in->wp - share;
	}
	c->z2 = in->pcc + c->wp_eff;
	snprintf(out->wp_why, sizeof(out->wp_why), "%s", !c->wp_fresh ? "NA" : !c->wp_on ? "OFF" : out->fc_active ? "FC"
			 : c->wp_fc_capped ? "FCBAD" : c->season_wp == BM_WINTER ? "Z2"
			 : c->season_wp == BM_TRANSITION && in->wp > cfg->wp_bat_max_transition ? "T1900" : "ALL");
}

/* S3: who delivers first at night. Sofar first (0.38-c, user 2026-10-10): while the heat pump runs and the Sofar Bat1
   is above BAT1_SOC_MIN it covers the night load by its own PCC regulation; the stacks only take the import it leaves
   (its 2.5 kW limit) and take over once it is down at BAT1_SOC_MIN (its DOD). Otherwise the night floor (0.25): the
   stacks take the night load over from the Sofar, not with the heat pump running in winter (the base load would flow
   into it). By day the Sofar goes first anyway (its discharge is ignored in S4) */
static void chain_source(const struct bm_cfg *cfg, const struct bm_in *in, struct bm_state *st, struct bm_out *out,
						 struct chain *c)
{
	if (!in->have_soc_bat1 || in->soc_bat1 <= cfg->bat1_soc_min)
		st->bat1_first = 0;
	else if (!st->bat1_first && in->soc_bat1 >= cfg->bat1_soc_min + BAT1_FIRST_HYST)
		st->bat1_first = 1;
	c->b1_first = st->bat1_first && c->wp_on;
	out->bat1_first = c->b1_first;
	c->night_floor = c->night && !c->b1_first && (c->season_wp != BM_WINTER || !c->wp_on);
	snprintf(out->src_why, sizeof(out->src_why), "%s", !c->night ? "DAY" : c->b1_first ? "B1" : c->night_floor ? "STK" : "B1W");
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
	c->block = ladesperre(cfg, in, st, out, c->stale, c->multis);
	/* surplus includes the own charging: once charging, it holds down to SOYO_HOLD_TH instead of 200 W */
	c->charge_mode = c->surplus > PCC_SURPLUS_TH || (st->soyo_chg_prev && c->surplus > SOYO_HOLD_TH);
	for (int b = 0; b < NBANK; b++) {
		for (int i = 0; i < BM_BANKS[b].nph; i++) {
			int p = BM_BANKS[b].ph[i];
			sp[p] = 0;
			if (!st->soc_ok[b])
				snprintf(why[p], WHY_LEN, "BMS_MISSING");
			else if (st->force[b]) {
				sp[p] = (int)cfg->force_charge_w;
				snprintf(why[p], WHY_LEN, "FORCE(%.1f%%)", st->soc[b]);
			} else if (c->stale)
				snprintf(why[p], WHY_LEN, "STALE");
			else if (c->charge_mode && c->block)
				snprintf(why[p], WHY_LEN, "LADESPERRE(%dh)", out->ls.peak_h);
			else if (c->charge_mode) {
				if (st->soc[b] < 100.0)
					snprintf(why[p], WHY_LEN, "CHARGE WP:%s SRC:%s", out->wp_why, out->src_why);
				else
					snprintf(why[p], WHY_LEN, "FULL");
				if (st->soc[b] < 100.0) {
					c->charge[p] = 1;
					c->n_chg++;
				}
			} else if (st->prot[b])
				snprintf(why[p], WHY_LEN, "PROT(%.1f%%)", st->soc[b]);
			else if (in->power_ok[b] && in->power[b] > CHARGING_TH)
				snprintf(why[p], WHY_LEN, "CHARGING(%.0fW)", in->power[b]);
			else {
				c->discharge[p] = 1;
				c->n_dis++;
			}
		}
	}
}

/* S4 + S5 discharge: deficit = what the Multis already give (minus what of it goes into the Sofar battery) + the
   import still left (0.18); the own charging (force charge of the other stack) is no house load (0.20) */
static void chain_discharge(const struct bm_cfg *cfg, const struct bm_in *in, struct bm_state *st, struct bm_out *out,
							const struct chain *c)
{
	int w;
	char rule[64];
	double house = c->z2 + c->own_charge;
	/* night floor (0.25, user 2026-10-08: PCC near 0): the Sofar Bat1 counts signed. Its discharge is house load the
	   stacks take over, its charge is our own overshoot. pcc + bat1 is the load behind the Sofar however the Sofar
	   splits it, so both regulators do not fight; the Sofar battery idles, the PCC stays near 0 */
	double b1_def = c->night_floor ? in->bat1 : (in->bat1 > 0 ? in->bat1 : 0.0);
	/* 0.27 (user 2026-10-08): at night aim at pcc + bat1 = +SOFAR_TRICKLE, so the Sofar battery charges a few W
	   steadily instead of swinging between charge and discharge around 0 */
	double target = cfg->soyo_target + (c->night_floor ? cfg->sofar_trickle : 0.0);
	double deficit = c->own_discharge - b1_def - KP * (house - target);
	if (house < PCC_IMPORT_TH || ((st->soyo_prop_prev || c->night_floor) && deficit > SOYO_HOLD_TH)) {
		w = deficit > 0 ? (int)deficit : 0;   /* never turn a discharge into charging (Sofar TOU charge) */
		if (w > (int)cfg->w_max)
			w = (int)cfg->w_max;
		strcpy(rule, "PROP");
		st->soyo_prop_prev = 1;
	} else {
		st->soyo_prop_prev = 0;
		w = c->night_floor ? 0 : B_DAY_IDLE;
		strcpy(rule, "IDLE");
	}
	snprintf(rule + strlen(rule), sizeof(rule) - strlen(rule), " WP:%s SRC:%s", out->wp_why, out->src_why);
	int wp_cap = !c->wp_fresh && c->winter && c->wp_running && w > (int)cfg->wp_cap;
	if (wp_cap)
		w = (int)cfg->wp_cap;
	alloc_discharge(st->lead, w, c->discharge, out->dis_cap, rule, out->sp, out->why);
	if (wp_cap)
		for (int p = 0; p < NPH; p++)
			if (c->discharge[p])
				strncat(out->why[p], " WPCAP", WHY_LEN - 1 - strlen(out->why[p]));
}

/* S4 + S5 charge: own charge + KP x (Z2 surplus incl. the Sofar charge share), at most the phase ceilings */
static void chain_charge(const struct bm_cfg *cfg, const struct bm_state *st, struct bm_out *out, const struct chain *c)
{
	double cap_sum = 0;
	for (int p = 0; p < NPH; p++)
		if (c->charge[p])
			cap_sum += out->chg_cap[p];
	alloc_charge(cfg, st->lead, fmin((int)(c->own_charge + KP * (c->chg_ref + c->bat1_eff - cfg->soyo_target)), cap_sum),
				 c->charge, c->n_chg, out->chg_cap, out->sp);
	for (int p = 0; p < NPH; p++)                       /* CHARGE_PRIORITY order; with a lead the other stack first */
		if (c->charge[p])
			strncat(out->why[p], st->lead >= 0 ? " BAL" : " EQ", WHY_LEN - 1 - strlen(out->why[p]));
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
	if (u > 0)
		alloc_charge(cfg, st->lead, u, pi_chg, n_pichg, out->chg_cap, psp);
	else if (u < 0) {
		char rule[32];
		snprintf(rule, sizeof(rule), "PI(%+.0f)", u);
		alloc_discharge(st->lead, (int)-u, pi_dis, out->dis_cap, rule, psp, pwhy);
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
	chain_forecast(cfg, in, st, out, &c);
	chain_source(cfg, in, st, out, &c);
	chain_scope(cfg, in, out, &c);
	chain_gates(cfg, in, st, out);
	chain_mode(cfg, in, st, out, &c);
	update_lead(cfg, st, out);
	if (c.n_dis)
		chain_discharge(cfg, in, st, out, &c);
	if (c.n_chg)
		chain_charge(cfg, st, out, &c);
	for (int p = 0; p < NPH; p++)                       /* the BMS limit cut this phase's setpoint */
		if ((out->sp[p] > 0 && out->ccl_bind[p] && out->sp[p] >= (int)out->chg_cap[p] - 1) ||
			(out->sp[p] < 0 && out->dcl_bind[p] && -out->sp[p] >= (int)out->dis_cap[p] - 1))
			strncat(out->why[p], out->sp[p] > 0 ? " CCL" : " DCL", WHY_LEN - 1 - strlen(out->why[p]));
	st->soyo_chg_prev = c.n_chg > 0;
	if (!c.n_dis)
		st->soyo_prop_prev = 0;
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
