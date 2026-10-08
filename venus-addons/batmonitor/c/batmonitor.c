/*
 * batmonitor (C port of batmonitor.py 0.15): battery protection and discharge control for a three-phase MultiPlus
 * system whose phases sit on SEPARATE battery banks. Same rules, constants, D-Bus paths, MQTT topics and state file
 * as batmonitor.py (the reference; its docstring explains every rule); differences:
 *   - one cycle every CYCLE_SECONDS = 5 s (Python: calc 60 s, send 10 s); sofar_fast.py delivers the PCC every 4 s
 *   - calc and send in the same cycle, the ETA power samples too (5 s instead of 10 s, same 300 s window)
 *   - the AC-in of the Multis is read once per cycle (Python: twice, for the surplus and for the charge block)
 *   - log line only when a rule changes or every LOG_SECONDS (else 17000 lines a day)
 *   - an inverter message with PCC null is ignored as a whole (Python: TypeError in the callback, same effect)
 *   - soyo optimised to PCC 0 W (0.18, user 2026-10-08, also in batmonitor.py 0.16): the own power of the Multis is
 *     kept and only the new part is scaled with KP, so a PCC held at 0 by the Sofar holds the setpoint instead of
 *     growing it 1 % per cycle:
 *       charge     own charge + KP * (pcc + bat1_eff - SOYO_TARGET)      (old: KP * (surplus - 200 W): +200 W export)
 *       discharge  own discharge - Bat1 charge - KP * (pcc - SOYO_TARGET) (old: -KP * pcc, back to IDLE once covered)
 *     both hold down to SOYO_HOLD_TH; entry thresholds unchanged (+200 W surplus, -100 W import)
 *   - discharge regulated on the Z2 point (0.19, user 2026-10-08): the Sofar PCC sits at Z1 and includes the heat pump
 *     (measured: WP 4.1 kW at night -> Sofar load +3.3 kW). The heat pump shall take the cheap Z1 grid power, the
 *     batteries serve the expensive Z2 house power: z2 = pcc + WP (SDM72D em0/power, once a minute) replaces pcc in
 *     the discharge rule and the PI's discharge side; no night floor while the WP runs; WP_CAP only as fallback when
 *     em0/power is stale. Charging stays on the Z1 PCC (on Z2 the Sofar would refill the charging from its Bat1).
 *     Winter only (Oct-Apr, as WP_CAP): in summer mode (SUMMER_FROM..SUMMER_TO) the heat pump may and must be served
 *     100 % from the batteries, the discharge regulates on the Z1 PCC as before (user 2026-10-08).
 *   - no fixed night floor (0.25, user 2026-10-08: obsolete with the 4 s PCC over MQTT): at night the discharge
 *     regulates on PCC + signed Sofar Bat1 (the load behind the Sofar), so the Sofar battery idles and the PCC stays
 *     near 0; the 936 W floor had charged the Sofar battery with ~600 W at a 300 W house. B_NIGHT is unused.
 *   - discharge split per bank (0.26, user 2026-10-08): without a SoC lead, Stack1 (L1 alone) gives as much as
 *     Stack2 (L2+L3): L1 50 %, L2/L3 25 % each, so both 300 Ah stacks drain alike; L1 above DISCHARGE_MAX_PHASE
 *     (4000 W) spills to L2/L3. The SoC balancing (lead stack delivers alone) is unchanged.
 * PI prototype (not armed): BATMONITOR_PI=1 lets a velocity-form PI on y = PCC + Sofar Bat1 replace the soyo
 * discharge/charge amounts (same gates, same phase split). Without it the PI runs in shadow: every cycle one line
 * in PI_SHADOW_FILE with what soyo set and what the PI would set, to compare both before arming it.
 * Build inside Venus (gcc + dbus headers are in the image; libmosquitto/libcjson only as .so.1): see Makefile.
 * The control arithmetic lives in bm_logic.c (bm_step, pure: no D-Bus, MQTT, clock or files) and is tested on any
 * host with tests/test_logic.c (make test); this file only reads the inputs, calls bm_step and writes the outputs.
 * Build inside Venus (gcc + dbus headers are in the image; libmosquitto/libcjson only as .so.1): see Makefile.
 */
#define _GNU_SOURCE
#include <dbus/dbus.h>
#include <math.h>
#include <mosquitto.h>
#include <cjson/cJSON.h>
#include <pthread.h>
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include "bm_logic.h"

