/*
 * bm_logic: the pure control logic of batmonitor (one cycle = bm_step), without D-Bus, MQTT, clock or files.
 * batmonitor.c collects the inputs, calls bm_step and writes setpoints, log and state file;
 * tests/test_logic.c drives it with a plant model (make test, runs on any host).
 */
#ifndef BM_LOGIC_H
#define BM_LOGIC_H

#include <time.h>

#ifdef __cplusplus
extern "C" {                         /* the ESP32 shadow (ESPHome, C++) calls the C logic */
#endif

#define NPH 3
#define NBANK 2
#define CYCLE_SECONDS 5
#define WHY_LEN 96
#define BM_SUMMER 1
#define BM_WINTER 2
#define BM_TRANSITION 3
#define BM_NOMINAL_V 51.2          /* 16S LFP, for BMS current limits without a voltage */

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
	double soyo_target;
	double fc_hyst;
	double sofar_trickle;
	double charger_a, charge_eff;   /* real charge ceiling per phase = charger_a x BMS voltage / charge_eff (0.29-c) */
	double charger_dc_w;            /* measured DC at the BMS per MultiPlus at its limit, full_at forecast (0.31-c) */
	double bat1_soc_min;            /* night: the Sofar battery serves house + WP first while above this % (0.38-c) */
	double fc_bad_target, wp_bat_share_bad;   /* bad forecast (target_soc >= this): stacks cover at most this % of the WP (0.37-c) */
	double export_cap, house_est;   /* summer peak window (0.49-c): charge only the export above this W; house load guess */
	double do4_grace_s, do4_hard_w; /* DO4 only after PCC > 20 kW for this long, at once above this (0.50-c) */
	double acout_feed_max_w, acout_feed_s;   /* DO4 when PV feeds more than this into one AC-out for this long (0.51-c) */
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
	int have_soc_bat1;
	double soc_bat1;                                        /* % Sofar Bat1 (SOC_Bat1), 0.38-c */
	int r290_hz;
	double r290_time;
	double aussen, aussen_time;                             /* degC */
	double wp, wp_time;                                     /* SDM72D heat pump W */
	int season;                                             /* batmonitor/season: 0 none, BM_SUMMER/WINTER/TRANSITION */
	time_t season_ts;                                       /* its "ts" (unix) */
	double fc_target;                                       /* batmonitor/forecast target_soc %, < 0 = none */
	time_t fc_ts;
	double fc_corr;                                         /* forecast.py correction, <= 0 = none (0.49-c) */
	int fc_n;                                               /* OWM 3 h slots of today / tomorrow (kt_slots) */
	time_t fc_slot_t[24];
	double fc_slot_kt[24];
	int ac_ok[NPH];
	double ac_in[NPH];                                      /* vebus /Ac/ActiveIn/Lx/P, + = the Multi takes */
	int grid_ok;                                            /* vebus /Ac/ActiveIn/Connected == 1: grid there, transfer
	                                                           relay closed (0.51-c: AC-out overfeed only matters off-grid) */
	int ac_out_ok[NPH];
	double ac_out[NPH];                                     /* vebus /Ac/Out/Lx/P: load on AC-out1 (0.36-c: read only,
	                                                           not used by the logic until critical loads hang there) */
	int bms_ok[NBANK];                                      /* Connected == 1 and /Soc valid */
	double soc[NBANK];
	int power_ok[NBANK];
	double power[NBANK];                                    /* BMS /Dc/0/Power, + = charge */
	int volt_ok[NBANK];
	double volt[NBANK];                                     /* BMS /Dc/0/Voltage */
	int lim_ok[NBANK];                                      /* the BMS sends /Info/MaxCharge/DischargeCurrent */
	double ccl[NBANK], dcl[NBANK];                          /* A DC (CAN 0x351 of the MUST: 100 A, tapering to 0 near full) */
};

struct bm_ls { int active, cap, peak_h, win_end_h; double dc, ratio, ratio_now, noon_h; };   /* active = waiting below the cap */
struct bm_pi { double y, e, applied, u, u_min, u_max; int sp[NPH]; };

