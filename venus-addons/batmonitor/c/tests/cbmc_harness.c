/*
 * cbmc_harness: proofs with CBMC (bounded model checker, bit-precise C incl. IEEE floating point) on the real
 * bm_logic.c code. Unlike tests/check_matrix.c (enumeration of the discrete layer) and the random amount checks,
 * every input here is symbolic: CBMC proves the assertions for ALL values in the assumed ranges, or prints a
 * counterexample.
 *
 *   h_d2_bits         the SoC predicates satisfy the constraints check_matrix assumes, for every SoC and every
 *                     threshold set in valid order (h_d2_bits_any_order: without the order -> counterexample)
 *   h_d1_bits         the D1 predicates satisfy the constraints check_matrix assumes
 *   h_alloc_discharge each phase within its cap, total never above the request, other phases untouched, the lead
 *                     bank first, without a lead both banks the same power (integer split, 0.45-c)
 *   h_dis_amount      the one amount formula: 0..W_MAX, WP_CAP while armed, IDLE never above the row's idle power
 *   h_charge          a CHARGE phase gets 0..its ceiling, never a discharge (0.44-c finding); assumes L2 + L3
 *                     share one cap (chain_gates computes it per bank, unequal caps give a counterexample)
 *   h_discharge       discharge phases within their caps, total within W_MAX / WP_CAP, IDLE at night = 0
 *
 * usage: make cbmc / make cbmc-full (apt install cbmc cadical; not on Venus), make cbmc-remote (see Makefile)
 */
#include "../bm_logic.c"
#include <assert.h>
#include <stdarg.h>

/* The rule / why texts are not part of any proof. cbmc >= 6 models vsnprintf character by character, which needs
 * more than --unwind 32 and blows the formula up to ~19 GB; this stub keeps the strings valid (empty) instead. */
int vsnprintf(char *s, size_t n, const char *fmt, va_list ap)
{
	(void)fmt;
	(void)ap;
	if (n)
		s[0] = 0;
	return 0;
}

double nondet_double(void);
int nondet_int(void);

static double in_range(double lo, double hi)
{
	double v = nondet_double();
	__CPROVER_assume(v >= lo && v <= hi);   /* excludes NaN and infinities */
	return v;
}

static int bit(void)
{
	int v = nondet_int();
	__CPROVER_assume(v == 0 || v == 1);
	return v;
}

static void soc_thresholds(struct bm_cfg *c, int valid_order)
{
	bm_cfg_default(c);
	c->soc_force = in_range(0, 30);
	c->soc_force_release = in_range(0, 40);
	c->soc_min = in_range(0, 50);
	c->soc_min_release = in_range(0, 60);
	if (valid_order)
		__CPROVER_assume(c->soc_force <= c->soc_min && c->soc_force_release <= c->soc_min_release
						 && c->soc_min <= c->soc_min_release && c->soc_force <= c->soc_force_release
						 && c->soc_min < c->soc_min_release && c->soc_force < c->soc_force_release);
}

static void d2_bits_props(int valid_order)
{
	struct bm_cfg c;
	struct bm_d2_bank x;
	soc_thresholds(&c, valid_order);
	d2_bank_bits(&c, 1, in_range(0, 100), bit(), in_range(-20000, 20000), &x);
	assert(!x.soc_lt_force || x.soc_lt_min);
	assert(!(x.soc_lt_min && x.soc_ge_release));
	assert(!(x.soc_lt_force && x.soc_ge_force_release));
	assert(!x.soc_ge_release || x.soc_ge_force_release);
	assert(!x.soc_lt_min || x.soc_lt_100);
	assert(x.soc_lt_100 || x.soc_ge_release);
}

void h_d2_bits(void) { d2_bits_props(1); }
void h_d2_bits_any_order(void) { d2_bits_props(0); }

void h_d1_bits(void)
{
	struct bm_cfg c;
	struct bm_in in;
	struct bm_out out;
	struct chain ch;
	struct bm_d1_in b;
	bm_cfg_default(&c);
	c.fc_hyst = in_range(0, 20);
	c.wp_bat_share_bad = in_range(0, 100);
	memset(&in, 0, sizeof(in));
	memset(&out, 0, sizeof(out));
	memset(&ch, 0, sizeof(ch));
	in.wp = in_range(-1000, 30000);
	in.fc_target = in_range(-1, 100);
	in.soc_bat1 = in_range(0, 100);
	for (int k = 0; k < NBANK; k++) {
		in.bms_ok[k] = bit();
		in.soc[k] = in_range(0, 100);
	}
	out.season = BM_TRANSITION;
	ch.wp_fresh = bit();
	ch.wp_on = ch.wp_fresh && in.wp >= WP_ON_TH;            /* as chain_inputs */
	d1_bits(&c, &in, &out, &ch, &b);
	assert(!b.wp_pos || b.wp_fresh);
	assert(!b.wp_on || b.wp_pos);
	assert(!(b.fc_above_on && b.fc_below_off));
}

