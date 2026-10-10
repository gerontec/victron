/*
 * check_matrix: exhaustive check of the batmonitor decision layer (bm_d1, bm_d2, bm_dlead, bm_d3; 0.43-c).
 *
 * Every decision is a pure function of predicates ("bits", threshold comparisons of the inputs) and a few hysteresis
 * state bits. This program enumerates EVERY bit / state combination of each function that is physically consistent
 * (the constraints are listed at each function; they follow from the threshold order, which is checked against the
 * defaults first) and checks:
 *   - invariants (safety and consistency rules, listed below),
 *   - stability: with constant inputs the state reaches a fixed point after one step (no oscillation),
 *   - reachability: which reasons / modes occur, printed as matrices.
 * Combinations that cannot happen physically may be included (over-approximation): an invariant that holds for
 * them holds for the real plant too.
 * The amounts (W) are no finite state machine; for them a property check runs bm_step on random inputs.
 *
 * usage: make check        (exit code 1 on any violation)
 */
#include "bm_logic.h"
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static long checks, fails;
#define INV(cond, ...) do { checks++; if (!(cond)) { if (fails++ < 20) { printf("  FAIL %s:%d: ", __func__, __LINE__); \
	printf(__VA_ARGS__); putchar('\n'); } } } while (0)
#define BIT(v, i) (((v) >> (i)) & 1)

static void check_threshold_order(void)
{
	struct bm_cfg c;
	bm_cfg_default(&c);
	/* the D2 constraints below need SOC_FORCE <= SOC_MIN, SOC_FORCE_RELEASE <= SOC_MIN_RELEASE and the hysteresis
	   order; D1 / lead need FC_HYST >= 0, SOC_BALANCE_OFF <= SOC_BALANCE_ON */
	INV(c.soc_force <= c.soc_min, "SOC_FORCE %g <= SOC_MIN %g", c.soc_force, c.soc_min);
	INV(c.soc_force_release <= c.soc_min_release, "SOC_FORCE_RELEASE <= SOC_MIN_RELEASE");
	INV(c.soc_min <= c.soc_min_release && c.soc_force <= c.soc_force_release, "release above stop");
	INV(c.soc_min_release <= 100, "SOC_MIN_RELEASE <= 100");
	INV(c.fc_hyst >= 0, "FC_HYST >= 0");
	INV(c.soc_balance_off <= c.soc_balance_on, "SOC_BALANCE_OFF <= SOC_BALANCE_ON");
	printf("threshold order of the defaults: checked\n");
}

/* ---- discharge matrix (0.45-c): the table rows themselves ------------------------------------------------------ */
static void check_dis_table(void)
{
	printf("\ndischarge matrix, S3 source rows:\n  %-4s %9s %7s %6s %9s\n", "SRC", "b1_signed", "trickle", "idle_w",
		   "prop_hold");
	for (int s = 0; s < BM_SRC_N; s++) {
		const struct bm_src_rule *r = &BM_SRC_RULE[s];
		printf("  %-4s %9d %7d %6d %9d\n", BM_SRC_NAME[s], r->b1_signed, r->trickle, r->idle_w, r->prop_hold);
		int stk = s == BM_SRC_STK;
		INV(r->b1_signed == stk && r->trickle == stk && r->prop_hold == stk, "only the night floor takes Bat1 over");
		INV((r->idle_w == 0) == stk, "idle power 0 exactly with the night floor (PCC near 0)");
		INV(r->idle_w >= 0, "idle power never charges");
	}
	printf("  S2 heat pump kinds:");
	for (int k = 0; k < BM_WPK_N; k++)
		printf(" %s", BM_WPK_NAME[k]);
	putchar('\n');
}

