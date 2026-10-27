/*
 * test_logic: bm_step against a plant model, no D-Bus/MQTT (make test).
 *
 * Plant per 5 s cycle (powers in W):
 *   Multis      follow last cycle's setpoints (+ = charge from AC), DC power of a bank = sum of its phases
 *   net         = PV - house - heat pump - Multis      (what is left behind the Sofar)
 *   Sofar Bat1  holds its PCC (at Z1, incl. heat pump) at 0 within +-2500 W, if its SoC allows
 *   PCC         = net - Bat1                           (+ = export at Z1)
 * Each scenario runs N cycles and checks the steady state.
 */
#define _GNU_SOURCE
#include "bm_logic.h"
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int fails, checks;

#define CHECK(cond, ...) do { checks++; if (!(cond)) { fails++; printf("  FAIL %s:%d: ", __func__, __LINE__); \
	printf(__VA_ARGS__); putchar('\n'); } } while (0)

struct plant {
	double pv, house, wp;          /* W */
	int sofar_dis, sofar_chg;      /* Sofar Bat1 may discharge / charge */
	double soc[NBANK];
	int wp_fresh, r290_hz, stale_inv;
	int season, season_age_h;      /* batmonitor/season: 0 none, BM_SUMMER, BM_WINTER; age of its ts */
	double fc_target;              /* batmonitor/forecast target SoC, 0 = none */
	int fc_age_h;
	double bat1_soc;               /* Sofar Bat1 SoC %, 0 = not sent */
	double aussen;
	double volt[NBANK];            /* BMS voltage, 0 = none; given: the Multis saturate at 70 A x U / 0.93 (real) */
	int lim[NBANK];                /* 1 = the BMS sends CCL / DCL */
	double ccl[NBANK], dcl[NBANK]; /* A */
	double pv_sofar;               /* measured Sofar PV (the forecast anchor), 0 = pv */
	double fc_kt;                  /* OWM kt of every 3 h slot (correction 1.0), 0 = no slots */
	double acout_feed[NPH];        /* W PV feeding back into each AC-out (FoxESS on AC-out1), 0 = none */
	int grid_off;                  /* 1 = grid offline (island, transfer relay open) */
	int force_full;                /* batmonitor/force_full_from_pv: 0 off, 1 = S1, 2 = S2 (0.62-c) */
	double cell_min[NBANK], cell_max[NBANK];   /* BMS lowest / highest cell V, 0 = not sent (0.67-c) */
};

static double r_override_w_max;     /* > 0: W_MAX for the next simulate() (parameter test) */
static int r_override_cap_off;   /* 1: EXPORT_CAP 0 in simulate */

struct run {
	struct bm_cfg cfg;
	struct bm_state st;
	struct bm_out out;
	struct bm_in in;
	double ac[NPH], pcc, bat1;
	double pcc_hist[1000];
	int n;
	int do4_pulses;                /* D5 pulses over the run (0.46-c) */
};

static time_t local_time(int y, int mo, int d, int h, int mi)
{
	struct tm tm = {0};
	tm.tm_year = y - 1900;
	tm.tm_mon = mo - 1;
	tm.tm_mday = d;
	tm.tm_hour = h;
	tm.tm_min = mi;
	tm.tm_isdst = -1;
	return mktime(&tm);
}

/* run n cycles from t0, the plant reacting to the setpoints */
static void simulate(struct run *r, const struct plant *pl, time_t t0, int n)
{
	memset(r, 0, sizeof(*r));
	bm_cfg_default(&r->cfg);
	if (r_override_w_max > 0)
		bm_param_set(&r->cfg, 0, r_override_w_max);
	if (r_override_cap_off)
		r->cfg.export_cap = 0;
	bm_init(&r->st);
	double now = 1000.0;
	for (int k = 0; k < n; k++, now += CYCLE_SECONDS) {
		double multis = r->ac[0] + r->ac[1] + r->ac[2];
		double net = pl->pv - pl->house - pl->wp - multis;
		double b1 = 0;
		if (net < 0 && pl->sofar_dis)
			b1 = fmax(net, -2500);
		if (net > 0 && pl->sofar_chg)
			b1 = fmin(net, 2500);
		r->pcc = net - b1;
		r->bat1 = b1;
		struct bm_in *in = &r->in;
		memset(in, 0, sizeof(*in));
		in->now = now;
		in->t = t0 + (time_t)(k * CYCLE_SECONDS);
		in->have_pcc = in->have_pv = 1;
		in->pcc = in->pcc_avg5 = r->pcc;
		in->bat1 = in->bat1_avg5 = b1;
		in->have_soc_bat1 = pl->bat1_soc > 0;
		in->soc_bat1 = pl->bat1_soc;
		in->pv = pl->pv_sofar > 0 ? pl->pv_sofar : pl->pv;
		if (pl->fc_kt > 0) {
			in->fc_corr = 1.0;
			in->fc_n = 8;
			for (int i = 0; i < 8; i++) {
				in->fc_slot_t[i] = (t0 / 10800) * 10800 + (time_t)i * 10800;
				in->fc_slot_kt[i] = pl->fc_kt;
			}
		}
		in->inv_time = pl->stale_inv ? 1.0 : now;
		in->grid_ok = !pl->grid_off;
		in->force_full = pl->force_full;
		in->r290_hz = pl->r290_hz;
		in->r290_time = now;
		in->aussen = pl->aussen;
		in->aussen_time = now;
		in->wp = pl->wp;
		in->wp_time = pl->wp_fresh ? now : 0;
		in->season = pl->season;
		in->season_ts = in->t - pl->season_age_h * 3600;
		in->fc_target = pl->fc_target > 0 ? pl->fc_target : -1;
		in->fc_ts = in->t - pl->fc_age_h * 3600;
		for (int p = 0; p < NPH; p++) {
			in->ac_ok[p] = 1;
			in->ac_in[p] = r->ac[p];
			in->ac_out_ok[p] = 1;
			in->ac_out[p] = 50 - pl->acout_feed[p];         /* 50 W loads, - = feed */
		}
		for (int b = 0; b < NBANK; b++) {
			in->bms_ok[b] = 1;
			in->soc[b] = pl->soc[b];
			in->power_ok[b] = 1;
			in->volt_ok[b] = pl->volt[b] > 0;
			in->volt[b] = pl->volt[b];
			in->lim_ok[b] = pl->lim[b];
			in->ccl[b] = pl->ccl[b];
			in->dcl[b] = pl->dcl[b];
			in->cell_ok[b] = pl->cell_min[b] > 0;
			in->cell_min[b] = pl->cell_min[b];
			in->cell_max_ok[b] = pl->cell_max[b] > 0;
			in->cell_max[b] = pl->cell_max[b];
			in->power[b] = 0;
			for (int i = 0; i < BM_BANKS[b].nph; i++)
				in->power[b] += r->ac[BM_BANKS[b].ph[i]];
		}
		bm_step(&r->cfg, in, &r->st, &r->out);
		r->do4_pulses += r->out.do4_pulse;
		for (int p = 0; p < NPH; p++) {
			r->ac[p] = r->out.sp[p];     /* the Multis follow within one cycle */
			for (int b = 0; b < NBANK; b++)
				for (int i = 0; i < BM_BANKS[b].nph; i++)
					if (BM_BANKS[b].ph[i] == p && pl->volt[b] > 0 && r->ac[p] > 70.0 * pl->volt[b] / 0.93)
						r->ac[p] = 70.0 * pl->volt[b] / 0.93;   /* charger limit */
		}
		if (r->n < 1000)
			r->pcc_hist[r->n++] = r->pcc;
	}
}

static int sum_sp(const struct run *r) { return r->out.sp[0] + r->out.sp[1] + r->out.sp[2]; }

static int rule_has(const struct run *r, const char *s)
{
	for (int p = 0; p < NPH; p++)
		if (strstr(r->out.why[p], s))
			return 1;
	return 0;
}

/* largest PCC swing over the last k cycles: oscillation check */
static double pcc_swing(const struct run *r, int k)
{
	double lo = 1e9, hi = -1e9;
	for (int i = r->n - k; i < r->n; i++) {
		lo = fmin(lo, r->pcc_hist[i]);
		hi = fmax(hi, r->pcc_hist[i]);
	}
	return hi - lo;
}

static void print_state(const char *name, const struct run *r)
{
	printf("  %-44s pcc %+6.0f bat1 %+6.0f sp %+5d %+5d %+5d  %s | %s | %s\n", name, r->pcc, r->bat1,
		   r->out.sp[0], r->out.sp[1], r->out.sp[2], r->out.why[0], r->out.why[1], r->out.why[2]);
}

/* defaults first, the scenario's fields after them (-Woverride-init is intended here) */
#pragma GCC diagnostic ignored "-Woverride-init"
#define BASE(...) ((struct plant){.soc = {50, 50}, .wp_fresh = 1, .aussen = 10, __VA_ARGS__})