#define VERSION "0.26-c"
#define INVERTER_TOPIC "inverter/power_grid_exchange/json"
#define R290_TOPIC "r290/heatpump/all"
#define AUSSEN_TOPIC "aussen/temp"
#define WP_TOPIC "em0/power"          /* SDM72D, heat pump electrical power in W (sdm72d.py on .218, once a minute) */
#define SEASON_TOPIC "batmonitor/season"   /* season.py on .218, hourly: {"mode": "summer"|"winter", ..., "ts"} */
#define FORECAST_TOPIC "batmonitor/forecast"   /* forecast.py on .218, hourly: {"target_soc", ..., "ts"} */
#define STATE_FILE_DEFAULT "/data/batmonitor/state.json"
#define TZ_BERLIN "CET-1CEST,M3.5.0,M10.5.0/3"   /* Europe/Berlin without a zoneinfo file */
#define SETPOINT_MAX_AGE 90.0
#define LOG_SECONDS 60.0
#define ETA_WINDOW 300.0
#define ETA_MIN_W 50.0
#define ETA_MAX_N 128
#define PI_SHADOW_FILE "/data/batmonitor/pi_shadow.csv"
#define PI_SHADOW_MAX 8000000L  /* bytes, then renamed to .1: CSV <= 16 MB, + multilog 4 x 1 MB = 20 MB (user) */

static int LIVE, hub4mode_set;
static struct bm_cfg cfg;
static struct bm_state st;
static struct bm_out o_;                            /* result of the last bm_step */
static char STATE_FILE[256] = STATE_FILE_DEFAULT;   /* BATMONITOR_STATE_FILE: other path for a dry test run */
static volatile sig_atomic_t stop_flag;

/* MQTT inputs, written by the mosquitto thread under mq_lock */
static pthread_mutex_t mq_lock = PTHREAD_MUTEX_INITIALIZER;
static struct {
	int have_pcc, have_pv;
	double pcc, pv, bat1, pcc_avg5, bat1_avg5, inv_time;
	int r290_hz;
	double r290_time;
	double aussen, aussen_time;
	double wp, wp_time;
	int season;
	double season_ts;
	double fc_target, fc_ts;
} mq;

static double setpoint_time;
static struct { double t, p; } hist[NBANK][ETA_MAX_N];
static int hist_n[NBANK];

static DBusConnection *bus;

static double mono(void)
{
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return ts.tv_sec + ts.tv_nsec / 1e9;
}

static void logf_(const char *lvl, const char *fmt, ...)
{
	va_list ap;
	printf("%s ", lvl);
	va_start(ap, fmt);
	vprintf(fmt, ap);
	va_end(ap);
	putchar('\n');
	fflush(stdout);
}
#define LOG(...) logf_("INFO", __VA_ARGS__)
#define LOGE(...) logf_("ERROR", __VA_ARGS__)

/* ---- D-Bus ------------------------------------------------------------------------------------- */

/* GetValue of a Victron BusItem; 0 = number in *out, -1 = missing service/path, empty array or no number */
static int get(const char *service, const char *path, double *out)
{
	DBusMessage *m, *r;
	DBusMessageIter it, v;
	DBusError err;
	int ok = -1;
	if (!service)
		return -1;
	m = dbus_message_new_method_call(service, path, "com.victronenergy.BusItem", "GetValue");
	if (!m)
		return -1;
	dbus_error_init(&err);
	r = dbus_connection_send_with_reply_and_block(bus, m, 2000, &err);
	dbus_message_unref(m);
	if (!r) {
		dbus_error_free(&err);
		return -1;
	}
	if (dbus_message_iter_init(r, &it) && dbus_message_iter_get_arg_type(&it) == DBUS_TYPE_VARIANT) {
		dbus_message_iter_recurse(&it, &v);
		union { double d; dbus_int16_t i16; dbus_uint16_t u16; dbus_int32_t i32; dbus_uint32_t u32;
				dbus_int64_t i64; dbus_uint64_t u64; unsigned char b; dbus_bool_t bo; } u;
		ok = 0;
		switch (dbus_message_iter_get_arg_type(&v)) {
		case DBUS_TYPE_DOUBLE: dbus_message_iter_get_basic(&v, &u); *out = u.d; break;
		case DBUS_TYPE_INT16: dbus_message_iter_get_basic(&v, &u); *out = u.i16; break;
		case DBUS_TYPE_UINT16: dbus_message_iter_get_basic(&v, &u); *out = u.u16; break;
		case DBUS_TYPE_INT32: dbus_message_iter_get_basic(&v, &u); *out = u.i32; break;
		case DBUS_TYPE_UINT32: dbus_message_iter_get_basic(&v, &u); *out = u.u32; break;
		case DBUS_TYPE_INT64: dbus_message_iter_get_basic(&v, &u); *out = (double)u.i64; break;
		case DBUS_TYPE_UINT64: dbus_message_iter_get_basic(&v, &u); *out = (double)u.u64; break;
		case DBUS_TYPE_BYTE: dbus_message_iter_get_basic(&v, &u); *out = u.b; break;
		case DBUS_TYPE_BOOLEAN: dbus_message_iter_get_basic(&v, &u); *out = u.bo; break;
		default: ok = -1;   /* empty array = invalid, strings are no numbers */
		}
	}
	dbus_message_unref(r);
	return ok;
}