/* ---- mode matrix (0.48-c): what each S1 mode means for the setpoint -------------------------------------------- */
static void check_mode_table(void)
{
	static const char *A[] = {"0 W", "FORCE_W", "charge", "discharge"};
	static const char *W[] = {"-", "NAME", "NAME(soc)", "NAME(h)", "NAME(W)", "NAME WP SRC"};
	printf("\nmode matrix, S1 mode rows:\n  %-12s %-10s %6s  %s\n", "mode", "action", "capped", "rule text");
	for (int m = 0; m < BM_M_N; m++) {
		const struct bm_mode_rule *r = &BM_MODE_RULE[m];
		printf("  %-12s %-10s %6d  %s\n", BM_MODE_NAME[m], A[r->action], r->capped, W[r->why]);
		INV((r->action == BM_A_DISCHARGE) == (m == BM_M_DIS), "only mode DIS discharges");
		INV((r->action == BM_A_CHARGE) == (m == BM_M_CHARGE), "only mode CHARGE takes the charge amount");
		INV((r->why == BM_W_NONE) == (r->action == BM_A_DISCHARGE), "every mode writes its rule text, DIS in S4");
		INV(r->action == BM_A_ZERO || r->capped, "every mode that gives power is capped (FORCE since 0.48-c)");
	}
}

