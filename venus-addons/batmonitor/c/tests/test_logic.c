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
	double aussen;
};

static double r_override_w_max;     /* > 0: W_MAX for the next simulate() (parameter test) */

struct run {
	struct bm_cfg cfg;
	struct bm_state st;
	struct bm_out out;
	struct bm_in in;
	double ac[NPH], pcc, bat1;
	double pcc_hist[1000];
	int n;
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
		in->pv = pl->pv;
		in->inv_time = pl->stale_inv ? 1.0 : now;
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
		}
		for (int b = 0; b < NBANK; b++) {
			in->bms_ok[b] = 1;
			in->soc[b] = pl->soc[b];
			in->power_ok[b] = 1;
			in->power[b] = 0;
			for (int i = 0; i < BM_BANKS[b].nph; i++)
				in->power[b] += r->ac[BM_BANKS[b].ph[i]];
		}
		bm_step(&r->cfg, in, &r->st, &r->out);
		for (int p = 0; p < NPH; p++)
			r->ac[p] = r->out.sp[p];     /* the Multis follow within one cycle */
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

static void test_winter_wp_from_grid(void)
{
	struct run r;
	struct plant pl = BASE(.house = 500, .wp = 4000, .r290_hz = 60);
	simulate(&r, &pl, local_time(2026, 1, 15, 22, 0), 60);       /* winter night, Sofar Bat1 empty */
	print_state("winter night, WP 4 kW, Sofar empty", &r);
	CHECK(abs(sum_sp(&r) + 500) <= 60, "Multis should cover the house 500 W only, sp sum %d", sum_sp(&r));
	CHECK(fabs(r.pcc + 4000) <= 80, "the heat pump should come from the grid, pcc %.0f", r.pcc);
	CHECK(rule_has(&r, "|Z2"), "rule should carry |Z2");
	CHECK(pcc_swing(&r, 20) < 100, "no oscillation, swing %.0f", pcc_swing(&r, 20));
}

static void test_winter_wp_5kw(void)
{
	struct run r;
	struct plant pl = BASE(.house = 400, .wp = 5000, .r290_hz = 70);
	simulate(&r, &pl, local_time(2026, 2, 1, 3, 0), 60);
	print_state("winter night, WP 5 kW, Sofar empty", &r);
	CHECK(abs(sum_sp(&r) + 400) <= 60, "Multis should cover the house 400 W only, sp sum %d", sum_sp(&r));
	CHECK(fabs(r.pcc + 5000) <= 80, "WP 5 kW from the grid, pcc %.0f", r.pcc);
}

static void test_summer_wp_from_battery(void)
{
	struct run r;
	struct plant pl = BASE(.house = 500, .wp = 1200, .r290_hz = 40);
	simulate(&r, &pl, local_time(2026, 7, 15, 23, 0), 60);       /* summer night: WP from the batteries */
	print_state("summer night, WP 1.2 kW, Sofar empty", &r);
	CHECK(abs(sum_sp(&r) + 1700) <= 60, "Multis should cover house + WP 1700 W, sp sum %d", sum_sp(&r));
	CHECK(fabs(r.pcc) <= 80, "no grid import in summer, pcc %.0f", r.pcc);
	CHECK(!rule_has(&r, "|Z2"), "no |Z2 in summer");
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
		CHECK(r.out.sp[p] >= -4000, "phase %d at most 4000 W, got %d", p, r.out.sp[p]);

	pl = BASE(.house = 6000, .soc = {60, 50}, .season = BM_SUMMER, .season_age_h = 1);
	simulate(&r, &pl, local_time(2026, 7, 15, 23, 0), 60);       /* Stack1 leads, but L1 alone cannot give 6 kW */
	print_state("Stack1 leads, house 6 kW", &r);
	CHECK(r.out.sp[0] == -4000 && rule_has(&r, "BALANCE_SPILL"), "L1 at 4000 W, the rest spills to L2/L3, got %d %d %d",
		  r.out.sp[0], r.out.sp[1], r.out.sp[2]);
	CHECK(fabs(r.pcc) <= 80, "house covered, pcc %.0f", r.pcc);
}