/* 0.59-c (user 2026-10-24: PCC near 0): the heat pump is house load the stacks cover (0.19..0.58-c: in winter it came
   from the Z2 grid). 0.61-c: in the winter half-year with the heat pump running at most WP_WINTER_W (1000 W) */
static void test_winter_wp_from_battery(void)
{
	struct run r;
	struct plant pl = BASE(.house = 500, .wp = 4000, .r290_hz = 60);
	simulate(&r, &pl, local_time(2026, 1, 15, 22, 0), 60);       /* winter night, Sofar Bat1 empty */
	print_state("winter night, WP 4 kW, Sofar empty", &r);
	CHECK(abs(sum_sp(&r) + 1000) <= 3, "0.61-c: Multis give WP_WINTER_W 1000 W, sp sum %d", sum_sp(&r));
	CHECK(fabs(r.pcc + 3500) <= 80, "the rest from the grid, pcc %.0f", r.pcc);
	CHECK(!rule_has(&r, "WP:"), "no heat pump rule text");
	CHECK(pcc_swing(&r, 20) < 100, "no oscillation, swing %.0f", pcc_swing(&r, 20));
}

static void test_winter_wp_5kw(void)
{
	struct run r;
	struct plant pl = BASE(.house = 400, .wp = 5000, .r290_hz = 70);
	simulate(&r, &pl, local_time(2026, 2, 1, 3, 0), 60);
	print_state("winter night, WP 5 kW, Sofar empty", &r);
	CHECK(abs(sum_sp(&r) + 1000) <= 3, "0.61-c: 1000 W from the stacks, sp sum %d", sum_sp(&r));
	CHECK(fabs(r.pcc + 4400) <= 80, "the rest from the grid, pcc %.0f", r.pcc);
}

static void test_summer_wp_from_battery(void)
{
	struct run r;
	struct plant pl = BASE(.house = 500, .wp = 1200, .r290_hz = 40);
	simulate(&r, &pl, local_time(2026, 7, 15, 23, 0), 60);       /* summer night: WP from the batteries */
	print_state("summer night, WP 1.2 kW, Sofar empty", &r);
	CHECK(abs(sum_sp(&r) + 1700) <= 60, "Multis should cover house + WP 1700 W, sp sum %d", sum_sp(&r));
	CHECK(fabs(r.pcc) <= 80, "no grid import in summer, pcc %.0f", r.pcc);
	CHECK(!rule_has(&r, "WP:"), "no heat pump rule text");
}

static void test_summer_wp_4kw_from_battery(void)
{
	struct run r;
	struct plant pl = BASE(.house = 500, .wp = 4000, .r290_hz = 60);
	simulate(&r, &pl, local_time(2026, 7, 15, 23, 0), 60);
	print_state("summer night, WP 4 kW, Sofar empty", &r);
	CHECK(abs(sum_sp(&r) + 4500) <= 60, "house + WP 4.5 kW from the batteries (W_MAX 10 kW), sp sum %d", sum_sp(&r));
	CHECK(fabs(r.pcc) <= 80, "no grid import, pcc %.0f", r.pcc);
}

static void test_w_max_and_phase_cap(void)
{
	struct run r;
	struct plant pl = BASE(.house = 14000, .r290_hz = 0, .season = BM_SUMMER, .season_age_h = 1);
	simulate(&r, &pl, local_time(2026, 7, 15, 23, 0), 60);
	print_state("summer night, house 14 kW", &r);
	CHECK(sum_sp(&r) >= -10000 && sum_sp(&r) <= -9990, "W_MAX 10 kW total, sp sum %d", sum_sp(&r));
	for (int p = 0; p < NPH; p++)
		CHECK(r.out.sp[p] >= -3800, "phase %d at most 3800 W, got %d", p, r.out.sp[p]);

	pl = BASE(.house = 6000, .soc = {60, 50}, .season = BM_SUMMER, .season_age_h = 1);
	simulate(&r, &pl, local_time(2026, 7, 15, 23, 0), 60);       /* Stack1 leads, but L1 alone cannot give 6 kW */
	print_state("Stack1 leads, house 6 kW", &r);
	CHECK(r.out.sp[0] == -3800 && rule_has(&r, " SPILL"), "L1 at 3800 W, the rest spills to L2/L3, got %d %d %d",
		  r.out.sp[0], r.out.sp[1], r.out.sp[2]);
	CHECK(fabs(r.pcc) <= 80, "house covered, pcc %.0f", r.pcc);
}

static void test_bank_split(void)
{
	struct run r;
	struct plant pl = BASE(.house = 4000, .season = BM_SUMMER, .season_age_h = 1);
	simulate(&r, &pl, local_time(2026, 7, 15, 23, 0), 60);       /* 0.26: L1 = L2 + L3 (0.63-c: above SINGLE_DIS_W) */
	print_state("bank split, house 4 kW", &r);
	CHECK(abs(r.out.sp[0] - (r.out.sp[1] + r.out.sp[2])) <= 3 && abs(r.out.sp[1] - r.out.sp[2]) <= 1,
		  "L1 = L2 + L3, got %d %d %d", r.out.sp[0], r.out.sp[1], r.out.sp[2]);
	CHECK(abs(sum_sp(&r) + 4000) <= 40, "house covered, sp sum %d", sum_sp(&r));

	pl = BASE(.house = 9000, .season = BM_SUMMER, .season_age_h = 1);
	simulate(&r, &pl, local_time(2026, 7, 15, 23, 0), 60);       /* L1 would need 4500 W: capped at 3800 W, rest to L2/L3 */
	print_state("bank split, house 9 kW", &r);
	CHECK(r.out.sp[0] == -3800, "L1 capped at 3800 W, got %d", r.out.sp[0]);
	CHECK(abs(r.out.sp[1] + 2600) <= 30 && abs(r.out.sp[2] + 2600) <= 30, "L2/L3 take the spill, got %d %d",
		  r.out.sp[1], r.out.sp[2]);
	CHECK(fabs(r.pcc) <= 80, "house covered, pcc %.0f", r.pcc);
}

/* 0.63-c: with the grid there one MultiPlus discharges up to SINGLE_DIS_W (the meter nets the phases) */
static void test_single_unit(void)
{
	struct run r;
	struct plant pl = BASE(.house = 2000, .season = BM_SUMMER, .season_age_h = 1);
	simulate(&r, &pl, local_time(2026, 7, 15, 23, 0), 60);
	print_state("one unit, house 2 kW", &r);
	CHECK(abs(r.out.sp[0] + 2000) <= 30 && r.out.sp[1] == 0 && r.out.sp[2] == 0 && rule_has(&r, " 1PH") &&
		  rule_has(&r, " STBY"), "L1 alone (equal SoC: the first stack), got %d %d %d", r.out.sp[0], r.out.sp[1],
		  r.out.sp[2]);
	CHECK(fabs(r.pcc) <= 80, "house covered, pcc %.0f", r.pcc);

	pl = BASE(.house = 2000, .soc = {50, 52}, .season = BM_SUMMER, .season_age_h = 1);
	simulate(&r, &pl, local_time(2026, 7, 15, 23, 0), 60);
	print_state("one unit, Stack2 fuller", &r);
	CHECK(r.out.sp[0] == 0 && abs(r.out.sp[1] + 2000) <= 30 && r.out.sp[2] == 0,
		  "L2 alone (Stack2 fuller), got %d %d %d", r.out.sp[0], r.out.sp[1], r.out.sp[2]);

	pl = BASE(.house = 3400, .season = BM_SUMMER, .season_age_h = 1);
	simulate(&r, &pl, local_time(2026, 7, 15, 23, 0), 8);        /* 0.64-c: 40 s above, one unit carries it */
	print_state("house 3.4 kW for 40 s: one unit", &r);
	CHECK(abs(r.out.sp[0] + 3400) <= 40 && r.out.sp[1] == 0 && r.out.sp[2] == 0 && !r.st.single_all,
		  "below SINGLE_DIS_UP_S one unit up to its cap, got %d %d %d", r.out.sp[0], r.out.sp[1], r.out.sp[2]);

	pl = BASE(.house = 3400, .season = BM_SUMMER, .season_age_h = 1);
	simulate(&r, &pl, local_time(2026, 7, 15, 23, 0), 60);
	print_state("house 3.4 kW: all units", &r);
	CHECK(r.out.sp[0] < 0 && r.out.sp[1] < 0 && r.out.sp[2] < 0 && r.st.single_all,
		  "above SINGLE_DIS_W all phases, got %d %d %d", r.out.sp[0], r.out.sp[1], r.out.sp[2]);
	CHECK(fabs(r.pcc) <= 80, "house covered, pcc %.0f", r.pcc);

	pl = BASE(.house = 2000, .season = BM_SUMMER, .season_age_h = 1, .grid_off = 1);
	simulate(&r, &pl, local_time(2026, 7, 15, 23, 0), 60);
	print_state("off-grid, house 2 kW", &r);
	CHECK(r.out.sp[0] < 0 && r.out.sp[1] < 0 && r.out.sp[2] < 0, "off-grid every phase, got %d %d %d",
		  r.out.sp[0], r.out.sp[1], r.out.sp[2]);
}