/* ---- D1: forecast, S3 source, S2 scope ------------------------------------------------------------------------ */
static void check_d1(void)
{
	static const int SEASONS[3] = {BM_SUMMER, BM_TRANSITION, BM_WINTER};
	static const char *SN[4] = {"", "summer", "winter", "transition"};
	long combos = 0, reach[BM_WP_N][BM_SRC_N] = {{0}}, reach_season[4][BM_SRC_N] = {{0}};
	for (int si = 0; si < 3; si++)
		for (unsigned v = 0; v < (1u << 14); v++)
			for (int s = 0; s < 4; s++) {
				struct bm_d1_in b = {SEASONS[si], BIT(v, 0), BIT(v, 1), BIT(v, 2), BIT(v, 3), BIT(v, 4), BIT(v, 5),
									 BIT(v, 6), BIT(v, 7), BIT(v, 8), BIT(v, 9), BIT(v, 10), BIT(v, 11), BIT(v, 12),
									 BIT(v, 13)};
				/* constraints: wp_pos needs fresh data (> 0 W), wp_on needs wp_pos (WP_ON_TH > 0);
				   SoC > target + FC_HYST excludes SoC < target */
				if ((b.wp_pos && !b.wp_fresh) || (b.wp_on && !b.wp_pos) || (b.fc_above_on && b.fc_below_off))
					continue;
				int fa = s & 1, bf = s >> 1;
				struct bm_d1_out o, o2;
				bm_d1(&b, fa, bf, &o);
				combos++;
				reach[o.wp_reason][o.src_reason]++;
				reach_season[o.season_wp][o.src_reason]++;
				INV(!o.fc_active || o.fc_ok, "FC_ACTIVE only with a fresh forecast and a BMS");
				INV(!o.fc_bad || (b.fc_fresh && !o.fc_active), "FC_BAD only with a fresh forecast, never with FC_ACTIVE");
				INV(!o.fc_active || o.season_wp == BM_SUMMER, "FC_ACTIVE serves the heat pump as in summer");
				INV(o.fc_active || o.season_wp == b.season, "without FC_ACTIVE the measured season applies");
				INV(!o.b1_first || (b.wp_on && b.b1_above_min), "B1FIRST only with the WP running and Bat1 > BAT1_SOC_MIN");
				INV(!o.night_floor || (b.night && !o.b1_first), "night floor only at night and not with B1FIRST");
				INV((o.src_reason == BM_SRC_DAY) == !b.night, "SRC DAY exactly by day");
				INV((o.src_reason == BM_SRC_B1) == (b.night && o.b1_first), "SRC B1 exactly at night with B1FIRST");
				INV((o.src_reason == BM_SRC_STK) == o.night_floor, "SRC STK exactly with the night floor");
				INV(o.src_reason != BM_SRC_B1W || (b.season == BM_WINTER && b.wp_on), "SRC B1W only winter + WP");
				/* S3 independent of the forecast state (0.45-c) */
				{
					struct bm_d1_out x;
					bm_d1(&b, !fa, bf, &x);
					INV(x.src_reason == o.src_reason && x.night_floor == o.night_floor, "SRC independent of FC_ACTIVE");
				}
				INV(!o.wp_fc_capped || (o.fc_bad && b.wp_pos), "forecast cap only with FC_BAD and WP power");
				INV(!o.wp_cap_armed || (!b.wp_fresh && o.winter && b.wp_running), "WP_CAP only as stale-data fallback");
				INV((o.wp_reason == BM_WP_NA) == !b.wp_fresh, "WP:NA exactly with stale em0/power");
				INV((o.wp_reason == BM_WP_OFF) == (b.wp_fresh && !b.wp_on), "WP:OFF exactly below WP_ON_TH");
				INV((o.wp_reason == BM_WP_FC) == (b.wp_on && o.fc_active), "WP:FC exactly WP on + FC_ACTIVE");
				INV(o.wp_reason != BM_WP_FCBAD || o.wp_fc_capped, "WP:FCBAD only when the forecast cap binds");
				INV(o.wp_reason != BM_WP_Z2 || o.season_wp == BM_WINTER, "WP:Z2 only in winter");
				/* discharge matrix rows (0.45-c) */
				INV((o.wp_kind == BM_WPK_BAD) == o.wp_fc_capped, "WP kind BAD exactly when the forecast cap binds");
				INV(o.wp_reason != BM_WP_Z2 || o.wp_kind == BM_WPK_NONE, "WP:Z2 -> the stacks cover nothing");
				INV(o.wp_reason != BM_WP_FC || o.wp_kind == BM_WPK_ALL, "WP:FC -> the stacks cover all");
				INV(o.wp_reason != BM_WP_FCBAD || o.wp_kind == BM_WPK_BAD, "WP:FCBAD -> WP_BAT_SHARE_BAD");
				INV(o.wp_reason != BM_WP_T1900 || o.wp_kind == BM_WPK_CAP, "WP:T1900 -> the transition cap");
				INV(BM_SRC_RULE[o.src_reason].b1_signed == o.night_floor, "Bat1 signed exactly with the night floor");
				/* B1FIRST in winter changes only the reason, not the regulation (night floor, 0.40-c note) */
				if (b.season == BM_WINTER) {
					struct bm_d1_out x;
					bm_d1(&b, fa, !bf, &x);
					INV(x.night_floor == o.night_floor, "winter: night floor independent of bat1_first");
				}
				bm_d1(&b, o.fc_active, o.bat1_first, &o2);
				INV(o2.fc_active == o.fc_active && o2.bat1_first == o.bat1_first, "D1 state fixed after one step");
			}
	printf("\nD1 forecast / source / scope: %ld consistent combinations (14 bits x 3 seasons x 4 states)\n", combos);
	printf("  WP reason x SRC reason (count of combinations, 0 = unreachable):\n  %-6s", "");
	for (int s = 0; s < BM_SRC_N; s++)
		printf("%8s", BM_SRC_NAME[s]);
	putchar('\n');
	for (int w = 0; w < BM_WP_N; w++) {
		printf("  %-6s", BM_WP_NAME[w]);
		for (int s = 0; s < BM_SRC_N; s++)
			printf("%8ld", reach[w][s]);
		putchar('\n');
	}
	printf("  season used for the WP x SRC:\n");
	for (int se = 1; se <= 3; se++) {
		printf("  %-11s", SN[se]);
		for (int s = 0; s < BM_SRC_N; s++)
			printf("%8ld", reach_season[se][s]);
		putchar('\n');
	}
}