static void set_int(const char *service, const char *path, int value)
{
	DBusMessage *m, *r;
	DBusMessageIter it, v;
	DBusError err;
	dbus_int32_t x = value;
	if (!LIVE || !service)
		return;
	m = dbus_message_new_method_call(service, path, "com.victronenergy.BusItem", "SetValue");
	if (!m)
		return;
	dbus_message_iter_init_append(m, &it);
	dbus_message_iter_open_container(&it, DBUS_TYPE_VARIANT, DBUS_TYPE_INT32_AS_STRING, &v);
	dbus_message_iter_append_basic(&v, DBUS_TYPE_INT32, &x);
	dbus_message_iter_close_container(&it, &v);
	dbus_error_init(&err);
	r = dbus_connection_send_with_reply_and_block(bus, m, 2000, &err);
	dbus_message_unref(m);
	if (!r) {
		LOGE("SetValue %s %s: %s", service, path, err.message ? err.message : "?");
		dbus_error_free(&err);
		return;
	}
	dbus_message_unref(r);
}

/* first com.victronenergy.vebus.* on the bus, NULL if none */
static const char *vebus(void)
{
	static char name[128];
	DBusMessage *m, *r;
	DBusMessageIter it, a;
	DBusError err;
	const char *found = NULL;
	m = dbus_message_new_method_call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "ListNames");
	if (!m)
		return NULL;
	dbus_error_init(&err);
	r = dbus_connection_send_with_reply_and_block(bus, m, 2000, &err);
	dbus_message_unref(m);
	if (!r) {
		dbus_error_free(&err);
		return NULL;
	}
	if (dbus_message_iter_init(r, &it) && dbus_message_iter_get_arg_type(&it) == DBUS_TYPE_ARRAY) {
		dbus_message_iter_recurse(&it, &a);
		while (dbus_message_iter_get_arg_type(&a) == DBUS_TYPE_STRING) {
			const char *s;
			dbus_message_iter_get_basic(&a, &s);
			if (strncmp(s, "com.victronenergy.vebus.", 24) == 0) {
				snprintf(name, sizeof(name), "%s", s);
				found = name;
				break;
			}
			dbus_message_iter_next(&a);
		}
	}
	dbus_message_unref(r);
	return found;
}

/* ---- MQTT (mosquitto thread) -------------------------------------------------------------------- */

/* ---- MQTT (mosquitto thread) -------------------------------------------------------------------- */

static double jnum(const cJSON *o, const char *k, double dflt, int *is_null)
{
	const cJSON *x = cJSON_GetObjectItemCaseSensitive(o, k);
	if (is_null)
		*is_null = cJSON_IsNull(x);
	return cJSON_IsNumber(x) ? x->valuedouble : dflt;
}

static void on_connect(struct mosquitto *m, void *ud, int rc)
{
	(void)ud;
	LOG("MQTT connected (%d)", rc);
	if (rc == 0) {
		mosquitto_subscribe(m, NULL, INVERTER_TOPIC, 0);
		mosquitto_subscribe(m, NULL, R290_TOPIC, 0);
		mosquitto_subscribe(m, NULL, AUSSEN_TOPIC, 0);
		mosquitto_subscribe(m, NULL, WP_TOPIC, 0);
		mosquitto_subscribe(m, NULL, SEASON_TOPIC, 0);
		mosquitto_subscribe(m, NULL, FORECAST_TOPIC, 0);
	}
}