static void test_bank_split(void)
{
	struct run r;
	struct plant pl = BASE(.house = 2000, .season = BM_SUMMER, .season_age_h = 1);
	simulate(&r, &pl, local_time(2026, 7, 15, 23, 0), 60);       /* 0.26: L1 = L2 + L3 */
	print_state("bank split, house 2 kW", &r);
	CHECK(abs(r.out.sp[0] - (r.out.sp[1] + r.out.sp[2])) <= 3 && abs(r.out.sp[1] - r.out.sp[2]) <= 1,
		  "L1 = L2 + L3, got %d %d %d", r.out.sp[0], r.out.sp[1], r.out.sp[2]);
	CHECK(abs(sum_sp(&r) + 2000) <= 30, "house covered, sp sum %d", sum_sp(&r));

	pl = BASE(.house = 9000, .season = BM_SUMMER, .season_age_h = 1);
	simulate(&r, &pl, local_time(2026, 7, 15, 23, 0), 60);       /* L1 would need 4500 W: capped, rest to L2/L3 */
	print_state("bank split, house 9 kW", &r);
	CHECK(r.out.sp[0] == -4000, "L1 capped at 4000 W, got %d", r.out.sp[0]);
	CHECK(abs(r.out.sp[1] + 2500) <= 30 && abs(r.out.sp[2] + 2500) <= 30, "L2/L3 take the spill, got %d %d",
		  r.out.sp[1], r.out.sp[2]);
	CHECK(fabs(r.pcc) <= 80, "house covered, pcc %.0f", r.pcc);
}

static void test_winter_night_floor(void)
{
	struct run r;
	struct plant pl = BASE(.house = 500, .sofar_dis = 1, .sofar_chg = 1);
	simulate(&r, &pl, local_time(2026, 1, 15, 22, 0), 40);       /* no heat pump: the stacks take over the Sofar */
	print_state("winter night, no WP, Sofar covers", &r);
	CHECK(abs(sum_sp(&r) + 500) <= 30, "Multis cover the house 500 W (0.25: no fixed 936 W floor), sp sum %d", sum_sp(&r));
	CHECK(fabs(r.bat1) <= 30, "Sofar Bat1 neither charged nor discharged, bat1 %.0f", r.bat1);
	CHECK(fabs(r.pcc) <= 30, "PCC near 0, pcc %.0f", r.pcc);
	CHECK(pcc_swing(&r, 20) < 60, "no oscillation, swing %.0f", pcc_swing(&r, 20));

	pl = BASE(.house = 300, .sofar_dis = 1, .sofar_chg = 1);
	simulate(&r, &pl, local_time(2026, 10, 8, 20, 30), 40);      /* 2026-10-08: 936 W floor charged the Sofar +600 W */
	print_state("night, house 300 W, Sofar charges/discharges", &r);
	CHECK(abs(sum_sp(&r) + 300) <= 30, "Multis cover 300 W, not 936 W, sp sum %d", sum_sp(&r));
	CHECK(fabs(r.bat1) <= 30, "the stacks do not charge the Sofar battery, bat1 %.0f", r.bat1);
}

static void test_wp_running_no_night_floor(void)
{
	struct run r;
	struct plant pl = BASE(.house = 300, .wp = 3000, .r290_hz = 50, .sofar_dis = 1, .sofar_chg = 1);
	simulate(&r, &pl, local_time(2026, 1, 15, 22, 0), 40);       /* Sofar covers the house: Multis idle */
	print_state("winter night, WP 3 kW, Sofar covers", &r);
	CHECK(sum_sp(&r) >= -20 && sum_sp(&r) <= 0, "no night floor while the WP runs, sp sum %d", sum_sp(&r));
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
	CHECK(strstr(r.out.why[0], "FORCE_CHARGE") != NULL, "rule FORCE_CHARGE, got %s", r.out.why[0]);
}

static void test_discharge_protection(void)
{
	struct run r;
	struct plant pl = BASE(.house = 500, .soc = {50, 4.0});
	simulate(&r, &pl, local_time(2026, 1, 15, 22, 0), 10);
	print_state("Stack2 at 4 %", &r);
	CHECK(r.out.sp[1] == 0 && r.out.sp[2] == 0, "no discharge on L2/L3, got %d %d", r.out.sp[1], r.out.sp[2]);
	CHECK(strstr(r.out.why[1], "DISCHARGE_PROTECTION") != NULL, "rule DISCHARGE_PROTECTION, got %s", r.out.why[1]);
}

static void test_stale(void)
{
	struct run r;
	struct plant pl = BASE(.house = 500, .stale_inv = 1);
	simulate(&r, &pl, local_time(2026, 1, 15, 22, 0), 5);
	CHECK(sum_sp(&r) == 0 && rule_has(&r, "STALE"), "stale inverter data: all 0, got %d %s", sum_sp(&r), r.out.why[0]);
}

