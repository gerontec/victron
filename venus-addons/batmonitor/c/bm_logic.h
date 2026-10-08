/*
 * bm_logic: the pure control logic of batmonitor (one cycle = bm_step), without D-Bus, MQTT, clock or files.
 * batmonitor.c collects the inputs, calls bm_step and writes setpoints, log and state file;
 * tests/test_logic.c drives it with a plant model (make test, runs on any host).
 */
#ifndef BM_LOGIC_H
#define BM_LOGIC_H

#include <time.h>

#define NPH 3
#define NBANK 2
#define CYCLE_SECONDS 5
#define WHY_LEN 96
#define BM_SUMMER 1
#define BM_WINTER 2
#define BM_TRANSITION 3

struct bm_bank {
	const char *name, *service;
	int nph, ph[NPH];
	double capacity_wh;
};
extern const struct bm_bank BM_BANKS[NBANK];
extern const char *BM_PH[NPH];

struct bm_cfg {
	int ladesperre;          /* BATMONITOR_LADESPERRE (default on) */
	int pi_armed;            /* BATMONITOR_PI=1: the PI replaces the soyo amounts */
	int forecast;            /* BATMONITOR_FORECAST (default on): forecast target SoC may override the season */
	/* tunable parameters, defaults and limits in BM_PARAMS (bm_logic.c), env BATMONITOR_<NAME> in batmonitor.c */
	double w_max, discharge_max_phase, charge_max_phase, charger_cap_phase;
	double wp_bat_max_transition, wp_cap;
	double soc_min, soc_min_release, soc_force, soc_force_release, force_charge_w;
	double soc_balance_on, soc_balance_off;
	double soyo_target, b_night;
	double fc_hyst;
	double sofar_trickle;
};

/* parameter table: name (env BATMONITOR_<name>), default, allowed range, unit, meaning */
struct bm_param {
	const char *name;
	unsigned off;            /* offsetof(struct bm_cfg, field) */
	double def, min, max;
	const char *unit, *help;
};
extern const struct bm_param BM_PARAMS[];
extern const int BM_NPARAMS;
void bm_cfg_default(struct bm_cfg *cfg);                       /* all BM_PARAMS defaults, ladesperre on, PI off */
double *bm_param_ptr(struct bm_cfg *cfg, int i);
int bm_param_set(struct bm_cfg *cfg, int i, double v);         /* 0 = set, -1 = outside min..max (unchanged) */

/* everything one cycle reads; times are monotonic seconds like now, 0 = never received */
struct bm_in {
	double now;              /* monotonic s */
	time_t t;                /* unix time; local month/hour via TZ (Europe/Berlin) */
	int have_pcc, have_pv;
	double pcc, pv, bat1, pcc_avg5, bat1_avg5, inv_time;   /* W, PCC + = export, Bat1 + = charge */
	int r290_hz;
	double r290_time;
	double aussen, aussen_time;                             /* degC */
	double wp, wp_time;                                     /* SDM72D heat pump W */
	int season;                                             /* batmonitor/season: 0 none, BM_SUMMER/WINTER/TRANSITION */
	time_t season_ts;                                       /* its "ts" (unix) */
	double fc_target;                                       /* batmonitor/forecast target_soc %, < 0 = none */
	time_t fc_ts;
	int ac_ok[NPH];
	double ac_in[NPH];                                      /* vebus /Ac/ActiveIn/Lx/P, + = the Multi takes */
	int bms_ok[NBANK];                                      /* Connected == 1 and /Soc valid */
	double soc[NBANK];
	int power_ok[NBANK];
	double power[NBANK];                                    /* BMS /Dc/0/Power, + = charge */
};

struct bm_ls { int active, peak_h, win_end_h; double dc, ratio, ratio_now, noon_h; };
struct bm_pi { double y, e, applied, u, u_min, u_max; int sp[NPH]; };

/* what survives from cycle to cycle */
struct bm_state {
	int prot[NBANK], force[NBANK], soc_ok[NBANK];
	double soc[NBANK];
	int lead;                                    /* bank more than SOC_BALANCE_ON ahead, -1 = none */
	int soyo_chg_prev, soyo_prop_prev;
	int ls_yday, peak_today, ls_latched, badweather_today;
	int fc_active;                               /* forecast: stacks above the target SoC -> serve everything */
	double pi_e_prev, pi_t_prev;
	int setpoints[NPH];                          /* sent last cycle (the PI's u_applied) */
};

struct bm_out {
	int sp[NPH];
	char why[NPH][WHY_LEN];
	double surplus, wp_eff;
	int season, season_measured;                 /* BM_SUMMER/WINTER/TRANSITION used; 1 = from batmonitor/season */
	int fc_active;
	double fc_target, fc_min_soc;
	struct bm_ls ls;
	struct bm_pi pi;
	int lead_event;                              /* 1 = a bank took the lead, -1 = back within SOC_BALANCE_OFF */
	double lead_diff;
};

void bm_init(struct bm_state *st);
void bm_step(const struct bm_cfg *cfg, const struct bm_in *in, struct bm_state *st, struct bm_out *out);

/* clear-sky model (fox2db_logic.h), exposed for tests */
void bm_sun_pos(time_t t, double *elev, double *az);
double bm_dc_now(time_t t, int month);

#endif