static void test_winter_night_floor(void)
{
	struct run r;
	struct plant pl = BASE(.house = 500, .sofar_dis = 1, .sofar_chg = 1);
	simulate(&r, &pl, local_time(2026, 1, 15, 22, 0), 40);       /* no heat pump: the stacks take over the Sofar */
	print_state("winter night, no WP, Sofar covers", &r);
	CHECK(abs(sum_sp(&r) + 530) <= 30, "Multis cover the house 500 W + 30 W trickle (0.25/0.27), sp sum %d", sum_sp(&r));
	CHECK(r.bat1 >= 10 && r.bat1 <= 50, "Sofar Bat1 charges gently ~30 W, never discharges, bat1 %.0f", r.bat1);
	CHECK(fabs(r.pcc) <= 30, "PCC near 0, pcc %.0f", r.pcc);
	CHECK(pcc_swing(&r, 20) < 60, "no oscillation, swing %.0f", pcc_swing(&r, 20));

	pl = BASE(.house = 300, .sofar_dis = 1, .sofar_chg = 1);
	simulate(&r, &pl, local_time(2026, 10, 8, 20, 30), 40);      /* 2026-10-08: 936 W floor charged the Sofar +600 W */
	print_state("night, house 300 W, Sofar charges/discharges", &r);
	CHECK(abs(sum_sp(&r) + 330) <= 30, "Multis cover 300 W + 30 W trickle, not 936 W, sp sum %d", sum_sp(&r));
	CHECK(r.bat1 >= 10 && r.bat1 <= 50, "only the 30 W trickle goes into the Sofar battery, bat1 %.0f", r.bat1);
}

/* 0.59-c: the night floor also while the heat pump runs (0.19..0.58-c: not in winter, SRC B1W); 0.61-c: summer
   half-year only (winter: the heat pump rule, test_winter_wp_rule) */
static void test_wp_running_night_floor(void)
{
	struct run r;
	struct plant pl = BASE(.house = 300, .wp = 3000, .r290_hz = 50, .sofar_dis = 1, .sofar_chg = 1);
	simulate(&r, &pl, local_time(2026, 7, 15, 22, 0), 60);       /* Sofar could cover, SOC_Bat1 unknown: no B1FIRST */
	print_state("summer night, WP 3 kW, Sofar could cover", &r);
	CHECK(rule_has(&r, "SRC:STK"), "night floor while the WP runs");
	CHECK(abs(sum_sp(&r) + 3330) <= 60, "stacks take house + WP + trickle over, sp sum %d", sum_sp(&r));
	CHECK(r.bat1 >= 0 && fabs(r.pcc) <= 60, "Sofar not discharging, PCC near 0, bat1 %.0f pcc %.0f", r.bat1, r.pcc);
}

static void test_day_surplus_to_zero(void)
{
	struct run r;
	struct plant pl = BASE(.pv = 5000, .house = 800, .soc = {50, 50});
	simulate(&r, &pl, local_time(2026, 3, 10, 12, 0), 40);       /* Sofar full: no Bat1 */
	print_state("day, PV 5 kW, house 0.8 kW, Sofar full", &r);
	CHECK(fabs(r.pcc) <= 60, "charging to PCC 0, pcc %.0f", r.pcc);
	CHECK(abs(sum_sp(&r) - 4200) <= 60, "Multis take 4200 W, sp sum %d", sum_sp(&r));
	CHECK(pcc_swing(&r, 20) < 100, "no oscillation, swing %.0f", pcc_swing(&r, 20));
}

static void test_real_charge_ceiling(void)
{
	struct run r;
	/* 0.29-c: 16 kW PV, house 0.8 kW: with the BMS voltage 53.7 V no phase above 70 A x 53.7 V / 0.93 = 4042 W */
	struct plant pl = BASE(.pv = 16000, .house = 800, .soc = {50, 50}, .volt = {53.7, 53.7});
	simulate(&r, &pl, local_time(2026, 3, 10, 12, 0), 40);
	print_state("day, PV 16 kW, BMS 53.7 V", &r);
	for (int p = 0; p < NPH; p++)
		CHECK(r.out.sp[p] <= 4043 && r.out.sp[p] >= 4000, "phase %d at the real ceiling ~4042 W, got %d", p, r.out.sp[p]);
	CHECK(fabs(r.out.chg_cap[0] - 4041.9) < 1, "charge_cap L1 %.1f", r.out.chg_cap[0]);
	CHECK(r.pcc > 2500, "the rest is exported, pcc %.0f", r.pcc);
	struct plant pl2 = BASE(.pv = 16000, .house = 800, .soc = {50, 50});
	simulate(&r, &pl2, local_time(2026, 3, 10, 12, 0), 40);
	CHECK(sum_sp(&r) == 3 * 4200, "without a BMS voltage CHARGER_CAP_PHASE per phase, no spill, sp sum %d", sum_sp(&r));
}

static void test_bms_current_limits(void)
{
	struct run r;
	/* 0.35-c: the MUST BMS tapers its CCL near full (15 A at 99 %): L1 at most 15 A x 55 V / 0.93 = 887 W, the
	   Pytes phases keep their charger ceiling */
	struct plant pl = BASE(.pv = 16000, .house = 800, .soc = {90, 90}, .volt = {55, 55}, .lim = {1, 1},
						   .ccl = {15, 586}, .dcl = {200, 586});
	simulate(&r, &pl, local_time(2026, 3, 10, 12, 0), 40);
	print_state("day, MUST CCL 15 A", &r);
	CHECK(r.out.sp[0] >= 880 && r.out.sp[0] <= 888 && strstr(r.out.why[0], " CCL"), "L1 at the CCL ~887 W, got %d (%s)",
		  r.out.sp[0], r.out.why[0]);
	CHECK(r.out.sp[1] >= 4000 && r.out.sp[2] >= 4000, "L2/L3 at their charger ceiling, got %d %d", r.out.sp[1], r.out.sp[2]);
	CHECK(!strstr(r.out.why[1], " CCL"), "no CCL tag on L2 (%s)", r.out.why[1]);

	pl.ccl[0] = 0;                                             /* CCL 0: Stack1 takes nothing */
	simulate(&r, &pl, local_time(2026, 3, 10, 12, 0), 40);
	CHECK(r.out.sp[0] == 0, "CCL 0: L1 does not charge, got %d", r.out.sp[0]);

	pl.ccl[0] = 100;
	pl.ccl[1] = 30;                                            /* a bank CCL is split over its phases */
	simulate(&r, &pl, local_time(2026, 3, 10, 12, 0), 40);
	CHECK(r.out.sp[1] >= 880 && r.out.sp[1] <= 888 && r.out.sp[2] >= 880 && r.out.sp[2] <= 888,
		  "Stack2 CCL 30 A over L2 + L3: ~887 W each, got %d %d", r.out.sp[1], r.out.sp[2]);

	/* discharge: MUST DCL 10 A at 52 V -> L1 at most 10 x 52 x 0.93 = 484 W, the rest of the 2 kW from L2/L3 */
	struct plant pd = BASE(.house = 2000, .season = BM_SUMMER, .season_age_h = 1, .volt = {52, 52}, .lim = {1, 1},
						   .ccl = {100, 586}, .dcl = {10, 586});
	simulate(&r, &pd, local_time(2026, 7, 15, 23, 0), 60);
	print_state("night, MUST DCL 10 A", &r);
	CHECK(r.out.sp[0] <= -480 && r.out.sp[0] >= -484 && strstr(r.out.why[0], " DCL"), "L1 at the DCL ~-484 W, got %d (%s)",
		  r.out.sp[0], r.out.why[0]);
	CHECK(abs(sum_sp(&r) + 2000) <= 30, "house covered by L2/L3, sp sum %d", sum_sp(&r));
}

/* 0.59-c: charging on the real PCC in every season (0.30-c..0.58-c: in winter on PCC + WP, the grid paid the WP) */
static void test_winter_charge_on_pcc(void)
{
	struct run r;
	struct plant pl = BASE(.pv = 9000, .house = 800, .wp = 4000, .r290_hz = 60);
	simulate(&r, &pl, local_time(2026, 12, 21, 12, 0), 60);
	print_state("Dec noon, PV 9 kW, WP 4 kW", &r);
	CHECK(abs(sum_sp(&r) - 4200) <= 80, "stacks take PV - house - WP, sp sum %d", sum_sp(&r));
	CHECK(fabs(r.pcc) <= 80, "PCC near 0, pcc %.0f", r.pcc);
	CHECK(pcc_swing(&r, 20) < 100, "no oscillation, swing %.0f", pcc_swing(&r, 20));
	/* Sofar with room: it no longer covers the WP from Bat1 while the PV goes into the stacks (0.30-c side effect) */
	struct plant pb = BASE(.pv = 9000, .house = 800, .wp = 4000, .r290_hz = 60, .sofar_dis = 1, .sofar_chg = 1);
	simulate(&r, &pb, local_time(2026, 12, 21, 12, 0), 80);
	print_state("Dec noon, Sofar Bat1 can discharge", &r);
	CHECK(fabs(r.bat1) <= 100 && fabs(r.pcc) <= 100, "no Bat1 -> WP transfer, bat1 %.0f pcc %.0f", r.bat1, r.pcc);
}