static void on_message(struct mosquitto *m, void *ud, const struct mosquitto_message *msg)
{
	(void)m; (void)ud;
	char buf[2048];
	int n = msg->payloadlen < (int)sizeof(buf) - 1 ? msg->payloadlen : (int)sizeof(buf) - 1;
	memcpy(buf, msg->payload, n);
	buf[n] = 0;
	if (strcmp(msg->topic, WP_TOPIC) == 0) {
		char *end;
		double v = strtod(buf, &end);
		if (end != buf && v > -1000.0 && v < 30000.0) {
			pthread_mutex_lock(&mq_lock);
			mq.wp = v;
			mq.wp_time = mono();
			pthread_mutex_unlock(&mq_lock);
		}
		return;
	}
	if (strcmp(msg->topic, AUSSEN_TOPIC) == 0) {
		char *end;
		double v = strtod(buf, &end);
		if (end != buf && v > -40.0 && v < 55.0) {
			pthread_mutex_lock(&mq_lock);
			mq.aussen = v;
			mq.aussen_time = mono();
			pthread_mutex_unlock(&mq_lock);
		}
		return;
	}
	cJSON *d = cJSON_Parse(buf);
	if (!cJSON_IsObject(d)) {
		LOGE("bad payload on %s", msg->topic);
		cJSON_Delete(d);
		return;
	}
	if (strcmp(msg->topic, INVERTER_TOPIC) == 0) {
		int pcc_null, p5_null, b5_null;
		double pcc = jnum(d, "ActivePower_PCC_Total", 0, &pcc_null);
		if (!pcc_null) {
			double pcc5 = jnum(d, "ActivePower_PCC_Total_avg5", 0, &p5_null);
			double bat1 = jnum(d, "Power_Bat1", 0, NULL), bat5 = jnum(d, "Power_Bat1_avg5", 0, &b5_null);
			int have5 = cJSON_GetObjectItemCaseSensitive(d, "ActivePower_PCC_Total_avg5") && !p5_null;
			int haveb5 = cJSON_GetObjectItemCaseSensitive(d, "Power_Bat1_avg5") && !b5_null;
			pthread_mutex_lock(&mq_lock);
			mq.pcc = pcc * 1000.0;
			mq.bat1 = bat1 * 1000.0;
			mq.pv = (jnum(d, "Power_PV1", 0, NULL) + jnum(d, "Power_PV2", 0, NULL)) * 1000.0;
			mq.pcc_avg5 = (have5 ? pcc5 : pcc) * 1000.0;
			mq.bat1_avg5 = (haveb5 ? bat5 : bat1) * 1000.0;
			mq.have_pcc = mq.have_pv = 1;
			mq.inv_time = mono();
			pthread_mutex_unlock(&mq_lock);
		}
	} else if (strcmp(msg->topic, FORECAST_TOPIC) == 0) {
		pthread_mutex_lock(&mq_lock);
		mq.fc_target = jnum(d, "target_soc", -1, NULL);
		mq.fc_ts = jnum(d, "ts", 0, NULL);
		pthread_mutex_unlock(&mq_lock);
		LOG("forecast: %s", buf);
	} else if (strcmp(msg->topic, SEASON_TOPIC) == 0) {
		const cJSON *mo = cJSON_GetObjectItemCaseSensitive(d, "mode");
		int season = cJSON_IsString(mo) ? (strcmp(mo->valuestring, "summer") == 0 ? BM_SUMMER
										   : strcmp(mo->valuestring, "winter") == 0 ? BM_WINTER
										   : strcmp(mo->valuestring, "transition") == 0 ? BM_TRANSITION : 0) : 0;
		pthread_mutex_lock(&mq_lock);
		mq.season = season;
		mq.season_ts = jnum(d, "ts", 0, NULL);
		pthread_mutex_unlock(&mq_lock);
		LOG("season: %s (%s)", season == BM_SUMMER ? "summer" : season == BM_WINTER ? "winter"
			: season == BM_TRANSITION ? "transition" : "?", buf);
	} else if (strcmp(msg->topic, R290_TOPIC) == 0) {
		pthread_mutex_lock(&mq_lock);
		mq.r290_hz = (int)jnum(d, "comp_freq_actual", 0, NULL);
		mq.r290_time = mono();
		pthread_mutex_unlock(&mq_lock);
	}
	cJSON_Delete(d);
}

/* ---- outputs ------------------------------------------------------------------------------------ */