/* ---- D2: S0 protection / force charge, S1 mode ---------------------------------------------------------------- */
static int bank_ok(const struct bm_d2_bank *x)
{
	/* constraints from the threshold order (check_threshold_order): SOC_FORCE <= SOC_MIN < SOC_MIN_RELEASE <= 100,
	   SOC_FORCE < SOC_FORCE_RELEASE <= SOC_MIN_RELEASE */
	if (x->soc_lt_force && !x->soc_lt_min)
		return 0;
	if (x->soc_lt_min && x->soc_ge_release)
		return 0;
	if (x->soc_lt_force && x->soc_ge_force_release)
		return 0;
	if (x->soc_ge_release && !x->soc_ge_force_release)
		return 0;
	if (x->soc_lt_min && !x->soc_lt_100)
		return 0;
	if (!x->soc_lt_100 && !x->soc_ge_release)
		return 0;
	return 1;
}

static void check_d2(void)
{
	struct bm_d2_bank banks[128];
	int nb = 0;
	for (unsigned v = 0; v < 128; v++) {
		struct bm_d2_bank x = {BIT(v, 0), BIT(v, 1), BIT(v, 2), BIT(v, 3), BIT(v, 4), BIT(v, 5), BIT(v, 6)};
		if (bank_ok(&x))
			banks[nb++] = x;
	}
	long combos = 0, modes[BM_M_N] = {0};
	for (int i0 = 0; i0 < nb; i0++)
		for (int i1 = 0; i1 < nb; i1++)
			for (unsigned g = 0; g < 16; g++)
				for (unsigned s = 0; s < 32; s++) {
					struct bm_d2_in b = {BIT(g, 0), BIT(g, 1), BIT(g, 2), BIT(g, 3), {banks[i0], banks[i1]}};
					if (b.surplus_gt_on && !b.surplus_gt_hold)   /* PCC_SURPLUS_TH > SOYO_HOLD_TH */
						continue;
					int prot[NBANK] = {BIT(s, 0), BIT(s, 1)}, force[NBANK] = {BIT(s, 2), BIT(s, 3)}, cp = BIT(s, 4);
					struct bm_d2_out o, o2;
					bm_d2(&b, prot, force, cp, &o);
					combos++;
					for (int k = 0; k < NBANK; k++) {
						const struct bm_d2_bank *x = &b.bank[k];
						int m = o.mode[k];
						modes[m]++;
						INV(m != BM_M_DIS || (x->bms_ok && !o.prot[k] && !o.force[k] && !b.stale && !o.charge_mode
											   && !x->charging), "DIS only with BMS, unprotected, fresh data, no surplus");
						INV(!(x->bms_ok && x->soc_lt_min) || m != BM_M_DIS, "never discharge below SOC_MIN");
						INV(!(x->bms_ok && x->soc_lt_force) || m == BM_M_FORCE, "below SOC_FORCE always force charge");
						INV(m != BM_M_CHARGE || (x->soc_lt_100 && o.charge_mode && !b.block && !o.force[k] && !b.stale),
							"CHARGE only below 100 %%, with surplus, no charge block, fresh data");
						INV(!b.stale || (m != BM_M_DIS && m != BM_M_CHARGE), "stale data: neither charge nor discharge");
						INV(x->bms_ok || (m == BM_M_BMS_MISSING && o.prot[k] == prot[k] && o.force[k] == force[k]),
							"without BMS: BMS_MISSING, protection state kept");
						INV(!o.force[k] || !x->bms_ok || m == BM_M_FORCE, "force charge has priority");
					}
					bm_d2(&b, o.prot, o.force, o.chg_prev, &o2);
					INV(!memcmp(o2.prot, o.prot, sizeof(o.prot)) && !memcmp(o2.force, o.force, sizeof(o.force))
						&& o2.chg_prev == o.chg_prev, "D2 state fixed after one step");
				}
	printf("\nD2 protection / mode: %ld consistent combinations (%d bank bit sets of 128, ^2 x 4 global bits x 32 states)\n",
		   combos, nb);
	printf("  mode reached (bank-cycles):");
	for (int m = 0; m < BM_M_N; m++)
		printf(" %s %ld", BM_MODE_NAME[m], modes[m]);
	putchar('\n');
}