static void test_day_sofar_covers_house(void)
{
	struct run r;
	struct plant pl = BASE(.pv = 300, .house = 1500, .sofar_dis = 1, .sofar_chg = 1);
	simulate(&r, &pl, local_time(2026, 3, 10, 15, 0), 30);
	print_state("day, Sofar covers the house", &r);
	CHECK(sum_sp(&r) >= -20 && sum_sp(&r) <= -18, "Multis idle 20 W (3 x -6 after integer split), sp sum %d", sum_sp(&r));
}

static void test_day_sofar_saturated(void)
{
	struct run r;
	struct plant pl = BASE(.pv = 300, .house = 3000, .sofar_dis = 1, .sofar_chg = 1);
	simulate(&r, &pl, local_time(2026, 3, 10, 15, 0), 60);       /* Sofar at 2.5 kW: 200 W import left */
	print_state("day, Sofar saturated, 200 W import", &r);
	CHECK(abs(sum_sp(&r) + 200) <= 10, "Multis cover the missing 200 W, sp sum %d", sum_sp(&r));
	CHECK(pcc_swing(&r, 30) < 50, "holds without oscillation, swing %.0f", pcc_swing(&r, 30));
}

static void test_force_charge(void)
{
	struct run r;
	struct plant pl = BASE(.pv = 300, .house = 500, .soc = {2.0, 50});
	simulate(&r, &pl, local_time(2026, 1, 15, 14, 0), 30);       /* day: no night floor */
	print_state("Stack1 at 2 %, day", &r);
	CHECK(r.out.sp[0] == 500, "force charge 500 W on L1, got %d", r.out.sp[0]);
	CHECK(abs(r.out.sp[1] + r.out.sp[2] + 200) <= 30, "Stack2 covers only the house deficit 200 W, L2+L3 %d",
		  r.out.sp[1] + r.out.sp[2]);
	CHECK(fabs(r.pcc + 500) <= 40, "the force charge comes from the grid, pcc %.0f", r.pcc);
	CHECK(strstr(r.out.why[0], "FORCE(") != NULL, "rule FORCE_CHARGE, got %s", r.out.why[0]);

	/* 0.48-c: the force charge obeys the BMS charge limit like every charge (BMS cold / full: CCL 0) */
	pl.volt[0] = 52.0;
	pl.lim[0] = 1;
	pl.ccl[0] = 0;
	pl.dcl[0] = 100;
	simulate(&r, &pl, local_time(2026, 1, 15, 14, 0), 30);
	CHECK(r.out.sp[0] == 0, "CCL 0 A: no force charge, got %d", r.out.sp[0]);
	pl.ccl[0] = 5;                                               /* 5 A x 52 V / 0.93 = 279 W on the one phase */
	simulate(&r, &pl, local_time(2026, 1, 15, 14, 0), 30);
	CHECK(r.out.sp[0] == 279, "CCL 5 A: force charge 279 W, got %d", r.out.sp[0]);
	CHECK(strstr(r.out.why[0], "FORCE(") != NULL, "still rule FORCE, got %s", r.out.why[0]);
}

/* 0.62-c (user 2026-10-26: force balancing): force_full_from_pv S1 charges Stack1 first from the PV even when it is
   the SoC lead, keeps it in CHARGE at 100 %, never discharges it and ends after FORCE_FULL_HOLD_MIN at 100 % */
static void test_force_full_from_pv(void)
{
	struct run r;
	struct plant pl = BASE(.pv = 2500, .house = 500, .wp = 0, .soc = {60, 30});
	simulate(&r, &pl, local_time(2026, 1, 15, 12, 0), 60);
	print_state("2 kW surplus, Stack1 ahead, no force_full", &r);
	CHECK(r.out.sp[0] == 0 && rule_has(&r, " BAL"), "without: the lead Stack1 charges last, L1 %d", r.out.sp[0]);
	pl.force_full = 1;
	simulate(&r, &pl, local_time(2026, 1, 15, 12, 0), 60);
	print_state("2 kW surplus, force_full S1", &r);
	CHECK(abs(r.out.sp[0] - 2000) <= 60 && r.out.sp[1] + r.out.sp[2] <= 30, "S1: the surplus into Stack1 first, "
		  "sp %d %d %d", r.out.sp[0], r.out.sp[1], r.out.sp[2]);
	CHECK(strstr(r.out.why[0], "CHARGE SRC:DAY FF") != NULL, "rule text FF, got %s", r.out.why[0]);
	CHECK(fabs(r.pcc) <= 60, "PCC near 0, pcc %.0f", r.pcc);

	pl.pv = 6500;                                                /* more than one charger: the rest to Stack2 */
	pl.volt[0] = pl.volt[1] = 53.0;
	simulate(&r, &pl, local_time(2026, 1, 15, 12, 0), 60);
	CHECK(r.out.sp[0] >= 3980 && r.out.sp[1] + r.out.sp[2] >= 1900, "S1 at its charger, the rest to Stack2, sp %d %d %d",
		  r.out.sp[0], r.out.sp[1], r.out.sp[2]);

	pl.pv = 2500;
	pl.soc[0] = 100;                                             /* full: stays in CHARGE for the cell balancing */
	simulate(&r, &pl, local_time(2026, 1, 15, 12, 0), 60);
	print_state("Stack1 at 100 %, force_full S1", &r);
	CHECK(strstr(r.out.why[0], "CHARGE") == r.out.why[0] && r.out.sp[0] > 0, "100 %%: CHARGE, not FULL, got %d %s",
		  r.out.sp[0], r.out.why[0]);
	CHECK(!r.out.ff_done, "not done after 5 min at 100 %%");
	simulate(&r, &pl, local_time(2026, 1, 15, 12, 0), 60 * 12 - 2);   /* 59.8 min */
	CHECK(!r.out.ff_done, "not done before FORCE_FULL_HOLD_MIN 60 min");
	simulate(&r, &pl, local_time(2026, 1, 15, 12, 0), 60 * 12 + 1);
	CHECK(r.out.ff_done, "done after 60 min at 100 %%");

	pl.pv = 0;                                                   /* evening, no surplus: HOLD, Stack2 covers the house */
	pl.house = 1500;
	pl.soc[0] = 97;
	simulate(&r, &pl, local_time(2026, 1, 15, 19, 0), 60);
	print_state("evening 1.5 kW, force_full S1", &r);
	CHECK(r.out.sp[0] == 0 && strstr(r.out.why[0], "HOLD(97.0%)"), "Stack1 HOLDs, got %d %s", r.out.sp[0], r.out.why[0]);
	CHECK(abs(r.out.sp[1] + r.out.sp[2] + 1530) <= 40, "Stack2 covers the house, L2+L3 %d", r.out.sp[1] + r.out.sp[2]);
	CHECK(!r.out.ff_done, "below 100 %%: not done");

	pl.force_full = 2;                                           /* S2: the other way round */
	simulate(&r, &pl, local_time(2026, 1, 15, 19, 0), 60);
	CHECK(r.out.sp[1] == 0 && r.out.sp[2] == 0 && strstr(r.out.why[1], "HOLD("), "S2: Stack2 HOLDs, got %d %d %s",
		  r.out.sp[1], r.out.sp[2], r.out.why[1]);
	CHECK(abs(r.out.sp[0] + 1530) <= 40, "Stack1 covers the house, L1 %d", r.out.sp[0]);

	pl.force_full = 1;                                           /* LOW / FORCE keep their priority */
	pl.soc[0] = 2;
	simulate(&r, &pl, local_time(2026, 1, 15, 19, 0), 30);
	CHECK(r.out.sp[0] == 500 && strstr(r.out.why[0], "FORCE("), "force charge first, got %d %s", r.out.sp[0],
		  r.out.why[0]);
}

/* 0.67-c (user 2026-10-27): force_full_from_pv also ends once the cell spread at the top stayed small for 60 s, and
   at the latest FORCE_FULL_MAX_MIN after the command, also below 100 % */