static void sample_power(void)
{
	double now = mono();
	for (int b = 0; b < NBANK; b++) {
		double pw;
		int k = 0;
		if (get(BM_BANKS[b].service, "/Dc/0/Power", &pw) == 0) {
			if (hist_n[b] == ETA_MAX_N) {
				memmove(hist[b], hist[b] + 1, (ETA_MAX_N - 1) * sizeof(hist[b][0]));
				hist_n[b]--;
			}
			hist[b][hist_n[b]].t = now;
			hist[b][hist_n[b]].p = pw;
			hist_n[b]++;
		}
		while (k < hist_n[b] && now - hist[b][k].t > ETA_WINDOW)
			k++;
		if (k) {
			memmove(hist[b], hist[b] + k, (hist_n[b] - k) * sizeof(hist[b][0]));
			hist_n[b] -= k;
		}
	}
}

/* per stack: average charge power over ETA_WINDOW and the time 100 % SoC is reached at that power (0 = none) */
static void full_forecast(int b, int *has_avg, double *avg, long *full_at)
{
	double s = 0;
	*has_avg = hist_n[b] > 0;
	*full_at = 0;
	for (int i = 0; i < hist_n[b]; i++)
		s += hist[b][i].p;
	*avg = *has_avg ? s / hist_n[b] : 0;
	if (*has_avg && st.soc_ok[b] && *avg >= ETA_MIN_W && st.soc[b] < 100)
		*full_at = (long)(time(NULL) + (100 - st.soc[b]) / 100 * BM_BANKS[b].capacity_wh / *avg * 3600);
}

static void write_state(const int *sp, char why[][WHY_LEN], double surplus)
{
	cJSON *js = cJSON_CreateObject(), *o;
	char tmp[264];
	snprintf(tmp, sizeof(tmp), "%s.tmp", STATE_FILE);
	cJSON_AddNumberToObject(js, "ts", (double)time(NULL));
	cJSON_AddStringToObject(js, "version", VERSION);
	o = cJSON_AddObjectToObject(js, "sp");
	for (int p = 0; p < NPH; p++)
		cJSON_AddNumberToObject(o, BM_PH[p], sp[p]);
	o = cJSON_AddObjectToObject(js, "rule");
	for (int p = 0; p < NPH; p++) {
		char r[49];
		snprintf(r, sizeof(r), "%.48s", why[p]);
		cJSON_AddStringToObject(o, BM_PH[p], r);
	}
	if (st.lead >= 0)
		cJSON_AddStringToObject(js, "balance_lead", BM_BANKS[st.lead].name);
	else
		cJSON_AddNullToObject(js, "balance_lead");
	cJSON_AddNumberToObject(js, "surplus_w", round(surplus));
	o = cJSON_AddObjectToObject(js, "prot");
	for (int b = 0; b < NBANK; b++)
		cJSON_AddNumberToObject(o, BM_BANKS[b].name, st.prot[b]);
	o = cJSON_AddObjectToObject(js, "force");
	for (int b = 0; b < NBANK; b++)
		cJSON_AddNumberToObject(o, BM_BANKS[b].name, st.force[b]);
	o = cJSON_AddObjectToObject(js, "forecast");
	for (int b = 0; b < NBANK; b++) {
		int has;
		double avg;
		long full;
		full_forecast(b, &has, &avg, &full);
		cJSON *f = cJSON_AddObjectToObject(o, BM_BANKS[b].name);
		if (has)
			cJSON_AddNumberToObject(f, "avg5_w", round(avg));
		else
			cJSON_AddNullToObject(f, "avg5_w");
		if (full)
			cJSON_AddNumberToObject(f, "full_at", (double)full);
		else
			cJSON_AddNullToObject(f, "full_at");
	}
	o = cJSON_AddObjectToObject(js, "ladesperre");
	cJSON_AddNumberToObject(o, "active", o_.ls.active);
	cJSON_AddNumberToObject(o, "dc_expected_w", round(o_.ls.dc));
	cJSON_AddNumberToObject(o, "ratio", round(o_.ls.ratio * 1000) / 1000);
	cJSON_AddNumberToObject(o, "ratio_now", round(o_.ls.ratio_now * 1000) / 1000);
	cJSON_AddNumberToObject(o, "peak_h", o_.ls.peak_h);
	cJSON_AddNumberToObject(o, "win_end_h", o_.ls.win_end_h);
	cJSON_AddNumberToObject(o, "noon_h", round(o_.ls.noon_h * 100) / 100);
	cJSON_AddNumberToObject(o, "peak_today", st.peak_today);
	cJSON_AddNumberToObject(o, "badweather_today", st.badweather_today);
	cJSON_AddStringToObject(js, "season", o_.season == BM_WINTER ? "winter" : o_.season == BM_TRANSITION ? "transition"
							: "summer");
	cJSON_AddStringToObject(js, "season_source", o_.season_measured ? "measured" : "months");
	o = cJSON_AddObjectToObject(js, "forecast_rule");
	cJSON_AddNumberToObject(o, "active", o_.fc_active);
	cJSON_AddNumberToObject(o, "target_soc", o_.fc_target);
	cJSON_AddNumberToObject(o, "min_soc", o_.fc_min_soc);
	o = cJSON_AddObjectToObject(js, "pi");
	cJSON_AddNumberToObject(o, "armed", cfg.pi_armed);
	cJSON_AddNumberToObject(o, "y_w", round(o_.pi.y));
	cJSON_AddNumberToObject(o, "e_w", round(o_.pi.e));
	cJSON_AddNumberToObject(o, "applied_w", round(o_.pi.applied));
	cJSON_AddNumberToObject(o, "u_w", round(o_.pi.u));
	cJSON *ps = cJSON_AddObjectToObject(o, "sp");
	for (int p = 0; p < NPH; p++)
		cJSON_AddNumberToObject(ps, BM_PH[p], o_.pi.sp[p]);
	char *s = cJSON_PrintUnformatted(js);
	FILE *f = fopen(tmp, "w");
	if (!f || fputs(s, f) < 0 || fclose(f) != 0 || rename(tmp, STATE_FILE) != 0)
		LOGE("state file: write failed");
	free(s);
	cJSON_Delete(js);
}