/* ---- lead ------------------------------------------------------------------------------------------------------ */
static void check_lead(void)
{
	long combos = 0;
	for (unsigned v = 0; v < 16; v++)
		for (int lead = -1; lead <= 1; lead++) {
			struct bm_dl_in b = {BIT(v, 0), BIT(v, 1), BIT(v, 2), BIT(v, 3)};
			if (b.diff_gt_on && b.diff_lt_off)                  /* SOC_BALANCE_OFF <= SOC_BALANCE_ON */
				continue;
			int ev, ev2, n = bm_dlead(&b, lead, &ev);
			combos++;
			INV(b.both_ok || n == -1, "no lead without both BMS");
			INV(n == -1 || n == 0 || n == 1, "lead in range");
			INV(ev == 0 || n != lead || !b.both_ok, "event only on a change");
			INV(!(n >= 0 && lead < 0) || n == (b.first_ahead ? 0 : 1), "the bank ahead takes the lead");
			INV(bm_dlead(&b, n, &ev2) == n, "lead fixed after one step");
		}
	printf("\nlead: %ld consistent combinations, invariants checked\n", combos);
}

/* ---- D4: summer peak window as a forecast export cap (0.49-c) -------------------------------------------------- */
static void check_d4(void)
{
	long combos = 0, blocks = 0, caps = 0;
	for (unsigned v = 0; v < (1u << 8); v++)
		for (int k = 0; k < 2; k++) {
			struct bm_d4_in b = {BIT(v, 0), BIT(v, 1), BIT(v, 2), BIT(v, 3), BIT(v, 4), BIT(v, 5), BIT(v, 6), BIT(v, 7)};
			if (b.surplus_cap_on && !b.surplus_cap_hold)      /* PCC_SURPLUS_TH > SOYO_HOLD_TH */
				continue;
			struct bm_d4_state s = {k}, n, n2;
			int cap, cap2, r = bm_d4(&b, &s, &n, &cap);
			combos++;
			blocks += r;
			caps += cap;
			INV(cap == (b.enabled && b.season && b.fc_cap_ahead), "cap exactly when enabled, in season and expected");
			INV(!r || cap, "waiting below the cap only while the cap holds");
			INV(!r || !b.surplus_cap_on, "a surplus above the cap always charges");
			INV(!r || !(b.chg_prev && b.surplus_cap_hold), "charging above the cap holds down to SOYO_HOLD_TH");
			INV(!b.pcc_peak || n.peak_today, "a PCC peak is remembered for the day");
			INV(b.pcc_peak || (b.new_day ? !n.peak_today : n.peak_today == s.peak_today), "peak_today until midnight");
			b.new_day = 0;
			int r2 = bm_d4(&b, &n, &n2, &cap2);
			INV(r2 == r && cap2 == cap && n2.peak_today == n.peak_today, "D4 fixed point after one step");
		}
	printf("\nD4 peak window (export cap): %ld consistent combinations (8 bits x 2 states), %ld cap, %ld waiting\n",
		   combos, caps, blocks);
}

/* ---- D5: DO4 pulse (0.46-c) ----------------------------------------------------------------------------------- */
static void check_d5(void)
{
	for (int v = 0; v < 16; v++) {
		struct bm_d5_in b = {BIT(v, 0), BIT(v, 1), BIT(v, 2), BIT(v, 3)};
		if ((b.grace_over || b.pcc_hard) && !b.pcc_over)  /* both are refinements of pcc_over */
			continue;
		int r = bm_d5(&b);
		INV(!r || b.pcc_over, "DO4 only with a fresh PCC above 20 kW");
		INV(!r || b.grace_over || b.pcc_hard, "DO4 only after the grace time or above the hard limit");
		INV(!r || b.lockout_over, "DO4 at most once per DO4_LOCKOUT");
		INV(r || !b.lockout_over || !(b.grace_over || b.pcc_hard), "DO4 always when due");
	}
	printf("\nD5 DO4 pulse: 4 bits, grace / hard limit\n");
}

