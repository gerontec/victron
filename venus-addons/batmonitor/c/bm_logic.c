/*
 * bm_logic: one batmonitor cycle as a pure function (see bm_logic.h). The rules and their history are described in
 * batmonitor.c; this file holds the arithmetic only, unchanged from calc() of 0.19-c.
 */
#define _GNU_SOURCE
#include "bm_logic.h"
#include <math.h>
#include <stddef.h>
#include <stdio.h>
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
	{"DISCHARGE_MAX_PHASE",   P(discharge_max_phase),       4000,    0,       5000,    "W", "discharge per phase (MP2 48/5000 continuous)"},
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
	{"B_NIGHT",               P(b_night),                   936,     0,       4000,    "W", "night base discharge (no PV, heat pump off in winter)"},
	{"FC_HYST",               P(fc_hyst),                   2,       0,       20,      "%", "forecast rule back on above target + this"},
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

static double calc_arrays(const struct arr *a, int n, time_t t, int month)
{
	double elev, az;
	bm_sun_pos(t, &elev, &az);
	if (elev <= 0)
		return 0.0;
	double am = fmin(1.0 / sin(d2r(elev)), 37.0);
	double tr = pow(0.7, pow(am, 0.678));
	double kt = (month >= 1 && month <= 12) ? KT_MONTH[month] : 0.60;
	double shade = (elev >= ((az < HOR_AZ_SPLIT) ? HOR_EAST : HOR_SOUTH)) ? 1.0 : HOR_DIFFUSE;
	double e = d2r(elev), sum = 0;
	for (int i = 0; i < n; i++) {
		double b = d2r(a[i].tilt), da = d2r((az - 180.0) - a[i].az_s);
		sum += a[i].power * tr * kt * fmax(0.0, sin(e) * cos(b) + cos(e) * sin(b) * cos(da));
	}
	return sum * shade;
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

/* discharge w (W, > 0) over the phases in dis[], at most DISCHARGE_MAX_PHASE each; SoC balancing: the stack more
   than SOC_BALANCE_ON ahead delivers alone, what its phases cannot give goes to the other phases (0.22) */
static void alloc_discharge(const struct bm_cfg *cfg, int lead, int w, const int *dis, const char *rule, int *sp, char why[][WHY_LEN])
{
	int give[NPH] = {0}, n_give = 0, lead_ph, n_dis = 0;
	for (int p = 0; p < NPH; p++)
		n_dis += dis[p];
	if (lead >= 0)
		for (int i = 0; i < BM_BANKS[lead].nph; i++)
			if (dis[BM_BANKS[lead].ph[i]]) {
				give[BM_BANKS[lead].ph[i]] = 1;
				n_give++;
			}
	lead_ph = n_give > 0;
	if (!lead_ph)
		for (int p = 0; p < NPH; p++)
			if (dis[p]) {
				give[p] = 1;
				n_give++;
			}
	int dmax = (int)cfg->discharge_max_phase;
	int share = n_give ? (int)((double)w / n_give) : 0, spill = 0;
	if (share > dmax) {
		spill = (share - dmax) * n_give;
		share = dmax;
	}
	int n_rest = n_dis - n_give, extra = n_rest ? spill / n_rest : 0;
	if (extra > dmax)
		extra = dmax;
	for (int p = 0; p < NPH; p++) {
		if (!dis[p])
			continue;
		sp[p] = give[p] ? -share : -extra;
		snprintf(why[p], WHY_LEN, "%s%s", rule, lead_ph && give[p] ? "|BALANCE" : lead_ph ? (extra ? "|BALANCE_SPILL"
				 : "|BALANCE_HOLD") : "");
	}
}

/* charge rest (W) over the phases in chg[]: the banks in CHARGE_PRIORITY order up to their real charger capacity,
   the stack behind first; what is left up to CHARGE_MAX_PHASE on all phases alike */
static void alloc_charge(const struct bm_cfg *cfg, int lead, double rest, const int *chg, int n_chg, int *sp, char why[][WHY_LEN])
{
	int order[NBANK], k = 0;
	for (int i = 0; i < NBANK; i++)
		if (CHARGE_PRIORITY[i] != lead)
			order[k++] = CHARGE_PRIORITY[i];
	if (lead >= 0)
		order[k++] = lead;
	for (int i = 0; i < k; i++) {
		int b = order[i], ph[NPH], n = 0;
		for (int j = 0; j < BM_BANKS[b].nph; j++)
			if (chg[BM_BANKS[b].ph[j]])
				ph[n++] = BM_BANKS[b].ph[j];
		if (!n)
			continue;
		double share = fmin(rest, cfg->charger_cap_phase * n);
		for (int j = 0; j < n; j++) {
			sp[ph[j]] = (int)(share / n);
			strncat(why[ph[j]], "|CHARGE", WHY_LEN - 1 - strlen(why[ph[j]]));
		}
		rest -= share;
	}
	if (rest > 0)
		for (int p = 0; p < NPH; p++)
			if (chg[p])
				sp[p] += (int)fmin(rest / n_chg, cfg->charge_max_phase - sp[p]);
}

/* one cycle: the ESP soyo calculation per bank, the charge block and the PI prototype */
void bm_step(const struct bm_cfg *cfg, const struct bm_in *in, struct bm_state *st, struct bm_out *out)
{
	double now = in->now;
	struct tm loc;
	localtime_r(&in->t, &loc);
	int month = loc.tm_mon + 1;
	double pcc = in->pcc, pv = in->pv, bat1 = in->bat1;
	int have_pcc = in->have_pcc, have_pv = in->have_pv;

	memset(out, 0, sizeof(*out));
	int stale = in->inv_time == 0 || now - in->inv_time > STALE_SECONDS;
	int wp_running = in->r290_time > 0 && now - in->r290_time < STALE_SECONDS && in->r290_hz > 0;
	/* Z2 point = Z1 PCC + heat pump: the discharge serves only the house (0.19), winter only. Winter/summer from
	   the measured energy (season.py on .218: summer when export > 2 x heat pump over 7 days, winter below 1 x,
	   0.21), the months Oct-Apr only when that is missing or older than 2 days */
	int wp_fresh = in->wp_time > 0 && now - in->wp_time < WP_MAX_AGE;
	int season_ok = in->season && in->t - in->season_ts < SEASON_MAX_AGE;
	int season = season_ok ? in->season : (month >= SUMMER_FROM && month <= SUMMER_TO) ? BM_SUMMER : BM_WINTER;
	out->season = season;
	out->season_measured = season_ok;
	/* forecast (0.24, readers/forecast.py): while both stacks are above the SoC that the next day's PV surplus will
	   refill, they serve everything, the heat pump included, in any season (sell less, use more); below the target
	   the season rules apply again, back on above target + FC_HYST */
	{
		double min_soc = 101;
		for (int b = 0; b < NBANK; b++)
			if (in->bms_ok[b] && in->soc[b] < min_soc)
				min_soc = in->soc[b];
		int fc_ok = cfg->forecast && in->fc_target >= 0 && in->fc_ts > 0 && in->t - in->fc_ts < FC_MAX_AGE && min_soc <= 100;
		if (!fc_ok)
			st->fc_active = 0;
		else if (!st->fc_active && min_soc > in->fc_target + cfg->fc_hyst)
			st->fc_active = 1;
		else if (st->fc_active && min_soc < in->fc_target)
			st->fc_active = 0;
		out->fc_active = st->fc_active;
		out->fc_target = fc_ok ? in->fc_target : -1;
		out->fc_min_soc = min_soc;
		if (st->fc_active)
			season = BM_SUMMER;        /* serve the heat pump like in summer */
	}
	int winter = season != BM_SUMMER;          /* transition counts as winter for the WP_CAP fallback */
	/* the heat pump power the batteries must NOT cover: all of it in winter, the part above 1900 W in the
	   transition (0.22), none in summer */
	double wp_eff = 0.0;
	if (wp_fresh && in->wp > 0)
		wp_eff = season == BM_WINTER ? in->wp : season == BM_TRANSITION ? fmax(0.0, in->wp - cfg->wp_bat_max_transition) : 0.0;
	double z2 = pcc + wp_eff;
	int *sp = out->sp;
	char (*why)[WHY_LEN] = out->why;
	int discharge[NPH] = {0}, charge[NPH] = {0}, n_dis = 0, n_chg = 0;
	int pi_ok[NPH] = {0}, pi_chg[NPH] = {0}, pi_dis[NPH] = {0}, n_pichg = 0, n_pidis = 0;

	/* the meter already includes our own charging: use the measured AC-in, not the setpoints */
	double own_charge = 0.0, own_discharge = 0.0, multis = 0.0;
	for (int p = 0; p < NPH; p++)
		if (in->ac_ok[p]) {
			double a = in->ac_in[p];
			multis += a;
			if (a > 0)
				own_charge += a;
			else
				own_discharge -= a;
		}
	/* a Sofar Bat1 discharge is no surplus; its charging counts with BAT1_CHARGE_FACTOR. The own discharge is
	   subtracted (0.20, found by tests/test_logic.c): else the 936 W night floor, partly going into the Sofar
	   battery, counted as PV surplus and the Multis switched to charging at night / one stack charged the other */
	double bat1_eff = bat1 < 0 ? bat1 : bat1 * BAT1_CHARGE_FACTOR;
	double surplus = (have_pcc ? pcc : 0.0) + bat1_eff + own_charge - own_discharge;
	int block = ladesperre(cfg, in, st, out, stale, multis);
	/* surplus includes the own charging: once charging, it holds down to SOYO_HOLD_TH instead of 200 W */
	int charge_mode = surplus > PCC_SURPLUS_TH || (st->soyo_chg_prev && surplus > SOYO_HOLD_TH);

	for (int b = 0; b < NBANK; b++) {
		const char *bn = BM_BANKS[b].name;
		st->soc_ok[b] = in->bms_ok[b];
		if (st->soc_ok[b])
			st->soc[b] = in->soc[b];
		int have_power = in->power_ok[b];
		double power = in->power[b];
		if (st->soc_ok[b]) {
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
			sp[p] = 0;
			/* PI: own gates (the soyo ones depend on its fixed thresholds) */
			if (st->soc_ok[b] && !st->force[b] && !stale) {
				pi_ok[p] = 1;
				if (!block && st->soc[b] < 100.0) {
					pi_chg[p] = 1;
					n_pichg++;
				}
				if (!st->prot[b]) {
					pi_dis[p] = 1;
					n_pidis++;
				}
			}
			if (!st->soc_ok[b])
				snprintf(why[p], WHY_LEN, "%s:BMS_MISSING", bn);
			else if (st->force[b]) {
				sp[p] = (int)cfg->force_charge_w;
				snprintf(why[p], WHY_LEN, "%s:FORCE_CHARGE(%.1f%%)", bn, st->soc[b]);
			} else if (stale)
				snprintf(why[p], WHY_LEN, "STALE");
			else if (charge_mode && block)
				snprintf(why[p], WHY_LEN, "%s:LADESPERRE(peak %dh)", bn, out->ls.peak_h);
			else if (charge_mode) {
				snprintf(why[p], WHY_LEN, "%s:PV_SURPLUS", bn);
				if (st->soc[b] < 100.0) {
					charge[p] = 1;
					n_chg++;
				}
			} else if (st->prot[b])
				snprintf(why[p], WHY_LEN, "%s:DISCHARGE_PROTECTION(%.1f%%)", bn, st->soc[b]);
			else if (have_power && power > CHARGING_TH)
				snprintf(why[p], WHY_LEN, "%s:CHARGING(%.0fW)", bn, power);
			else {
				discharge[p] = 1;
				n_dis++;
			}
		}
	}
	update_lead(cfg, st, out);
	if (n_dis) {
		int night = have_pv && pv < NIGHT_PV_TH, w;
		char rule[64];
		/* deficit = what the Multis already give (minus what of it goes into the Sofar battery) + the import still
		   left (0.18; old: -KP * pcc, which dropped back to IDLE as soon as the own discharge covered the import) */
		/* the own charging (force charge of the other stack) is no house load: it comes from the grid (0.20) */
		double house = z2 + own_charge;
		double deficit = own_discharge - (bat1 > 0 ? bat1 : 0.0) - KP * (house - cfg->soyo_target);
		/* winter: the night base load would flow into the heat pump */
		int night_floor = night && (season != BM_WINTER || !wp_fresh || in->wp < WP_ON_TH);
		if (house < PCC_IMPORT_TH || (st->soyo_prop_prev && deficit > SOYO_HOLD_TH)) {
			w = deficit > 0 ? (int)deficit : 0;   /* never turn a discharge into charging (Sofar TOU charge) */
			if (night_floor && w < (int)cfg->b_night)
				w = (int)cfg->b_night;
			if (w > (int)cfg->w_max)
				w = (int)cfg->w_max;
			strcpy(rule, "PROPORTIONAL");
			st->soyo_prop_prev = 1;
		} else {
			st->soyo_prop_prev = 0;
			w = night_floor ? (int)cfg->b_night : B_DAY_IDLE;
			strcpy(rule, "IDLE");
		}
		if (night)
			strcat(rule, "|NIGHT");
		if (wp_eff >= WP_ON_TH)
			strcat(rule, season == BM_TRANSITION ? "|WP1900" : "|Z2");
		if (st->fc_active)
			strcat(rule, "|FC");
		if (!wp_fresh && winter && wp_running && w > (int)cfg->wp_cap) {
			w = (int)cfg->wp_cap;
			strcat(rule, "|WP_CAP");
		}
		alloc_discharge(cfg, st->lead, w, discharge, rule, sp, why);
	}
	if (n_chg)
		alloc_charge(cfg, st->lead, fmin((int)(own_charge + KP * ((have_pcc ? pcc : 0.0) + bat1_eff - cfg->soyo_target)),
									cfg->charge_max_phase * n_chg), charge, n_chg, sp, why);
	st->soyo_chg_prev = n_chg > 0;
	if (!n_dis)
		st->soyo_prop_prev = 0;

	/* PI prototype. y = PCC + Sofar Bat1: Bat1 charging counts with BAT1_CHARGE_FACTOR (as the soyo surplus);
	   a Bat1 discharge counts while the Multis charge (the Sofar must not feed them) and at night (the Multis take
	   the base load over from the Sofar, soyo: fixed 936 W); by day while they discharge it is ignored (the Sofar
	   battery covers the house first, the Multis only real grid import, as soyo) */
	{
		int night = have_pv && pv < NIGHT_PV_TH;
		double applied = 0;
		for (int p = 0; p < NPH; p++)
			if (pi_ok[p])
				applied += st->setpoints[p];
		double b1 = bat1 > 0 ? bat1 * BAT1_CHARGE_FACTOR
					: (applied > 0 || (night && (season != BM_WINTER || !wp_fresh || in->wp < WP_ON_TH))) ? bat1 : 0.0;
		/* discharge side on the Z2 point (0.19), charge side on the Z1 PCC, as soyo */
		double y = (have_pcc ? (applied > 0 ? pcc : z2) : 0.0) + b1, e = y - PI_TARGET;
		if (fabs(e) < PI_DEADBAND)
			e = 0;
		double dt = st->pi_t_prev > 0 ? fmin(fmax(now - st->pi_t_prev, 1.0), 30.0) : CYCLE_SECONDS;
		double u = applied + (st->pi_t_prev > 0 ? PI_KP * (e - st->pi_e_prev) : 0) + PI_KI * dt * e;
		double dis_cap = (!wp_fresh && winter && wp_running) ? cfg->wp_cap : cfg->w_max;
		double u_max = cfg->charge_max_phase * n_pichg, u_min = n_pidis ? -dis_cap : 0;
		/* crossing from discharge (Z2 side) into charging: never more than the real Z1 export, else the Sofar Bat1
		   would feed the charging while the heat pump runs */
		if (applied <= 0 && u > 0)
			u = fmax(0.0, fmin(u, (have_pcc ? pcc : 0.0) + b1 - PI_TARGET));
		u = fmax(u_min, fmin(u_max, u));
		st->pi_e_prev = e;
		st->pi_t_prev = now;
		int psp[NPH] = {0};
		char pwhy[NPH][WHY_LEN];
		for (int p = 0; p < NPH; p++)
			snprintf(pwhy[p], WHY_LEN, "%.95s", why[p]);
		for (int p = 0; p < NPH; p++)
			if (pi_ok[p])
				snprintf(pwhy[p], WHY_LEN, "PI(%+.0f)", u);
		if (u > 0)
			alloc_charge(cfg, st->lead, u, pi_chg, n_pichg, psp, pwhy);
		else if (u < 0) {
			char rule[32];
			snprintf(rule, sizeof(rule), "PI(%+.0f)", u);
			alloc_discharge(cfg, st->lead, (int)-u, pi_dis, rule, psp, pwhy);
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
			memcpy(why, pwhy, sizeof(pwhy));
		}
	}
	out->surplus = surplus;
	out->wp_eff = wp_eff;
	memcpy(st->setpoints, sp, sizeof(st->setpoints));
}