static void pi_shadow_log(double pcc, double bat1, double pv, const int *sp, char why[][WHY_LEN])
{
	FILE *f = fopen(PI_SHADOW_FILE, "a");
	if (!f)
		return;
	if (ftell(f) == 0)
		fputs("ts,pcc_w,bat1_w,pv_w,y_w,e_w,applied_w,pi_u_w,pi_min_w,pi_max_w,"
			  "sp_l1,sp_l2,sp_l3,pi_l1,pi_l2,pi_l3,armed,rule_l1,rule_l2,rule_l3\n", f);
	fprintf(f, "%ld,%.0f,%.0f,%.0f,%.0f,%.0f,%.0f,%.0f,%.0f,%.0f,%d,%d,%d,%d,%d,%d,%d,%s,%s,%s\n", (long)time(NULL),
			pcc, bat1, pv, o_.pi.y, o_.pi.e, o_.pi.applied, o_.pi.u, o_.pi.u_min, o_.pi.u_max, sp[0], sp[1], sp[2],
			o_.pi.sp[0], o_.pi.sp[1], o_.pi.sp[2], cfg.pi_armed, why[0], why[1], why[2]);
	long size = ftell(f);
	fclose(f);
	if (size > PI_SHADOW_MAX)
		rename(PI_SHADOW_FILE, PI_SHADOW_FILE ".1");
}