static void test_force_full_end(void)
{
	struct run r;
	struct plant pl = BASE(.pv = 2500, .house = 500, .wp = 0, .soc = {94, 60}, .force_full = 1,
						   .cell_min = {3.397, 3.30}, .cell_max = {3.489, 3.31});
	simulate(&r, &pl, local_time(2026, 1, 15, 12, 0), 60);
	CHECK(r.out.ff_done == 0, "spread 92 mV at the top: not done, got %d", r.out.ff_done);

	pl.cell_min[0] = 3.33;                                       /* at rest after the charge: small spread, low cells */
	pl.cell_max[0] = 3.335;
	simulate(&r, &pl, local_time(2026, 1, 15, 12, 0), 60);
	CHECK(r.out.ff_done == 0, "spread 5 mV at rest (highest cell below FORCE_FULL_TOP_V): not done, got %d",
		  r.out.ff_done);

	pl.cell_min[0] = 3.47;                                       /* the low cell caught up at the top */
	pl.cell_max[0] = 3.495;
	simulate(&r, &pl, local_time(2026, 1, 15, 12, 0), FF_SPREAD_STABLE_S / CYCLE_SECONDS);
	CHECK(r.out.ff_done == 0, "spread 25 mV not yet 60 s: not done, got %d", r.out.ff_done);
	simulate(&r, &pl, local_time(2026, 1, 15, 12, 0), FF_SPREAD_STABLE_S / CYCLE_SECONDS + 1);
	CHECK(r.out.ff_done == BM_FF_SPREAD, "spread 25 mV for 60 s: done (spread), got %d", r.out.ff_done);

	pl.cell_max[0] = 0;                                          /* no highest cell from the BMS: never by spread */
	simulate(&r, &pl, local_time(2026, 1, 15, 12, 0), 60);
	CHECK(r.out.ff_done == 0, "no MaxCellVoltage: not done, got %d", r.out.ff_done);

	pl.cell_min[0] = 3.397;                                      /* never full, spread stays: the timeout ends it */
	pl.cell_max[0] = 3.489;
	simulate(&r, &pl, local_time(2026, 1, 15, 12, 0), 240 * 12);
	CHECK(r.out.ff_done == 0, "below FORCE_FULL_MAX_MIN 240 min: not done, got %d", r.out.ff_done);
	simulate(&r, &pl, local_time(2026, 1, 15, 12, 0), 240 * 12 + 1);
	CHECK(r.out.ff_done == BM_FF_MAX, "after 240 min: done (max), got %d", r.out.ff_done);

	pl.soc[0] = 100;                                             /* the hold time comes first */
	simulate(&r, &pl, local_time(2026, 1, 15, 12, 0), 60 * 12 + 1);
	CHECK(r.out.ff_done == BM_FF_HOLD, "60 min at 100 %%: done (hold), got %d", r.out.ff_done);

	pl.force_full = 0;                                           /* off: nothing ends */
	simulate(&r, &pl, local_time(2026, 1, 15, 12, 0), 240 * 12 + 1);
	CHECK(r.out.ff_done == 0 && r.st.ff_since == 0, "off: not done, got %d", r.out.ff_done);
}

static void test_discharge_protection(void)
{
	struct run r;
	struct plant pl = BASE(.house = 500, .soc = {50, 4.0});
	simulate(&r, &pl, local_time(2026, 1, 15, 22, 0), 10);
	print_state("Stack2 at 4 %", &r);
	CHECK(r.out.sp[1] == 0 && r.out.sp[2] == 0, "no discharge on L2/L3, got %d %d", r.out.sp[1], r.out.sp[2]);
	CHECK(strstr(r.out.why[1], "PROT(") != NULL, "rule DISCHARGE_PROTECTION, got %s", r.out.why[1]);
}

static void test_stale(void)
{
	struct run r;
	struct plant pl = BASE(.house = 500, .stale_inv = 1);
	simulate(&r, &pl, local_time(2026, 1, 15, 22, 0), 5);
	CHECK(sum_sp(&r) == 0 && rule_has(&r, "STALE"), "stale inverter data: all 0, got %d %s", sum_sp(&r), r.out.why[0]);
}

/* 0.59-c: em0/power stale - the PCC still contains the heat pump, so it is covered (0.19..0.58-c: WP_CAP 1 kW) */
static void test_wp_stale_still_covered(void)
{
	struct run r;
	struct plant pl = BASE(.house = 500, .wp = 4000, .r290_hz = 60, .wp_fresh = 0);
	simulate(&r, &pl, local_time(2026, 1, 15, 22, 0), 40);
	print_state("winter, em0/power stale, R290 running", &r);
	CHECK(abs(sum_sp(&r) + 4530) <= 60 && fabs(r.pcc) <= 80, "house + WP from the PCC, sp sum %d pcc %.0f", sum_sp(&r),
		  r.pcc);
	CHECK(!rule_has(&r, "WPCAP"), "no WP_CAP");
}

static void test_balance(void)
{
	struct run r;
	struct plant pl = BASE(.house = 1500, .soc = {60, 50});
	simulate(&r, &pl, local_time(2026, 1, 15, 22, 0), 30);
	print_state("Stack1 10 % ahead", &r);
	CHECK(r.st.lead == 0, "Stack1 leads, lead %d", r.st.lead);
	CHECK(r.out.sp[0] < 0 && r.out.sp[1] == 0 && r.out.sp[2] == 0, "only L1 discharges, got %d %d %d",
		  r.out.sp[0], r.out.sp[1], r.out.sp[2]);
}

/* 0.49-c peak window as a forecast export cap. Clear-sky model 21 June (KT_MONTH 0.909): 09:00 total 9165 W of it
   Sofar 1605 W, 12:30 total 23012 W of it Sofar 11003 W; with OWM kt 1.0 the forecast peak is ~25 kW */
static void test_peak_window_cap(void)
{
	struct run r;
	struct plant pl = BASE(.house = 500, .soc = {50, 50}, .pv = 9165, .pv_sofar = 1605, .fc_kt = 1.0);
	simulate(&r, &pl, local_time(2026, 6, 21, 9, 0), 10);
	print_state("21 June 09:00, clear forecast", &r);
	CHECK(r.out.ls.cap && rule_has(&r, "LADESPERRE"), "export above the cap expected later: wait, why %s", r.out.why[0]);
	CHECK(sum_sp(&r) == 0 && r.out.ls.peak_h >= 11 && r.out.ls.win_end_h >= r.out.ls.peak_h,
		  "no charging below the cap, peak %d h, window to %d h", r.out.ls.peak_h, r.out.ls.win_end_h);

	pl.pv = 23012 + 2500;                                        /* a real noon a bit above the model */
	pl.pv_sofar = 11003;
	simulate(&r, &pl, local_time(2026, 6, 21, 12, 30), 40);
	print_state("21 June 12:30, PV 25.5 kW", &r);
	CHECK(r.out.ls.cap && rule_has(&r, "CAP"), "charging above the cap, why %s", r.out.why[0]);
	CHECK(fabs(r.pcc - 18000) <= 250, "PCC held at the cap 18 kW, pcc %.0f", r.pcc);
	CHECK(abs(sum_sp(&r) - (25512 - 500 - 18000)) <= 250, "stacks take the export above the cap, sp sum %d",
		  sum_sp(&r));

	pl = BASE(.house = 500, .soc = {50, 50}, .pv = 0.33 * 23012, .pv_sofar = 0.33 * 11003, .fc_kt = 0.3);
	simulate(&r, &pl, local_time(2026, 6, 21, 12, 30), 40);
	print_state("21 June 12:30, cloudy forecast", &r);
	CHECK(!r.out.ls.cap && !rule_has(&r, "CAP") && fabs(r.pcc) <= 100, "no cap: every watt charges, pcc %.0f", r.pcc);

	pl = BASE(.house = 500, .soc = {50, 50}, .pv = 6000, .fc_kt = 1.0);
	simulate(&r, &pl, local_time(2026, 1, 21, 11, 0), 20);
	print_state("21 Jan 11:00, PV 6 kW", &r);
	CHECK(!r.out.ls.cap && sum_sp(&r) > 0, "winter: no cap, sp sum %d", sum_sp(&r));
}

static void test_peak_window_off(void)
{
	struct run r;
	struct plant pl = BASE(.house = 500, .soc = {50, 50}, .pv = 9165, .pv_sofar = 1605, .fc_kt = 1.0);
	r_override_cap_off = 1;
	simulate(&r, &pl, local_time(2026, 6, 21, 9, 0), 20);
	r_override_cap_off = 0;
	CHECK(!r.out.ls.cap && sum_sp(&r) > 0, "EXPORT_CAP 0: no cap, sp sum %d", sum_sp(&r));
}