/* what survives from cycle to cycle */
struct bm_state {
	int prot[NBANK], force[NBANK], soc_ok[NBANK];
	double soc[NBANK];
	int lead;                                    /* bank more than SOC_BALANCE_ON ahead, -1 = none */
	int soyo_chg_prev, soyo_prop_prev;
	int ls_yday, peak_today;                     /* local day of the peak window, PCC > 20 kW seen today */
	int fc_active;                               /* forecast: stacks above the target SoC -> serve everything */
	int bat1_first;                              /* Sofar Bat1 above BAT1_SOC_MIN: it discharges first (0.38-c) */
	double do4_last;                             /* monotonic s of the last DO4 pulse, 0 = none (0.46-c) */
	double do4_over_since;                       /* monotonic s since the PCC is above PCC_PEAK_TH, 0 = not (0.50-c) */
	double acout_over_since;                     /* monotonic s since an AC-out feeds above ACOUT_FEED_MAX_W (0.51-c) */
	double pi_e_prev, pi_t_prev;
	int setpoints[NPH];                          /* sent last cycle (the PI's u_applied) */
};

struct bm_out {
	int sp[NPH];
	char why[NPH][WHY_LEN];
	double surplus, wp_eff;
	int season, season_measured;                 /* BM_SUMMER/WINTER/TRANSITION used; 1 = from batmonitor/season */
	int fc_active;
	int fc_bad;                                  /* forecast fresh and target_soc >= FC_BAD_TARGET: WP share capped */
	int bat1_first;
	char wp_why[8], src_why[8];                  /* S2 / S3 reason (0.42-c), see the rule text in bm_logic.c */
	double fc_target, fc_min_soc;
	struct bm_ls ls;
	struct bm_pi pi;
	int lead_event;                              /* 1 = a bank took the lead, -1 = back within SOC_BALANCE_OFF */
	int do4_pulse;                               /* 1 = send the DO4 pulse "curtail WR2" now (D5, 0.46-c) */
	int do4_acout;                               /* ... because of the AC-out feed (0.51-c), else the PCC */
	double acout_feed_w;                         /* largest PV feed into one AC-out this cycle (W, >= 0) */
	double lead_diff;
	double chg_cap[NPH];                         /* W AC: charge ceiling per phase used this cycle */
	double dis_cap[NPH];                         /* W AC: discharge ceiling per phase used this cycle */
	int ccl_bind[NPH], dcl_bind[NPH];            /* 1 = the BMS current limit is below the charger / phase limit */
};

/* ---- decision layer (0.43-c): the discrete part of the rule chain as pure functions of predicates ("bits", each a
   threshold comparison of the inputs) and the hysteresis state bits; the amounts (W) stay in bm_step. Every
   input / state combination of each function is enumerated by tests/check_matrix.c (make check). */
enum bm_wp_reason { BM_WP_NA, BM_WP_OFF, BM_WP_FC, BM_WP_FCBAD, BM_WP_Z2, BM_WP_T1900, BM_WP_ALL, BM_WP_N };
enum bm_src_reason { BM_SRC_DAY, BM_SRC_B1, BM_SRC_STK, BM_SRC_B1W, BM_SRC_N };
enum bm_mode { BM_M_BMS_MISSING, BM_M_FORCE, BM_M_STALE, BM_M_LADESPERRE, BM_M_CHARGE, BM_M_FULL, BM_M_PROT,
			   BM_M_CHARGING, BM_M_DIS, BM_M_N };
extern const char *BM_WP_NAME[BM_WP_N], *BM_SRC_NAME[BM_SRC_N], *BM_MODE_NAME[BM_M_N];

/* Discharge matrix (0.45-c): what the two decision axes of D1 mean for the discharge amount, one table row each.
   The amount code reads nothing else of D1. S3 source -> how the Sofar Bat1 counts, the target, the idle power, the
   PROP hold; S2 heat pump kind -> the part of the heat pump the stacks cover. Enumerated by make check */