/* one cycle: collect the inputs, bm_step, then log, PI shadow line and state file */
static void calc(void)
{
	static char last_rules[NPH * WHY_LEN];
	static double last_log;
	struct bm_in in;
	memset(&in, 0, sizeof(in));
	in.now = mono();
	in.t = time(NULL);
	pthread_mutex_lock(&mq_lock);
	in.have_pcc = mq.have_pcc;
	in.have_pv = mq.have_pv;
	in.pcc = mq.pcc;
	in.pv = mq.pv;
	in.bat1 = mq.bat1;
	in.pcc_avg5 = mq.pcc_avg5;
	in.bat1_avg5 = mq.bat1_avg5;
	in.inv_time = mq.inv_time;
	in.r290_hz = mq.r290_hz;
	in.r290_time = mq.r290_time;
	in.aussen = mq.aussen;
	in.aussen_time = mq.aussen_time;
	in.wp = mq.wp;
	in.wp_time = mq.wp_time;
	in.season = mq.season;
	in.season_ts = (time_t)mq.season_ts;
	in.fc_target = mq.fc_ts > 0 ? mq.fc_target : -1;
	in.fc_ts = (time_t)mq.fc_ts;
	pthread_mutex_unlock(&mq_lock);
	const char *vb = vebus();
	for (int p = 0; p < NPH; p++) {
		char path[32];
		snprintf(path, sizeof(path), "/Ac/ActiveIn/%s/P", BM_PH[p]);
		in.ac_ok[p] = vb && get(vb, path, &in.ac_in[p]) == 0;
	}
	for (int b = 0; b < NBANK; b++) {
		const char *s = BM_BANKS[b].service;
		double conn;
		in.bms_ok[b] = get(s, "/Connected", &conn) == 0 && conn == 1.0 && get(s, "/Soc", &in.soc[b]) == 0;
		in.power_ok[b] = get(s, "/Dc/0/Power", &in.power[b]) == 0;
	}

	bm_step(&cfg, &in, &st, &o_);

	if (o_.lead_event > 0)
		LOG("SoC balance: %s ahead by %.1f %%", BM_BANKS[st.lead].name, o_.lead_diff);
	else if (o_.lead_event < 0)
		LOG("SoC balance: back within 1.0 %%");
	const int *sp = o_.sp;
	pi_shadow_log(in.have_pcc ? in.pcc : 0, in.bat1, in.have_pv ? in.pv : 0, sp, o_.why);
	char rules[NPH * WHY_LEN] = "";
	for (int p = 0; p < NPH; p++) {
		strcat(rules, o_.why[p]);
		strcat(rules, ";");
	}
	if (strcmp(rules, last_rules) != 0 || in.now - last_log >= LOG_SECONDS) {
		char line[1024], eta[160] = "";
		int o = 0;
		for (int b = 0; b < NBANK; b++) {
			int has;
			double avg;
			long full;
			char hm[8] = "-";
			full_forecast(b, &has, &avg, &full);
			if (full) {
				time_t ft = full;
				struct tm fl;
				localtime_r(&ft, &fl);
				strftime(hm, sizeof(hm), "%H:%M", &fl);
			}
			o += snprintf(eta + o, sizeof(eta) - o, "%s%s full %s", b ? "  " : "", BM_BANKS[b].name, hm);
		}
		o = 0;
		for (int p = 0; p < NPH; p++)
			o += snprintf(line + o, sizeof(line) - o, "%s%s %+d W (%s)", p ? "  " : "", BM_PH[p], sp[p], o_.why[p]);
		char spcc[16] = "None", spv[16] = "None";
		if (in.have_pcc)
			snprintf(spcc, sizeof(spcc), "%.0f", in.pcc);
		if (in.have_pv)
			snprintf(spv, sizeof(spv), "%.0f", in.pv);
		LOG("%spcc %s W wp %.0f W bat1 %.0f W pv %s W -> %s | %s | PI%s u %+.0f W", LIVE ? "" : "DRY ", spcc, o_.wp_eff,
			in.bat1, spv, line, eta, cfg.pi_armed ? "" : " shadow", o_.pi.u);
		strcpy(last_rules, rules);
		last_log = in.now;
	}
	setpoint_time = in.now;
	write_state(sp, o_.why, o_.surplus);
}

static void send_setpoints(void)
{
	int sp[NPH];
	sample_power();
	const char *vb = vebus();
	if (!vb)
		return;
	memcpy(sp, st.setpoints, sizeof(sp));
	if (mono() - setpoint_time > SETPOINT_MAX_AGE)
		memset(sp, 0, sizeof(sp));          /* ESP TX watchdog */
	if (!hub4mode_set) {
		set_int("com.victronenergy.settings", "/Settings/CGwacs/Hub4Mode", 3);
		hub4mode_set = LIVE;
	}
	for (int p = 0; p < NPH; p++) {
		char path[48];
		snprintf(path, sizeof(path), "/Hub4/%s/MaxFeedInPower", BM_PH[p]);
		set_int(vb, path, (sp[p] < 0 ? -sp[p] : 0) + 100);
		snprintf(path, sizeof(path), "/Hub4/%s/AcPowerSetpoint", BM_PH[p]);
		set_int(vb, path, sp[p]);
	}
}

static void stop_control(void)
{
	const char *vb = vebus();
	if (vb)
		for (int p = 0; p < NPH; p++) {
			char path[48];
			snprintf(path, sizeof(path), "/Hub4/%s/AcPowerSetpoint", BM_PH[p]);
			set_int(vb, path, 0);
		}
	set_int("com.victronenergy.settings", "/Settings/CGwacs/Hub4Mode", 1);
	LOG("stopped%s", LIVE ? ", back to Hub4Mode 1" : "");
}

static void on_signal(int sig)
{
	(void)sig;
	stop_flag = 1;
}