void h_alloc_discharge(void)
{
	int sp[NPH] = {0}, dis[NPH], dcap[NPH], lead = nondet_int(), w = nondet_int();
	char why[NPH][WHY_LEN] = {{0}};
	__CPROVER_assume(lead >= -1 && lead <= 1 && w >= 0 && w <= 12000);
	for (int p = 0; p < NPH; p++) {
		dis[p] = bit();
		dcap[p] = nondet_int();
		__CPROVER_assume(dcap[p] >= 0 && dcap[p] <= 5000);
	}
	alloc_discharge(lead, w, dis, dcap, "PROP", sp, why);
	int total = 0, bank_w[NBANK] = {0}, bank_at_cap[NBANK] = {0}, bank_dis[NBANK] = {0}, other = 0, lead_full = 1;
	for (int p = 0; p < NPH; p++) {
		int b = bank_of(p);
		assert(sp[p] <= 0);
		assert(dis[p] || sp[p] == 0);
		assert(-sp[p] <= dcap[p]);
		total -= sp[p];
		bank_w[b] -= sp[p];
		bank_dis[b] |= dis[p];
		bank_at_cap[b] |= dis[p] && -sp[p] >= dcap[p] - NPH;
		if (lead >= 0 && dis[p] && b != lead)
			other -= sp[p];
		if (lead >= 0 && dis[p] && b == lead && -sp[p] < dcap[p] - NPH)
			lead_full = 0;
	}
	assert(total <= w);
	/* lead first: the other bank gets more than rounding only when every lead phase is at its cap */
	assert(other <= NPH || lead_full);
	/* no lead: both banks the same power unless a cap binds (0.26) */
	if (lead < 0 && bank_dis[0] && bank_dis[1] && !bank_at_cap[0] && !bank_at_cap[1]) {
		assert(bank_w[0] - bank_w[1] <= NPH && bank_w[1] - bank_w[0] <= NPH);
		assert(total >= w - 2 * NPH);                   /* and all of w, up to the division remainder */
	}
}

/* S4 amount: the one formula */
void h_dis_amount(void)
{
	int prop = bit(), armed = bit(), capped, deficit = nondet_int(), w_max = nondet_int(), wp_cap = nondet_int();
	__CPROVER_assume(w_max >= 0 && w_max <= 12000 && wp_cap >= 0 && wp_cap <= 10000);
	for (int s = 0; s < BM_SRC_N; s++) {
		int w = dis_amount(prop, deficit, BM_SRC_RULE[s].idle_w, w_max, armed, wp_cap, &capped);
		assert(w >= 0 && w <= w_max);
		assert(!armed || w <= wp_cap);
		assert(prop || w <= BM_SRC_RULE[s].idle_w);
		assert(!prop || deficit <= 0 || w == deficit || w == w_max || (armed && w == wp_cap));
		assert(!capped || (armed && w == wp_cap));   /* the WPCAP tag only when WP_CAP is what limits */
	}
}

void h_charge(void)
{
	struct bm_cfg cfg;
	struct bm_state st;
	struct bm_out out;
	struct chain c;
	bm_cfg_default(&cfg);
	bm_init(&st);
	memset(&out, 0, sizeof(out));
	memset(&c, 0, sizeof(c));
	st.lead = nondet_int();
	__CPROVER_assume(st.lead >= -1 && st.lead <= 1);
	c.own_charge = in_range(0, 15000);
	c.chg_ref = in_range(-20000, 30000);
	c.bat1_eff = in_range(-2500, 1250);
	for (int p = 0; p < NPH; p++) {
		c.charge[p] = bit();
		c.n_chg += c.charge[p];
		out.chg_cap[p] = in_range(0, 5000);
	}
	__CPROVER_assume(c.charge[1] == c.charge[2] && out.chg_cap[1] == out.chg_cap[2]);   /* L2 + L3 = one bank, one CCL */
	__CPROVER_assume(c.n_chg > 0);
	chain_charge(&cfg, &st, &out, &c);
	for (int p = 0; p < NPH; p++) {
		assert(out.sp[p] >= 0);
		assert(c.charge[p] || out.sp[p] == 0);
		assert(out.sp[p] <= fmin(out.chg_cap[p], cfg.charge_max_phase) + 1);
	}
}

void h_discharge(void)
{
	struct bm_cfg cfg;
	struct bm_state st;
	struct bm_out out;
	struct bm_in in;
	struct chain c;
	bm_cfg_default(&cfg);
	bm_init(&st);
	memset(&out, 0, sizeof(out));
	memset(&in, 0, sizeof(in));
	memset(&c, 0, sizeof(c));
	st.lead = nondet_int();
	__CPROVER_assume(st.lead >= -1 && st.lead <= 1);
	st.soyo_prop_prev = bit();
	in.bat1 = in_range(-2500, 2500);
	c.z2 = in_range(-20000, 20000);
	c.own_charge = in_range(0, 15000);
	c.own_discharge = in_range(0, 15000);
	c.src = nondet_int();
	__CPROVER_assume(c.src >= 0 && c.src < BM_SRC_N);
	c.wp_cap_armed = bit();
	for (int p = 0; p < NPH; p++) {
		c.discharge[p] = bit();
		c.n_dis += c.discharge[p];
		out.dis_cap[p] = in_range(0, cfg.discharge_max_phase);
	}
	__CPROVER_assume(c.n_dis > 0);
	strcpy(out.wp_why, "ALL");
	strcpy(out.src_why, "DAY");
	chain_discharge(&cfg, &in, &st, &out, &c);
	int total = 0;
	for (int p = 0; p < NPH; p++) {
		assert(out.sp[p] <= 0);
		assert(c.discharge[p] || out.sp[p] == 0);
		assert(-out.sp[p] <= out.dis_cap[p]);
		total -= out.sp[p];
	}
	assert(total <= cfg.w_max + NPH);
	assert(!c.wp_cap_armed || total <= cfg.wp_cap + NPH);
	assert(st.soyo_prop_prev || BM_SRC_RULE[c.src].idle_w || total == 0);   /* IDLE with the night floor: 0 W */
}