enum bm_wp_kind { BM_WPK_NONE, BM_WPK_CAP, BM_WPK_ALL, BM_WPK_BAD, BM_WPK_N };
struct bm_src_rule {
	int b1_signed;              /* 1: a Bat1 discharge is house load the stacks take over; 0: only its charge counts */
	int trickle;                /* target + SOFAR_TRICKLE: the Sofar battery charges a few W steadily */
	int idle_w;                 /* discharge W while not PROPORTIONAL */
	int prop_hold;              /* IDLE -> PROPORTIONAL already above SOYO_HOLD_TH, not only on import */
};
extern const struct bm_src_rule BM_SRC_RULE[BM_SRC_N];

/* Mode matrix (0.48-c): what the S1 mode of a bank (D2) means for its phases' setpoint, one row per mode */
enum bm_action { BM_A_ZERO, BM_A_FORCE, BM_A_CHARGE, BM_A_DISCHARGE };
enum bm_why { BM_W_NONE, BM_W_NAME, BM_W_SOC, BM_W_PEAK_H, BM_W_POWER, BM_W_CHAIN };
struct bm_mode_rule {
	int action;                 /* enum bm_action: 0 W, FORCE_CHARGE_W, the charge amount, the discharge amount */
	int capped;                 /* the setpoint is limited by the phase ceiling (chg_cap / dis_cap) */
	int why;                    /* enum bm_why: rule text NAME, NAME(soc %), NAME(peak h), NAME(BMS W), NAME WP: SRC:,
								   none (the discharge stage writes it) */
};
extern const struct bm_mode_rule BM_MODE_RULE[];
extern const char *BM_WPK_NAME[BM_WPK_N];

/* D1: forecast rule, S3 source, S2 scope. State: fc_active, bat1_first */
struct bm_d1_in {
	int season;                 /* base season BM_SUMMER / BM_WINTER / BM_TRANSITION (measured, else months) */
	int night;                  /* have_pv && pv < NIGHT_PV_TH */
	int wp_fresh;               /* em0/power younger than WP_MAX_AGE */
	int wp_pos, wp_on;          /* ... and > 0 W / >= WP_ON_TH */
	int wp_running;             /* R290 compressor > 0 Hz, data fresh */
	int wp_gt_trans;            /* wp > WP_BAT_MAX_TRANSITION (reason text only) */
	int fc_share_lt_share;      /* WP_BAT_SHARE_BAD % of wp < the base season's share */
	int fc_fresh;               /* forecast on, target_soc present, younger than FC_MAX_AGE */
	int fc_minsoc_ok;           /* lowest stack SoC <= 100 (a BMS present) */
	int fc_above_on, fc_below_off;   /* lowest SoC > target + FC_HYST / < target */
	int fc_target_bad;          /* target_soc >= FC_BAD_TARGET */
	int b1_above_min;           /* SOC_Bat1 present and > BAT1_SOC_MIN */
	int b1_ge_release;          /* SOC_Bat1 >= BAT1_SOC_MIN + BAT1_FIRST_HYST */
};
struct bm_d1_out {
	int fc_active, bat1_first;  /* next state */
	int fc_ok, fc_bad, season_wp, winter, b1_first, night_floor, wp_fc_capped, wp_cap_armed;
	int wp_reason, src_reason;
	int wp_kind;                /* enum bm_wp_kind: S2 row of the discharge matrix */
};
void bm_d1(const struct bm_d1_in *b, int fc_active, int bat1_first, struct bm_d1_out *o);