/* ---- D3 -------------------------------------------------------------------------------------------------------- */
static void check_d3(void)
{
	long combos = 0, prop = 0;
	for (unsigned v = 0; v < 16; v++)
		for (int pp = 0; pp < 2; pp++) {
			struct bm_d3_in b = {BIT(v, 0), BIT(v, 1), BIT(v, 2), BIT(v, 3)};
			int nx, nx2, r = bm_d3(&b, pp, &nx);
			combos++;
			prop += r;
			INV(b.any_dis || (!r && !nx), "no discharging phase: IDLE and reset");
			INV(!(b.any_dis && b.house_import) || r, "importing: PROPORTIONAL");
			INV(!(b.any_dis && !b.house_import && !b.deficit_gt_hold) || !r, "no import, no deficit: IDLE");
			bm_d3(&b, nx, &nx2);
			INV(nx2 == nx, "D3 state fixed after one step");
		}
	printf("\nD3 PROPORTIONAL / IDLE: %ld combinations, %ld PROPORTIONAL\n", combos, prop);
}

/* ---- amounts: properties of bm_step on random inputs ----------------------------------------------------------- */
static double r(double a, double b) { return a + (b - a) * (rand() / (double)RAND_MAX); }
static int ri(int n) { return rand() % n; }

static void check_amounts(void)
{
	long cycles = 0;
	srand(42);
	for (int run = 0; run < 1000; run++) {
		struct bm_cfg cfg;
		struct bm_state st;
		struct bm_out out;
		struct bm_in in;
		bm_cfg_default(&cfg);
		cfg.forecast = ri(5) != 0;
		bm_init(&st);
		time_t t0 = 1767225600 + (time_t)ri(365) * 86400 + ri(86400);
		double now = 1000, soc[2] = {r(0, 100), r(0, 100)};
		for (int k = 0; k < 200; k++, now += 5) {
			memset(&in, 0, sizeof(in));
			in.now = now;
			in.t = t0 + k * 5;
			in.have_pcc = ri(20) != 0;
			in.have_pv = ri(20) != 0;
			in.pcc = r(-8000, 12000);
			in.pv = ri(3) ? r(0, 25000) : r(0, 150);
			in.bat1 = r(-2500, 2500);
			in.pcc_avg5 = in.pcc;
			in.bat1_avg5 = in.bat1;
			in.inv_time = ri(15) ? now : now - 400;
			in.have_soc_bat1 = ri(10) != 0;
			in.soc_bat1 = r(0, 100);
			in.r290_hz = ri(2) ? ri(90) : 0;
			in.r290_time = ri(10) ? now : 0;
			in.wp = ri(3) ? r(0, 5000) : r(200, 450);
			in.wp_time = ri(6) ? now : now - 300;
			in.season = ri(4);
			in.season_ts = in.t - ri(4) * 43200;
			in.fc_target = ri(4) ? (ri(2) ? 100 : r(5, 100)) : -1;
			in.fc_ts = in.t - ri(5) * 3600;
			for (int p = 0; p < NPH; p++) {
				in.ac_ok[p] = ri(15) != 0;
				in.ac_in[p] = st.setpoints[p] + r(-200, 200);
			}
			for (int b = 0; b < NBANK; b++) {
				soc[b] = fmax(0, fmin(100, soc[b] + r(-3, 3)));
				in.bms_ok[b] = ri(25) != 0;
				in.soc[b] = ri(8) ? soc[b] : r(0, 10);
				in.power_ok[b] = ri(10) != 0;
				in.power[b] = r(-6000, 6000);
				in.volt_ok[b] = ri(5) != 0;
				in.volt[b] = r(48, 57);
				in.lim_ok[b] = ri(2);
				in.ccl[b] = r(0, 150);
				in.dcl[b] = r(0, 250);
			}
			bm_step(&cfg, &in, &st, &out);
			cycles++;
			int dis_sum = 0, wpcap = 0;
			for (int p = 0; p < NPH; p++) {
				const char *w = out.why[p];
				int sp = out.sp[p];
				if (sp < 0) {
					dis_sum -= sp;
					INV(!strncmp(w, "PROP", 4) || !strncmp(w, "IDLE", 4), "discharge only in PROP / IDLE, got %d %s", sp, w);
					INV(-sp <= out.dis_cap[p] + 1, "discharge %d within the phase cap %.0f", sp, out.dis_cap[p]);
				}
				if (sp > 0) {
					INV(!strncmp(w, "CHARGE", 6) || !strncmp(w, "FORCE", 5), "charge only in CHARGE / FORCE, got %d %s", sp, w);
					if (!strncmp(w, "CHARGE", 6))
						INV(sp <= fmin(out.chg_cap[p], cfg.charge_max_phase) + 1, "charge %d within the phase cap %.0f",
							sp, out.chg_cap[p]);
				}
				if (!strncmp(w, "PROT", 4) || !strncmp(w, "STALE", 5) || !strncmp(w, "BMS_MISSING", 11)
					|| !strncmp(w, "FULL", 4) || !strncmp(w, "CHARGING", 8) || !strncmp(w, "LADESPERRE", 10))
					INV(sp == 0, "no power in %s, got %d", w, sp);
				if (!strncmp(w, "FORCE", 5))
					INV(sp == (int)fmin(cfg.force_charge_w, out.chg_cap[p]), "force charge min(FORCE_CHARGE_W, chg_cap %.0f), "
						"got %d", out.chg_cap[p], sp);
				if (strstr(w, "WPCAP"))
					wpcap = 1;
			}
			INV(dis_sum <= cfg.w_max + NPH, "total discharge %d <= W_MAX", dis_sum);
			INV(!wpcap || dis_sum <= cfg.wp_cap + NPH, "total discharge %d <= WP_CAP with WPCAP", dis_sum);
			int wp_fresh = in.now - in.wp_time < 150.0;
			if (wp_fresh && in.wp > 0) {
				double share = in.wp - out.wp_eff;
				INV(share >= -1e-6 && share <= in.wp + 1e-6, "WP share %.0f within 0..wp %.0f", share, in.wp);
				if (!out.fc_active && out.season == BM_WINTER)
					INV(fabs(share) < 1e-6, "winter without FC: the stacks cover none of the WP, share %.0f", share);
				if (!out.fc_active && out.season == BM_TRANSITION)
					INV(share <= cfg.wp_bat_max_transition + 1e-6, "transition: share %.0f <= %.0f", share,
						cfg.wp_bat_max_transition);
				if (out.fc_bad)
					INV(share <= in.wp * cfg.wp_bat_share_bad / 100.0 + 1e-6, "bad forecast: share %.0f <= %.0f %%",
						share, cfg.wp_bat_share_bad);
			}
		}
	}
	printf("\namounts: %ld random bm_step cycles, properties checked (discharge / charge only in their modes, phase and\n"
		   "  total caps, no power in PROT / STALE / BMS_MISSING / FULL / CHARGING / LADESPERRE, WP share per season)\n", cycles);
}

int main(void)
{
	check_threshold_order();
	check_dis_table();
	check_mode_table();
	check_d4();
	check_d5();
	check_d1();
	check_d2();
	check_lead();
	check_d3();
	check_amounts();
	printf("\n%ld checks, %ld failed\n", checks, fails);
	return fails ? 1 : 0;
}