/* 0.59-c: the season is display only (state.json), the setpoints are the same in every season */
static void test_season_measured_overrides_months(void)
{
	struct run r;
	struct plant pl = BASE(.house = 500, .wp = 1200, .r290_hz = 40, .season = BM_SUMMER, .season_age_h = 1);
	simulate(&r, &pl, local_time(2026, 10, 20, 23, 0), 60);      /* October, but measured: still summer */
	print_state("Oct night, season.py says summer", &r);
	CHECK(r.out.season == BM_SUMMER && r.out.season_measured, "measured summer shown");
	int sp_summer = sum_sp(&r);
	CHECK(abs(sp_summer + 1000) <= 3, "0.61-c: October = winter half-year by the month, WP rule 1000 W, sp sum %d",
		  sp_summer);

	pl.season = BM_WINTER;
	simulate(&r, &pl, local_time(2026, 10, 20, 23, 0), 60);
	CHECK(r.out.season == BM_WINTER && r.out.season_measured, "measured winter shown");
	CHECK(sum_sp(&r) == sp_summer, "same setpoints as in summer, %d vs %d", sum_sp(&r), sp_summer);
}

static void test_season_stale_falls_back_to_months(void)
{
	struct run r;
	struct plant pl = BASE(.house = 500, .wp = 1200, .r290_hz = 40, .season = BM_SUMMER, .season_age_h = 49);
	simulate(&r, &pl, local_time(2026, 11, 20, 23, 0), 40);      /* summer message 49 h old: November = winter */
	print_state("Nov night, season message 49 h old", &r);
	CHECK(r.out.season == BM_WINTER && !r.out.season_measured, "stale message: month rule (winter)");
	CHECK(abs(sum_sp(&r) + 1000) <= 3, "0.61-c: winter half-year, WP rule 1000 W, sp sum %d", sum_sp(&r));
}

/* 0.59-c: no transition cap (0.22..0.58-c: the stacks gave the heat pump at most 1900 W, the grid the rest) */
static void test_transition_wp_covered(void)
{
	struct run r;
	struct plant pl = BASE(.pv = 200, .house = 200, .wp = 2500, .r290_hz = 50, .season = BM_TRANSITION, .season_age_h = 1);
	simulate(&r, &pl, local_time(2026, 9, 9, 14, 0), 60);       /* 0.61-c: September, outside the winter half-year */
	print_state("transition, WP 2.5 kW, house 0 net", &r);
	CHECK(fabs(r.pcc) <= 60 && abs(sum_sp(&r) + 2500) <= 60, "whole WP from the batteries, pcc %.0f sp %d", r.pcc,
		  sum_sp(&r));
	CHECK(!rule_has(&r, "T1900"), "no transition cap");
}

static void test_param_override(void)
{
	struct run r;
	struct bm_cfg c;
	bm_cfg_default(&c);
	CHECK(c.w_max == 10000 && c.soc_min == 5, "defaults from BM_PARAMS");
	CHECK(strcmp(BM_PARAMS[0].name, "W_MAX") == 0, "W_MAX is parameter 0");
	CHECK(bm_param_set(&c, 0, 50000) == -1 && c.w_max == 10000, "out of range rejected, default kept");
	CHECK(bm_param_set(&c, 0, 3000) == 0 && c.w_max == 3000, "in range accepted");

	struct plant pl = BASE(.house = 6000, .season = BM_SUMMER, .season_age_h = 1);
	r_override_w_max = 3000;
	simulate(&r, &pl, local_time(2026, 7, 15, 23, 0), 40);
	r_override_w_max = 0;
	print_state("W_MAX=3000, house 6 kW", &r);
	CHECK(sum_sp(&r) >= -3000 && sum_sp(&r) <= -2990, "W_MAX override 3000 W, sp sum %d", sum_sp(&r));
}

/* 0.59-c: the forecast target SoC (batmonitor/forecast) no longer changes the setpoints; its kt slots still feed D4
   and full_at */
/* 0.61-c (user 2026-10-25): WP_SOC_MIN gone. Winter half-year with the heat pump running: the stacks give 0 W while the
   Sofar Bat1 discharges, else at most WP_WINTER_W (1000 W); summer / WP off unchanged (PCC near 0) */
static void test_winter_wp_rule(void)
{
	struct run r;
	struct plant pl = BASE(.house = 500, .wp = 3000, .r290_hz = 50, .sofar_dis = 1, .sofar_chg = 1, .bat1_soc = 50);
	simulate(&r, &pl, local_time(2026, 1, 15, 22, 0), 60);
	print_state("winter night, WP 3 kW, Sofar Bat1 50 %", &r);
	CHECK(sum_sp(&r) == 0 && r.out.wp_max_w == 0, "stacks give nothing while the Sofar discharges, sp sum %d", sum_sp(&r));
	CHECK(rule_has(&r, "SRC:B1") && rule_has(&r, "WPMAX(0)"), "rule SRC:B1 WPMAX(0)");
	CHECK(r.bat1 <= -2400, "the Sofar Bat1 delivers, bat1 %.0f", r.bat1);

	pl = BASE(.house = 500, .wp = 3000, .r290_hz = 50, .bat1_soc = 4);
	simulate(&r, &pl, local_time(2026, 1, 15, 22, 0), 60);
	print_state("winter night, WP 3 kW, Sofar Bat1 empty", &r);
	CHECK(abs(sum_sp(&r) + 1000) <= 3 && r.out.wp_max_w == 1000, "at most 1000 W once the Sofar is empty, sp sum %d",
		  sum_sp(&r));
	CHECK(rule_has(&r, "SRC:WPG") && rule_has(&r, "WPMAX(1000)"), "rule SRC:WPG WPMAX(1000)");
	CHECK(fabs(r.pcc + 2500) <= 60, "the rest from the grid, pcc %.0f", r.pcc);

	pl = BASE(.house = 500, .wp = 3000, .r290_hz = 50, .bat1_soc = 50);   /* Sofar has charge but does not discharge */
	simulate(&r, &pl, local_time(2026, 1, 15, 22, 0), 60);
	CHECK(abs(sum_sp(&r) + 1000) <= 3 && rule_has(&r, "WPMAX(1000)"), "Sofar idle: 1000 W, sp sum %d", sum_sp(&r));

	pl = BASE(.house = 200, .wp = 600, .r290_hz = 30, .bat1_soc = 4);
	simulate(&r, &pl, local_time(2026, 3, 10, 2, 0), 60);       /* below the cap: PCC near 0 as before */
	CHECK(abs(sum_sp(&r) + 800) <= 30 && fabs(r.pcc) <= 60, "below 1000 W the stacks cover all, sp sum %d", sum_sp(&r));

	pl = BASE(.pv = 1000, .house = 800, .wp = 3000, .r290_hz = 50, .bat1_soc = 4);
	simulate(&r, &pl, local_time(2026, 12, 21, 12, 0), 60);     /* by day too */
	CHECK(abs(sum_sp(&r) + 1000) <= 3 && rule_has(&r, "SRC:DAY") && rule_has(&r, "WPMAX(1000)"),
		  "winter day, Sofar empty: 1000 W, sp sum %d", sum_sp(&r));

	pl = BASE(.house = 500, .wp = 3000, .r290_hz = 50, .bat1_soc = 4);
	simulate(&r, &pl, local_time(2026, 7, 15, 23, 0), 60);      /* summer half-year: no rule */
	CHECK(r.out.wp_max_w == -1 && !rule_has(&r, "WPMAX") && rule_has(&r, "SRC:STK"), "summer: night floor, no WP rule");
	CHECK(abs(sum_sp(&r) + 3530) <= 60 && fabs(r.pcc) <= 80, "summer: house + WP + trickle, sp sum %d", sum_sp(&r));

	pl = BASE(.house = 500, .sofar_dis = 1, .sofar_chg = 1, .bat1_soc = 50);
	simulate(&r, &pl, local_time(2026, 1, 15, 22, 0), 60);      /* winter, heat pump off: night floor */
	CHECK(r.out.wp_max_w == -1 && rule_has(&r, "SRC:STK") && abs(sum_sp(&r) + 530) <= 30,
		  "winter, WP off: night floor, sp sum %d", sum_sp(&r));

	struct plant pc = BASE(.pv = 9000, .house = 800, .wp = 4000, .r290_hz = 60, .soc = {15, 15});
	simulate(&r, &pc, local_time(2026, 12, 21, 12, 0), 60);       /* by day with PV: charging on the PCC */
	print_state("Dec noon, SoC 15, PV 9 kW, WP 4 kW", &r);
	CHECK(abs(sum_sp(&r) - 4200) <= 80 && fabs(r.pcc) <= 80, "charging unchanged, sp sum %d pcc %.0f", sum_sp(&r), r.pcc);
}

static void test_forecast_target_ignored(void)
{
	struct run r;
	struct plant pl = BASE(.house = 500, .wp = 4000, .r290_hz = 60, .season = BM_WINTER, .season_age_h = 1,
						   .soc = {50, 50});
	simulate(&r, &pl, local_time(2026, 1, 15, 22, 0), 60);
	int sp0 = sum_sp(&r);
	pl.fc_target = 20;                                           /* good forecast */
	simulate(&r, &pl, local_time(2026, 1, 15, 22, 0), 60);
	CHECK(sum_sp(&r) == sp0, "good forecast: same setpoints, %d vs %d", sum_sp(&r), sp0);
	pl.fc_target = 100;                                          /* bad forecast */
	simulate(&r, &pl, local_time(2026, 1, 15, 22, 0), 60);
	CHECK(sum_sp(&r) == sp0, "bad forecast: same setpoints, %d vs %d", sum_sp(&r), sp0);
}