static void test_wp_cap_fallback(void)
{
	struct run r;
	struct plant pl = BASE(.house = 500, .wp = 4000, .r290_hz = 60, .wp_fresh = 0);
	simulate(&r, &pl, local_time(2026, 1, 15, 22, 0), 40);       /* SDM72D silent: old 1000 W cap */
	print_state("winter, em0/power stale, R290 running", &r);
	CHECK(sum_sp(&r) >= -1000 && sum_sp(&r) <= -990, "WP_CAP 1000 W, sp sum %d", sum_sp(&r));
	CHECK(rule_has(&r, "WP_CAP"), "rule WP_CAP");
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

static void test_ladesperre_summer_vs_winter(void)
{
	struct run r;
	time_t t = local_time(2026, 6, 21, 9, 0);
	struct plant pl = BASE(.house = 500, .soc = {50, 50});
	pl.pv = bm_dc_now(t, 6);                                    /* clear-sky day: measured = model */
	simulate(&r, &pl, t, 10);
	print_state("21 June 09:00, clear sky", &r);
	CHECK(r.out.ls.active && rule_has(&r, "LADESPERRE"), "summer: charge block before the peak, why %s", r.out.why[0]);
	CHECK(sum_sp(&r) == 0, "no charging while blocked, sp sum %d", sum_sp(&r));

	t = local_time(2026, 1, 21, 11, 0);
	pl.pv = 6000;
	simulate(&r, &pl, t, 20);
	print_state("21 Jan 11:00, PV 6 kW", &r);
	CHECK(!r.out.ls.active && sum_sp(&r) > 0, "winter: every watt charges, sp sum %d", sum_sp(&r));
}

static void test_ladesperre_clouds_release(void)
{
	struct run r;
	time_t t = local_time(2026, 6, 21, 9, 0);
	struct plant pl = BASE(.house = 500, .soc = {50, 50});
	pl.pv = 0.5 * bm_dc_now(t, 6);                              /* half the clear-sky power: clouds */
	simulate(&r, &pl, t, 10);
	print_state("21 June 09:00, clouds", &r);
	CHECK(!r.out.ls.active && r.st.badweather_today, "clouds release the block for the day");
	CHECK(sum_sp(&r) > 0, "charging, sp sum %d", sum_sp(&r));
}

static void test_season_measured_overrides_months(void)
{
	struct run r;
	struct plant pl = BASE(.house = 500, .wp = 1200, .r290_hz = 40, .season = BM_SUMMER, .season_age_h = 1);
	simulate(&r, &pl, local_time(2026, 10, 20, 23, 0), 60);      /* October, but measured: still summer */
	print_state("Oct night, season.py says summer", &r);
	CHECK(r.out.season == BM_SUMMER && r.out.season_measured, "measured summer used");
	CHECK(abs(sum_sp(&r) + 1700) <= 60, "WP from the batteries, sp sum %d", sum_sp(&r));

	pl.season = BM_WINTER;
	simulate(&r, &pl, local_time(2026, 5, 10, 23, 0), 60);       /* May, but measured: still winter */
	print_state("May night, season.py says winter", &r);
	CHECK(r.out.season == BM_WINTER && rule_has(&r, "|Z2"), "measured winter used, Z2 rule");
	CHECK(abs(sum_sp(&r) + 500) <= 60, "Multis cover only the house, sp sum %d", sum_sp(&r));
}

static void test_season_stale_falls_back_to_months(void)
{
	struct run r;
	struct plant pl = BASE(.house = 500, .wp = 1200, .r290_hz = 40, .season = BM_SUMMER, .season_age_h = 49);
	simulate(&r, &pl, local_time(2026, 11, 20, 23, 0), 40);      /* summer message 49 h old: November = winter */
	print_state("Nov night, season message 49 h old", &r);
	CHECK(r.out.season == BM_WINTER && !r.out.season_measured, "stale message: month rule (winter)");
	CHECK(abs(sum_sp(&r) + 500) <= 60, "Z2: Multis cover only the house, sp sum %d", sum_sp(&r));
}

static void test_transition_wp_1900(void)
{
	struct run r;
	/* W_MAX (1800 W total) would hide the 1900 W limit with a big heat pump: day, house 0, WP 2500 W */
	struct plant pl = BASE(.pv = 200, .house = 200, .wp = 2500, .r290_hz = 50, .season = BM_TRANSITION, .season_age_h = 1);
	simulate(&r, &pl, local_time(2026, 10, 9, 14, 0), 60);
	print_state("transition, WP 2.5 kW, house 0 net", &r);
	CHECK(r.out.season == BM_TRANSITION, "transition used");
	CHECK(fabs(r.pcc + 600) <= 40, "WP above 1900 W from the grid: pcc -600, got %.0f", r.pcc);
	CHECK(abs(sum_sp(&r) + 1900) <= 30, "batteries give 1900 W to the heat pump, sp sum %d", sum_sp(&r));
	CHECK(rule_has(&r, "|WP1900"), "rule |WP1900");

	pl.wp = 1500;                                               /* below 1900 W: all from the batteries */
	simulate(&r, &pl, local_time(2026, 10, 9, 14, 0), 60);
	print_state("transition, WP 1.5 kW", &r);
	CHECK(fabs(r.pcc) <= 60 && abs(sum_sp(&r) + 1500) <= 60, "WP 1.5 kW fully from the batteries, pcc %.0f sp %d",
		  r.pcc, sum_sp(&r));
}

static void test_param_override(void)
{
	struct run r;
	struct bm_cfg c;
	bm_cfg_default(&c);
	CHECK(c.w_max == 10000 && c.wp_bat_max_transition == 1900 && c.soc_min == 5, "defaults from BM_PARAMS");
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

static void test_forecast_overrides_winter(void)
{
	struct run r;
	struct plant pl = BASE(.house = 500, .wp = 4000, .r290_hz = 60, .season = BM_WINTER, .season_age_h = 1,
						   .fc_target = 20, .soc = {50, 50});
	simulate(&r, &pl, local_time(2026, 1, 15, 22, 0), 60);       /* sunny day ahead: stacks may run down to 20 % */
	print_state("winter, forecast target 20 %, SoC 50", &r);
	CHECK(r.out.fc_active && rule_has(&r, "|FC"), "forecast rule active");
	CHECK(abs(sum_sp(&r) + 4500) <= 60, "house + heat pump from the batteries, sp sum %d", sum_sp(&r));

	pl.soc[0] = pl.soc[1] = 18;                                  /* below the target: tariff rules again */
	simulate(&r, &pl, local_time(2026, 1, 15, 22, 0), 60);
	print_state("winter, forecast target 20 %, SoC 18", &r);
	CHECK(!r.out.fc_active && rule_has(&r, "|Z2"), "below target: Z2 again");
	CHECK(abs(sum_sp(&r) + 500) <= 60, "only the house, sp sum %d", sum_sp(&r));

	pl.soc[0] = pl.soc[1] = 21;                                  /* inside the hysteresis: not back on from cold */
	simulate(&r, &pl, local_time(2026, 1, 15, 22, 0), 10);
	CHECK(!r.out.fc_active, "21 %% < target 20 + 2: rule stays off");

	pl.soc[0] = pl.soc[1] = 50;
	pl.fc_age_h = 4;                                             /* stale forecast */
	simulate(&r, &pl, local_time(2026, 1, 15, 22, 0), 30);
	CHECK(!r.out.fc_active && rule_has(&r, "|Z2"), "stale forecast ignored");
}

static void test_pi_shadow_not_armed(void)
{
	struct run r;
	struct plant pl = BASE(.pv = 5000, .house = 800);
	simulate(&r, &pl, local_time(2026, 3, 10, 12, 0), 20);
	CHECK(isfinite(r.out.pi.u), "PI output finite");
	CHECK(!rule_has(&r, "PI("), "PI not armed: soyo rules, got %s", r.out.why[0]);
}

int main(void)
{
	setenv("TZ", "CET-1CEST,M3.5.0,M10.5.0/3", 1);
	tzset();
	test_winter_wp_from_grid();
	test_winter_wp_5kw();
	test_summer_wp_from_battery();
	test_summer_wp_4kw_from_battery();
	test_w_max_and_phase_cap();
	test_bank_split();
	test_winter_night_floor();
	test_wp_running_no_night_floor();
	test_day_surplus_to_zero();
	test_day_sofar_covers_house();
	test_day_sofar_saturated();
	test_force_charge();
	test_discharge_protection();
	test_stale();
	test_wp_cap_fallback();
	test_balance();
	test_ladesperre_summer_vs_winter();
	test_ladesperre_clouds_release();
	test_season_measured_overrides_months();
	test_season_stale_falls_back_to_months();
	test_transition_wp_1900();
	test_param_override();
	test_forecast_overrides_winter();
	test_pi_shadow_not_armed();
	printf("%d checks, %d failed\n", checks, fails);
	return fails ? 1 : 0;
}