/* D2: S0 SoC protection / force charge per bank and the S1 mode per bank. State: prot[], force[], chg_prev */
struct bm_d2_bank {
	int bms_ok;
	int soc_lt_min, soc_ge_release;              /* SoC < SOC_MIN / >= SOC_MIN_RELEASE */
	int soc_lt_force, soc_ge_force_release;      /* SoC < SOC_FORCE / >= SOC_FORCE_RELEASE */
	int soc_lt_100;
	int charging;                                /* BMS power present and > CHARGING_TH */
};
struct bm_d2_in {
	int stale;                                   /* inverter data older than STALE_SECONDS */
	int block;                                   /* summer charge block (ladesperre) */
	int surplus_gt_on, surplus_gt_hold;          /* Z2 surplus > PCC_SURPLUS_TH / > SOYO_HOLD_TH */
	struct bm_d2_bank bank[NBANK];
};
struct bm_d2_out {
	int prot[NBANK], force[NBANK], chg_prev;     /* next state */
	int charge_mode;
	int mode[NBANK];                             /* enum bm_mode, the same for all phases of a bank */
};
void bm_d2(const struct bm_d2_in *b, const int *prot, const int *force, int chg_prev, struct bm_d2_out *o);

/* SoC balancing lead. State: lead (-1 none, 0, 1) */
struct bm_dl_in {
	int both_ok;                                 /* both BMS present */
	int diff_gt_on, diff_lt_off;                 /* |SoC0 - SoC1| > SOC_BALANCE_ON / < SOC_BALANCE_OFF */
	int first_ahead;                             /* SoC0 > SoC1 */
};
int bm_dlead(const struct bm_dl_in *b, int lead, int *event);        /* next lead; event 1 = new lead, -1 = cleared */

/* D3: S4 discharge PROPORTIONAL or IDLE. State: prop_prev */
struct bm_d3_in {
	int any_dis;                                 /* a phase discharges (mode DIS) */
	int house_import;                            /* Z2 house balance < PCC_IMPORT_TH */
	int deficit_gt_hold;                         /* deficit > SOYO_HOLD_TH */
	int prop_hold;                               /* BM_SRC_RULE[src].prop_hold */
};
int bm_d3(const struct bm_d3_in *b, int prop_prev, int *prop_next);   /* 1 = PROPORTIONAL, 0 = IDLE */

/* D4 (0.49-c): the summer peak window as a forecast export cap. While the forecast (clear sky x OWM slot x correction,
   anchored to the measured PV) still expects export above EXPORT_CAP today, the stacks charge only the export above
   the cap (charge target PCC = +EXPORT_CAP) and wait below it (block -> mode LADESPERRE, 0 W); so their capacity is
   left for the afternoon peaks instead of being full at noon (summer 2026: DO4 on 55 days, 11:45-16:15).
   State: peak_today (PCC > 20 kW seen, display; reset at local midnight) */
struct bm_d4_in {
	int new_day;                                 /* local day changed since the last cycle */
	int enabled;                                 /* BATMONITOR_LADESPERRE and EXPORT_CAP > 0 */
	int season;                                  /* LADESPERRE_FROM..TO (May..August) */
	int fc_cap_ahead;                            /* forecast export above EXPORT_CAP from now until the evening */
	int pcc_peak;                                /* PCC fresh and > PCC_PEAK_TH */
	int surplus_cap_on, surplus_cap_hold;        /* surplus - EXPORT_CAP > PCC_SURPLUS_TH / > SOYO_HOLD_TH */
	int chg_prev;                                /* a stack charged last cycle */
};
struct bm_d4_state { int peak_today; };
int bm_d4(const struct bm_d4_in *b, const struct bm_d4_state *s, struct bm_d4_state *next, int *cap_active);
/* returns block (wait below the cap); *cap_active = the charge target is the cap */

/* D5 (0.46-c, grace 0.50-c): DO4 pulse "curtail WR2" (MQTT pv_relay/DO4, FoxESS shedding) when the PCC export stays
   above PCC_PEAK_TH 20 kW for DO4_GRACE_S (user 2026-10-10: R290 boost and attic air conditioning first, their
   minute crons need the time), at once above DO4_HARD_W; at most one pulse per DO4_LOCKOUT. State: the time of the
   last pulse and the time since the PCC is above 20 kW, reduced to the bits lockout_over and grace_over */