/* 0.46-c / 0.50-c: PCC export above 20 kW for DO4_GRACE_S (120 s) -> one DO4 pulse "curtail WR2", at once above
   DO4_HARD_W (23 kW); then DO4_LOCKOUT 5 min */
static void test_do4_pulse(void)
{
	struct run r;
	struct plant pl = BASE(.pv = 32000, .house = 500, .soc = {100, 100});   /* stacks full: export ~31.5 kW */
	simulate(&r, &pl, local_time(2026, 6, 21, 12, 0), 120);              /* 10 min */
	print_state("21 June 12:00, PV 32 kW, stacks full", &r);
	CHECK(r.pcc > 23000 && r.do4_pulses == 2, "above 23 kW: a pulse at once, one after 5 min, got %d", r.do4_pulses);
	pl.pv = 22000;                                               /* export 21.5 kW: grace first */
	simulate(&r, &pl, local_time(2026, 6, 21, 12, 0), 23);       /* 110 s */
	CHECK(r.do4_pulses == 0, "21.5 kW for 110 s: boost / air conditioning first, no pulse, got %d", r.do4_pulses);
	simulate(&r, &pl, local_time(2026, 6, 21, 12, 0), 26);       /* 125 s */
	CHECK(r.do4_pulses == 1, "21.5 kW for 2 min: pulse, got %d", r.do4_pulses);
	pl.pv = 19000;
	simulate(&r, &pl, local_time(2026, 6, 21, 12, 0), 120);
	CHECK(r.do4_pulses == 0, "export below 20 kW: no pulse, got %d", r.do4_pulses);
	pl.pv = 32000;
	pl.stale_inv = 1;
	simulate(&r, &pl, local_time(2026, 6, 21, 12, 0), 20);
	CHECK(r.do4_pulses == 0, "stale PCC: no pulse, got %d", r.do4_pulses);
}

/* 0.51-c: grid offline and PV on AC-out1 (FoxESS) above 5 kW on one MultiPlus for 10 s -> DO4 at once (factor 1.0
   rule); with the grid there the feed passes to the grid (bypass), no pulse */
static void test_do4_acout(void)
{
	struct run r;
	struct plant pl = BASE(.house = 500, .soc = {60, 60}, .acout_feed = {0, 5600, 0});
	simulate(&r, &pl, local_time(2026, 6, 21, 12, 0), 60);
	CHECK(r.do4_pulses == 0, "grid there: 5.6 kW feed passes to the grid, no pulse, got %d", r.do4_pulses);
	pl.grid_off = 1;
	simulate(&r, &pl, local_time(2026, 6, 21, 12, 0), 2);         /* cycles at 0 and 5 s: not yet */
	CHECK(r.do4_pulses == 0, "5.6 kW feed for 5 s: no pulse yet, got %d", r.do4_pulses);
	simulate(&r, &pl, local_time(2026, 6, 21, 12, 0), 3);         /* ... and 10 s */
	CHECK(r.do4_pulses == 1 && r.out.acout_feed_w > 5000, "5.6 kW feed into L2 for 10 s: pulse, got %d (feed %.0f W)",
		  r.do4_pulses, r.out.acout_feed_w);
	pl.acout_feed[1] = 4900;
	simulate(&r, &pl, local_time(2026, 6, 21, 12, 0), 60);
	CHECK(r.do4_pulses == 0, "4.85 kW feed: within the 5000 VA, no pulse, got %d", r.do4_pulses);
}

static void test_bat1_first(void)
{
	struct run r;
	struct plant pl = BASE(.house = 500, .wp = 1600, .r290_hz = 60, .season = BM_TRANSITION, .season_age_h = 1,
						   .sofar_dis = 1, .sofar_chg = 1, .bat1_soc = 36, .soc = {50, 50});
	simulate(&r, &pl, local_time(2026, 10, 10, 2, 0), 60);       /* Sofar 36 %: it covers house + WP alone */
	print_state("night, Sofar 36 %, WP 1.6 kW", &r);
	CHECK(r.out.bat1_first && rule_has(&r, "SRC:B1 "), "Sofar first flagged");
	CHECK(sum_sp(&r) >= -30, "stacks idle, sp sum %d", sum_sp(&r));
	CHECK(fabs(r.bat1 + 2100) <= 60 && fabs(r.pcc) <= 60, "Bat1 2.1 kW, no import, bat1 %.0f pcc %.0f", r.bat1, r.pcc);

	pl.house = 1500;                                             /* 4.5 kW > Sofar 2.5 kW: 0.61-c (winter half-year) the */
	pl.wp = 3000;                                                /* stacks give nothing while the Sofar has charge */
	simulate(&r, &pl, local_time(2026, 10, 10, 2, 0), 60);
	print_state("night, Sofar 36 %, house 1.5 + WP 3 kW", &r);
	CHECK(fabs(r.bat1 + 2500) <= 60, "Sofar at its 2.5 kW limit, bat1 %.0f", r.bat1);
	CHECK(sum_sp(&r) == 0 && fabs(r.pcc + 2000) <= 80 && rule_has(&r, "WPMAX(0)"),
		  "0.61-c: stacks 0 W, 2 kW from the grid, sp sum %d pcc %.0f", sum_sp(&r), r.pcc);

	pl.house = 500;
	pl.wp = 1600;
	pl.bat1_soc = 5;                                             /* Sofar empty (DOD 95 %): it stops discharging */
	pl.sofar_dis = 0;
	simulate(&r, &pl, local_time(2026, 10, 10, 2, 0), 60);
	print_state("night, Sofar 5 %, WP 1.6 kW", &r);
	CHECK(!r.out.bat1_first && abs(sum_sp(&r) + 1000) <= 3 && rule_has(&r, "SRC:WPG"),
		  "0.61-c: stacks WP_WINTER_W 1000 W, sp sum %d", sum_sp(&r));
	pl.sofar_dis = 1;

	pl.bat1_soc = 36;                                            /* heat pump off: the normal rule (stacks take over the Sofar) */
	pl.wp = 0;
	pl.r290_hz = 0;
	simulate(&r, &pl, local_time(2026, 10, 10, 2, 0), 60);
	print_state("night, Sofar 36 %, WP off", &r);
	CHECK(!r.out.bat1_first && abs(sum_sp(&r) + 530) <= 30, "stacks house + trickle, sp sum %d", sum_sp(&r));

	pl.wp = 1600;
	pl.r290_hz = 60;
	pl.bat1_soc = 6;                                             /* inside the hysteresis: stays off */
	simulate(&r, &pl, local_time(2026, 10, 10, 2, 0), 10);
	CHECK(!r.out.bat1_first, "6 %% < 5 + 2: Sofar-first stays off");
}

static void test_pi_shadow_not_armed(void)
{
	struct run r;
	struct plant pl = BASE(.pv = 5000, .house = 800);
	simulate(&r, &pl, local_time(2026, 3, 10, 12, 0), 20);
	CHECK(isfinite(r.out.pi.u), "PI output finite");
	CHECK(!rule_has(&r, "PI("), "PI not armed: soyo rules, got %s", r.out.why[0]);
}

/* full_at forecast (0.28-c) */
static struct bm_fc_in fc_base(time_t t, double soc1, double soc2, double p1, double p2)
{
	struct bm_fc_in fi;
	memset(&fi, 0, sizeof(fi));
	fi.t = t;
	fi.lead = -1;
	fi.soc_ok[0] = fi.soc_ok[1] = fi.has_avg[0] = fi.has_avg[1] = 1;
	fi.soc[0] = soc1;
	fi.soc[1] = soc2;
	fi.avg[0] = p1;
	fi.avg[1] = p2;
	return fi;
}

static void fc_slots(struct bm_fc_in *fi, int y, int mo, int d, double kt, double corr)
{
	fi->corr = corr;
	fi->n_slots = 0;
	for (int h = 8; h <= 17; h += 3) {
		fi->slot_t[fi->n_slots] = local_time(y, mo, d, h, 0);
		fi->slot_kt[fi->n_slots++] = kt;
	}
}

static void print_fc(const char *name, const struct bm_fc_out *o)
{
	char a[2][8];
	for (int b = 0; b < 2; b++) {
		struct tm l;
		time_t t = o->full_at[b];
		strcpy(a[b], "-");
		if (t) {
			localtime_r(&t, &l);
			strftime(a[b], sizeof(a[b]), "%H:%M", &l);
		}
	}
	printf("  %-44s full %s %s  soc_sunset %5.1f %5.1f  anchor %.2f kt %.2f pv %5.0f W rest %.1f kWh\n", name, a[0], a[1],
		   o->soc_sunset[0], o->soc_sunset[1], o->anchor, o->kt_now, o->pv_now_w, o->rest_kwh);
}