int main(void)
{
	const char *e = getenv("BATMONITOR_LIVE"), *host = getenv("BATMONITOR_MQTT_HOST"), *l = getenv("BATMONITOR_LADESPERRE");
	DBusError err;
	LIVE = e && strcmp(e, "1") == 0;
	bm_cfg_default(&cfg);
	cfg.ladesperre = !l || strcmp(l, "1") == 0;
	const char *pe = getenv("BATMONITOR_PI");
	cfg.pi_armed = pe && strcmp(pe, "1") == 0;
	const char *fe = getenv("BATMONITOR_FORECAST");
	cfg.forecast = !fe || strcmp(fe, "1") == 0;
	bm_init(&st);
	const char *sf = getenv("BATMONITOR_STATE_FILE");
	if (sf && *sf)
		snprintf(STATE_FILE, sizeof(STATE_FILE), "%s", sf);
	if (!host || !*host)
		host = "192.168.178.218";
	setenv("TZ", TZ_BERLIN, 1);
	tzset();
	setvbuf(stdout, NULL, _IOLBF, 0);

	dbus_error_init(&err);
	bus = dbus_bus_get(DBUS_BUS_SYSTEM, &err);
	if (!bus) {
		LOGE("system bus: %s", err.message);
		return 1;
	}
	dbus_connection_set_exit_on_disconnect(bus, FALSE);
	LOG("batmonitor %s %s, cycle %d s, banks STACK1_MUST=L1, STACK2_PYTES=L2+L3, charge block %s, PI %s", VERSION,
		LIVE ? "LIVE" : "DRY RUN", CYCLE_SECONDS, cfg.ladesperre ? "on" : "off", cfg.pi_armed ? "ARMED" : "shadow");
	/* parameters: BATMONITOR_<NAME> overrides the default; out of range or not a number -> default stays */
	{
		char line[1024];
		int o = 0;
		for (int i = 0; i < BM_NPARAMS; i++) {
			char env[64], *end;
			snprintf(env, sizeof(env), "BATMONITOR_%s", BM_PARAMS[i].name);
			const char *v = getenv(env);
			int set = 0;
			if (v && *v) {
				double x = strtod(v, &end);
				if (end == v || *end || bm_param_set(&cfg, i, x) != 0)
					LOGE("%s=%s ignored (allowed %g..%g %s), default %g", env, v, BM_PARAMS[i].min, BM_PARAMS[i].max,
						 BM_PARAMS[i].unit, BM_PARAMS[i].def);
				else
					set = 1;
			}
			o += snprintf(line + o, sizeof(line) - o, "%s%s=%g%s", i ? " " : "", BM_PARAMS[i].name,
						  *bm_param_ptr(&cfg, i), set ? "*" : "");
		}
		LOG("params (* = from env): %s", line);
	}

	mosquitto_lib_init();
	/* random client id: Pi and NCR fallback (both containers are "raspberrypi4") share the broker, a fixed id
	   would make them kick each other off */
	struct mosquitto *mosq = mosquitto_new(NULL, true, NULL);
	if (!mosq) {
		LOGE("mosquitto_new failed");
		return 1;
	}
	mosquitto_connect_callback_set(mosq, on_connect);
	mosquitto_message_callback_set(mosq, on_message);
	mosquitto_reconnect_delay_set(mosq, 2, 30, true);
	if (mosquitto_connect_async(mosq, host, 1883, 60) != MOSQ_ERR_SUCCESS)
		LOGE("MQTT connect to %s failed, retrying in the loop", host);
	mosquitto_loop_start(mosq);

	struct sigaction sa = {0};
	sa.sa_handler = on_signal;
	sigaction(SIGTERM, &sa, NULL);
	sigaction(SIGINT, &sa, NULL);

	double next = mono() + CYCLE_SECONDS;   /* first cycle once the retained MQTT data is in */
	while (!stop_flag) {
		double wait = next - mono();
		if (wait > 0) {
			struct timespec ts = {(time_t)wait, (long)((wait - (time_t)wait) * 1e9)};
			nanosleep(&ts, NULL);         /* a signal ends the sleep early */
			continue;
		}
		calc();
		send_setpoints();
		while (dbus_connection_read_write_dispatch(bus, 0) && dbus_connection_get_dispatch_status(bus) == DBUS_DISPATCH_DATA_REMAINS)
			;
		next += CYCLE_SECONDS;
		if (next < mono())
			next = mono() + CYCLE_SECONDS;
	}
	stop_control();
	mosquitto_loop_stop(mosq, true);
	mosquitto_destroy(mosq);
	mosquitto_lib_cleanup();
	return 0;
}