struct bm_d5_in {
	int pcc_over;                                /* PCC younger than DO4_PCC_MAX_AGE and > PCC_PEAK_TH */
	int grace_over;                              /* ... continuously for DO4_GRACE_S */
	int pcc_hard;                                /* ... and > DO4_HARD_W */
	int lockout_over;                            /* no pulse yet, or the last one DO4_LOCKOUT ago */
	int acout_over;                              /* 0.51-c: grid offline and PV feeds more than ACOUT_FEED_MAX_W into
	                                                one MultiPlus AC-out for ACOUT_FEED_S (factor 1.0 rule, 5000 VA per
	                                                unit): the FoxESS on AC-out1 above 15 kW; at once, no grace. With the
	                                                grid there the feed passes the transfer relay to the grid (bypass) */
};
int bm_d5(const struct bm_d5_in *b);                                  /* 1 = pulse now */
#define DO4_LOCKOUT 300.0                    /* s between two pulses: WR2 needs its restart after a pulse */
#define DO4_PCC_MAX_AGE 15.0                 /* s: only a fresh PCC triggers (sofar_fast.py: every 4 s) */
#define DO4_PULSE_S 3.0                      /* s: DO4=1, then DO4=0 (as fox2db / the ESP: r4 pulse 3) */

void bm_init(struct bm_state *st);
/* charge ceiling of one phase (W AC): charger_a x BMS voltage of its bank / charge_eff, at most CHARGE_MAX_PHASE;
   without a plausible voltage CHARGER_CAP_PHASE (no spill above it any more) */
double bm_charge_cap(const struct bm_cfg *cfg, int volt_ok, double volt);
/* BMS current limit of a bank as W AC per phase (0.35-c): charge CCL x U / charge_eff, discharge DCL x U x charge_eff,
   split over the bank's phases; U = BMS voltage, BM_NOMINAL_V without a plausible one */
double bm_bms_cap_phase(const struct bm_cfg *cfg, int bank, int volt_ok, double volt, double amps, int charge);
void bm_step(const struct bm_cfg *cfg, const struct bm_in *in, struct bm_state *st, struct bm_out *out);

/* clear-sky model (fox2db_logic.h), exposed for tests */
void bm_sun_pos(time_t t, double *elev, double *az);
double bm_dc_now(time_t t, int month);

/* full_at forecast per stack (0.28-c): PV from the clear-sky model x OWM slot kt x correction (forecast.py), anchored
   to the measured Sofar PV, stepped to sunset; the charge power follows the PV change from the measured stack power */
#define BM_FC_SLOTS 24
struct bm_fc_in {
	time_t t;
	int soc_ok[NBANK];
	double soc[NBANK];
	int has_avg[NBANK];
	int volt_ok[NBANK];
	double volt[NBANK];                 /* BMS voltage: charger ceiling charger_a x U per unit */
	double avg[NBANK];                  /* 5 min mean BMS /Dc/0/Power, + = charge */
	int have_pv;
	double pv_sofar;                    /* W, measured Sofar PV1 + PV2 */
	int n_slots;
	time_t slot_t[BM_FC_SLOTS];         /* OWM 3 h slot times (forecast.py kt_slots) */
	double slot_kt[BM_FC_SLOTS];
	double corr;                        /* forecast.py correction, <= 0 = none */
	int lead;                           /* bm_state.lead: charged last */
};
struct bm_fc_out {
	time_t full_at[NBANK];              /* 0 = not full before sunset (or no forecast) */
	double soc_sunset[NBANK];           /* SoC at sunset, < 0 = no forecast */
	double anchor;                      /* measured / modelled Sofar PV now */
	double kt_now, pv_now_w, rest_kwh;  /* kt used now, modelled PV now (both inverters), PV until sunset */
	int weather;                        /* 1 = OWM slots used, 0 = monthly kt */
};
void bm_full_forecast(const struct bm_cfg *cfg, const struct bm_fc_in *in, struct bm_fc_out *out);

#ifdef __cplusplus
}
#endif

#endif