static void test_fc_sunset_limits(void)
{
	struct bm_cfg cfg;
	struct bm_fc_out o;
	bm_cfg_default(&cfg);
	/* 9 Oct 16:00, 2 kW per stack, SoC 40: the linear ETA said ~19:00; the sun sets before that */
	time_t t = local_time(2026, 10, 9, 16, 0);
	struct bm_fc_in fi = fc_base(t, 40, 40, 2000, 2000);
	fc_slots(&fi, 2026, 10, 9, 0.65, 0.69);
	bm_full_forecast(&cfg, &fi, &o);
	print_fc("Oct 16:00, SoC 40, 2 kW each", &o);
	CHECK(!o.full_at[0] && !o.full_at[1], "not full before sunset");
	CHECK(o.soc_sunset[0] > 40 && o.soc_sunset[0] < 60, "Stack1 gains a bit until sunset, %.1f", o.soc_sunset[0]);
	struct bm_fc_in night = fc_base(local_time(2026, 10, 9, 22, 0), 40, 40, 500, 500);
	bm_full_forecast(&cfg, &night, &o);
	CHECK(o.soc_sunset[0] < 0 && !o.full_at[0], "no weather forecast at night (linear ETA in batmonitor.c)");
}

static void test_fc_clouds_later(void)
{
	struct bm_cfg cfg;
	struct bm_fc_out sun, cloud;
	bm_cfg_default(&cfg);
	time_t t = local_time(2026, 10, 9, 10, 30);
	struct bm_fc_in fi = fc_base(t, 40, 40, 3500, 1500);
	fc_slots(&fi, 2026, 10, 9, 0.70, 0.69);
	bm_full_forecast(&cfg, &fi, &sun);
	print_fc("Oct 10:30, kt 0.70", &sun);
	fc_slots(&fi, 2026, 10, 9, 0.30, 0.69);
	bm_full_forecast(&cfg, &fi, &cloud);
	print_fc("Oct 10:30, kt 0.30", &cloud);
	CHECK(sun.full_at[0] > t && sun.full_at[0] < local_time(2026, 10, 9, 15, 0), "sunny: Stack1 full early afternoon");
	CHECK(!cloud.full_at[1] || cloud.full_at[1] > sun.full_at[1], "clouds: Stack2 later or not at all");
	CHECK(cloud.soc_sunset[1] <= sun.soc_sunset[1], "clouds: lower SoC at sunset");
	CHECK(sun.weather == 1, "OWM slots used");
}

static void test_fc_anchor(void)
{
	struct bm_cfg cfg;
	struct bm_fc_out o, a;
	bm_cfg_default(&cfg);
	time_t t = local_time(2026, 10, 9, 11, 0);
	struct bm_fc_in fi = fc_base(t, 40, 40, 3000, 1500);
	fc_slots(&fi, 2026, 10, 9, 0.70, 0.69);
	bm_full_forecast(&cfg, &fi, &o);
	fi.have_pv = 1;
	fi.pv_sofar = 0.3 * o.pv_now_w * 0.5;        /* Sofar is ~half of the model; measured far below the forecast */
	bm_full_forecast(&cfg, &fi, &a);
	print_fc("Oct 11:00, measured PV well below forecast", &a);
	CHECK(a.anchor < 0.8, "anchor follows the measurement, %.2f", a.anchor);
	CHECK(a.rest_kwh < o.rest_kwh, "less PV expected, %.1f < %.1f kWh", a.rest_kwh, o.rest_kwh);
	struct bm_fc_in m = fc_base(t, 40, 40, 3000, 1500);       /* no slots: monthly kt */
	bm_full_forecast(&cfg, &m, &o);
	CHECK(o.weather == 0 && o.soc_sunset[0] >= 0, "without forecast.py the monthly kt still gives a forecast");
}

static void test_fc_full_stack_moves_power(void)
{
	struct bm_cfg cfg;
	struct bm_fc_out o;
	bm_cfg_default(&cfg);
	time_t t = local_time(2026, 6, 20, 11, 0);
	struct bm_fc_in fi = fc_base(t, 99, 60, 3800, 3800);
	fc_slots(&fi, 2026, 6, 20, 0.90, 1.0);
	bm_full_forecast(&cfg, &fi, &o);
	print_fc("June 11:00, Stack1 99 %", &o);
	CHECK(o.full_at[0] && o.full_at[0] - t <= 900, "Stack1 full within minutes");
	CHECK(o.full_at[1] && o.full_at[1] < local_time(2026, 6, 20, 16, 0), "Stack2 gets Stack1's share, full before 16:00");
}

static void test_fc_paused_stack_catches_up(void)
{
	struct bm_cfg cfg;
	struct bm_fc_out o;
	bm_cfg_default(&cfg);
	/* 9 Oct 11:10 (real): Stack1 53 % paused as the lead, Stack2 51.3 % alone at 3.5 kW; reality: both full at
	   13:17 / 13:19. 0.31-c said 14:40 for Stack1 (waited until Stack2 was full) */
	time_t t = local_time(2026, 10, 9, 11, 10);
	struct bm_fc_in fi = fc_base(t, 53, 51.3, 0, 3483);
	fi.lead = 0;
	fc_slots(&fi, 2026, 10, 9, 0.62, 0.69);
	bm_full_forecast(&cfg, &fi, &o);
	print_fc("Oct 11:10, Stack1 paused as lead", &o);
	/* the absolute time depends on the 3.5 kW of this cloud minute taken as the 5 min mean; the point is the lead:
	   Stack1 must not wait until Stack2 is full (0.31-c: 14:50 vs 13:10, 100 min apart) */
	CHECK(o.full_at[0] && o.full_at[1] && labs((long)(o.full_at[0] - o.full_at[1])) <= 15 * 60,
		  "both stacks full within 15 min of each other");
}

static void test_fc_charger_limit(void)
{
	struct bm_cfg cfg;
	struct bm_fc_out o;
	bm_cfg_default(&cfg);
	/* June noon, 19 kW PV: 3 x 3.6 kW DC at most (measured), Stack2 (2 units) from 20 % needs >= 80 % x 15.36 kWh
	   / 7.2 kW = 1.71 h, Stack1 (1 unit) 3.41 h */
	time_t t = local_time(2026, 6, 20, 11, 0);
	struct bm_fc_in fi = fc_base(t, 20, 20, 3900, 7800);
	fi.volt_ok[0] = fi.volt_ok[1] = 1;
	fi.volt[0] = fi.volt[1] = 56.0;
	fc_slots(&fi, 2026, 6, 20, 0.95, 1.0);
	bm_full_forecast(&cfg, &fi, &o);
	print_fc("June 11:00, SoC 20, chargers at 3.6 kW DC", &o);
	CHECK(o.full_at[1] - t >= (time_t)(0.8 * 15360 / 7200 * 3600) - 300, "Stack2 not faster than 2 x 3.6 kW DC");
	CHECK(o.full_at[0] - t >= (time_t)(0.8 * 15360 / 3600 * 3600) - 300, "Stack1 not faster than 3.6 kW DC");
	CHECK(o.full_at[1] - t <= (time_t)(0.8 * 15360 / 7200 * 3600) + 600, "Stack2 at the 3.6 kW limit, not slower");
}

int main(void)
{
	setenv("TZ", "CET-1CEST,M3.5.0,M10.5.0/3", 1);
	tzset();
	test_winter_wp_from_battery();
	test_winter_wp_5kw();
	test_summer_wp_from_battery();
	test_summer_wp_4kw_from_battery();
	test_w_max_and_phase_cap();
	test_bank_split();
	test_single_unit();
	test_bms_current_limits();
	test_winter_night_floor();
	test_wp_running_night_floor();
	test_day_surplus_to_zero();
	test_real_charge_ceiling();
	test_winter_charge_on_pcc();
	test_day_sofar_covers_house();
	test_day_sofar_saturated();
	test_force_charge();
	test_force_full_from_pv();
	test_force_full_end();
	test_discharge_protection();
	test_stale();
	test_wp_stale_still_covered();
	test_balance();
	test_peak_window_cap();
	test_peak_window_off();
	test_season_measured_overrides_months();
	test_season_stale_falls_back_to_months();
	test_transition_wp_covered();
	test_param_override();
	test_forecast_target_ignored();
	test_winter_wp_rule();
	test_do4_pulse();
	test_do4_acout();
	test_bat1_first();
	test_pi_shadow_not_armed();
	test_fc_sunset_limits();
	test_fc_clouds_later();
	test_fc_anchor();
	test_fc_full_stack_moves_power();
	test_fc_charger_limit();
	test_fc_paused_stack_catches_up();
	printf("%d checks, %d failed\n", checks, fails);
	return fails ? 1 : 0;
}
