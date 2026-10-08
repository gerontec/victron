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
 * PI prototype (not armed): BATMONITOR_PI=1 lets a velocity-form PI on y = PCC + Sofar Bat1 replace the soyo
 * discharge/charge amounts (same gates, same phase split). Without it the PI runs in shadow: every cycle one line
 * in PI_SHADOW_FILE with what soyo set and what the PI would set, to compare both before arming it.
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

#define VERSION "0.19-c"
#define INVERTER_TOPIC "inverter/power_grid_exchange/json"
#define R290_TOPIC "r290/heatpump/all"
#define AUSSEN_TOPIC "aussen/temp"
#define WP_TOPIC "em0/power"          /* SDM72D, heat pump electrical power in W (sdm72d.py on .218, once a minute) */
#define WP_MAX_AGE 150.0              /* s, older: no Z2 correction, WP_CAP as before */
#define WP_ON_TH 300.0                /* W: heat pump counts as running (no night floor) */
#define STATE_FILE_DEFAULT "/data/batmonitor/state.json"
#define TZ_BERLIN "CET-1CEST,M3.5.0,M10.5.0/3"   /* Europe/Berlin without a zoneinfo file */

/* soyo, 1:1 from sofar_waveshare.yaml, power values doubled (POWER_SCALE) */
#define POWER_SCALE 2
#define KP 1.01
#define B_NIGHT (468 * POWER_SCALE)
#define B_DAY_IDLE (10 * POWER_SCALE)
#define W_MAX (900 * POWER_SCALE)
#define PCC_IMPORT_TH -100.0
#define PCC_SURPLUS_TH 200.0
#define SOYO_TARGET 0.0         /* W at the PCC (0.18: was +200 W export in effect) */
#define SOYO_HOLD_TH 20.0       /* W: charging / proportional discharging continues while surplus / deficit is above */
#define CHARGING_TH 200.0
#define NIGHT_PV_TH 100.0
#define WP_CAP (500 * POWER_SCALE)
#define SUMMER_FROM 5          /* SUMMER_MONTHS = range(5, 10) */
#define SUMMER_TO 9
#define STALE_SECONDS 180.0
#define SETPOINT_MAX_AGE 90.0
#define CYCLE_SECONDS 5
#define LOG_SECONDS 60.0
/* batmonitor */
#define SOC_MIN 5.0
#define SOC_MIN_RELEASE 7.0
#define SOC_FORCE 3.0
#define SOC_FORCE_RELEASE 5.0
#define FORCE_CHARGE_W 500
#define CHARGE_MAX_PHASE 4900
#define CHARGER_CAP_PHASE 4200
#define SOC_BALANCE_ON 3.0
#define SOC_BALANCE_OFF 1.0
#define BAT1_CHARGE_FACTOR 0.5
#define ETA_WINDOW 300.0
#define ETA_MIN_W 50.0
#define ETA_MAX_N 128
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
#define PI_TARGET 0.0           /* W at the PCC (user 2026-10-08: PCC near 0; soyo settles at ~+200 W export) */
#define PI_DEADBAND 75.0        /* |e| below this counts as 0 (PCC resolution 10 W, 4 s samples) */
#define PI_KP 0.2               /* on the change of e */
#define PI_KI 0.06              /* 1/s: KI * 5 s = 0.3 of the error per cycle (plant gain ~1, one cycle delay) */
#define PI_SHADOW_FILE "/data/batmonitor/pi_shadow.csv"
#define PI_SHADOW_MAX 8000000L  /* bytes, then renamed to .1: CSV <= 16 MB, + multilog 4 x 1 MB = 20 MB (user) */

#define NPH 3
#define NBANK 2
static const char *PH[NPH] = {"L1", "L2", "L3"};

struct bank {
	const char *name, *service;
	int nph, ph[NPH];
	double capacity_wh;
};
/* Stack1 MUST feeds the middle unit = HQ2606P4NCH = Devices/0 = L1 (user photo 2026-10-08) */
static const struct bank BANKS[NBANK] = {
	{"STACK1_MUST", "com.victronenergy.battery.socketcan_can0", 1, {0}, 300 * 51.2},
	{"STACK2_PYTES", "com.victronenergy.battery.ebox", 2, {1, 2}, 300 * 51.2},
};
static const int CHARGE_PRIORITY[NBANK] = {0, 1};   /* Stack1 first (one 70 A charger), then Stack2 */

struct arr { double tilt, az_s, power; };
/* fitted per string (gen_pv_strings.py, 14 clear days): Sofar PV1 west / PV2 south, FoxESS pv2 east / pv1 west */
static const struct arr ARRAYS[2] = {{60, 33, 19430}, {68, -12, 7690}};
static const struct arr ARRAYS_EAST[2] = {{59, -29, 22036}, {67, 32, 2781}};
static const double KT_MONTH[13] = {0, .331, .402, .563, .838, .909, .880, .840, .820, .760, .600, .350, .134};

static int LIVE, LADESPERRE_ENABLE, PI_ENABLE;
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
} mq;

/* controller state */
static int prot[NBANK], force_[NBANK], soc_ok[NBANK], lead = -1, hub4mode_set;
static int soyo_chg_prev, soyo_prop_prev;   /* last cycle charged from PV / discharged proportionally */
static double soc[NBANK];
static int setpoints[NPH];
static double setpoint_time;
static struct { double t, p; } hist[NBANK][ETA_MAX_N];
static int hist_n[NBANK];
/* charge block day latches (ESP State: peak_today, ladesperre_latched, badweather_today) */
static int ls_yday = -1, peak_today, ls_latched, badweather_today;
static struct { int active, peak_h, win_end_h; double dc, ratio, ratio_now, noon_h; } ls;
/* PI prototype */
static double pi_e_prev, pi_t_prev;
static struct { double y, e, applied, u, u_min, u_max; int sp[NPH]; } pi;

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

/* ---- clear-sky DC model (fox2db_logic.h) -------------------------------------------------------- */

static double d2r(double d) { return d * M_PI / 180.0; }

/* NOAA sun position: elevation + azimuth from north for unix UTC */
static void sun_pos(time_t t, double *elev, double *az)
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
	sun_pos(t, &elev, &az);
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

static double dc_now(time_t t, int month)
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
	} else if (strcmp(msg->topic, R290_TOPIC) == 0) {
		pthread_mutex_lock(&mq_lock);
		mq.r290_hz = (int)jnum(d, "comp_freq_actual", 0, NULL);
		mq.r290_time = mono();
		pthread_mutex_unlock(&mq_lock);
	}
	cJSON_Delete(d);
}

/* ---- control ------------------------------------------------------------------------------------ */

/* bank whose SoC is more than SOC_BALANCE_ON above the other one; cleared below SOC_BALANCE_OFF */
static void update_lead(void)
{
	if (!soc_ok[0] || !soc_ok[1]) {
		lead = -1;
		return;
	}
	double diff = soc[0] - soc[1];
	if (lead < 0 && fabs(diff) > SOC_BALANCE_ON) {
		lead = diff > 0 ? 0 : 1;
		LOG("SoC balance: %s ahead by %.1f %%", BANKS[lead].name, fabs(diff));
	} else if (lead >= 0 && fabs(diff) < SOC_BALANCE_OFF) {
		LOG("SoC balance: back within %.1f %%", SOC_BALANCE_OFF);
		lead = -1;
	}
}

/* fox2db_logic.h step(): charge block until the PCC peak window, summer only. 1 = do not charge from PV.
   multis = signed AC-in sum of the Multis (+ = they take from the AC side) */
static int ladesperre(int stale, double pcc, int have_pcc, double bat1, double pcc_avg5, double bat1_avg5,
					  double aussen, double aussen_time, double multis)
{
	time_t now = time(NULL);
	struct tm loc, mid;
	localtime_r(&now, &loc);
	int month = loc.tm_mon + 1;
	if (loc.tm_yday != ls_yday) {      /* midnight reset */
		ls_yday = loc.tm_yday;
		peak_today = ls_latched = badweather_today = 0;
	}
	double dc = dc_now(now, month);
	if (dc > 0 && aussen_time > 0 && mono() - aussen_time < AUSSEN_MAX_AGE) {
		double elev, az;
		sun_pos(now, &elev, &az);
		dc *= dc_temp_factor(elev, aussen);
	}
	mid = loc;
	mid.tm_hour = mid.tm_min = mid.tm_sec = 0;
	mid.tm_isdst = -1;
	time_t midnight = mktime(&mid);
	double best_w = 0;
	int peak_h = -1, win_end = -1;
	for (int h = 5; h <= 20; h++) {
		double w = dc_now(midnight + (time_t)h * 3600, month);
		if (w > best_w) {
			best_w = w;
			peak_h = h;
		}
		if (w > PCC_PEAK_TH)
			win_end = h;
	}
	time_t noon = solar_noon_utc(now);
	if (have_pcc && !stale && pcc > PCC_PEAK_TH)
		peak_today = 1;
	/* measured PV minus house load: export + Sofar Bat1 + what the Multis take (ESP: + EBox charger) */
	double ratio = -1.0, ratio_now = -1.0;
	if (dc > DC_RATIO_MIN && !stale && have_pcc) {
		ratio = (dc - (pcc_avg5 + multis + bat1_avg5)) / dc;
		ratio_now = (dc - (pcc + multis + bat1)) / dc;
	}
	int block = 0;
	if (LADESPERRE_ENABLE) {
		int in_window = month >= LADESPERRE_FROM && month <= LADESPERRE_TO && best_w > PCC_PEAK_TH && win_end >= 0
						&& !peak_today && loc.tm_hour <= peak_h && now < noon;
		if (in_window && ratio_now >= LADESPERRE_NOW_RATIO)
			badweather_today = 1;
		if (!in_window)
			ls_latched = 0;
		else if (ratio >= 0.0) {
			if (!ls_latched && ratio <= LADESPERRE_RATIO)
				ls_latched = 1;
			else if (ls_latched && ratio >= LADESPERRE_RATIO + LADESPERRE_HYST)
				ls_latched = 0;
		}
		block = in_window && ls_latched && !badweather_today;
	}
	ls.active = block;
	ls.dc = dc;
	ls.ratio = ratio;
	ls.ratio_now = ratio_now;
	ls.peak_h = best_w > PCC_PEAK_TH ? peak_h : -1;
	ls.win_end_h = win_end;
	ls.noon_h = (double)(noon - midnight) / 3600.0;
	return block;
}

static void sample_power(void)
{
	double now = mono();
	for (int b = 0; b < NBANK; b++) {
		double pw;
		int k = 0;
		if (get(BANKS[b].service, "/Dc/0/Power", &pw) == 0) {
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
	if (*has_avg && soc_ok[b] && *avg >= ETA_MIN_W && soc[b] < 100)
		*full_at = (long)(time(NULL) + (100 - soc[b]) / 100 * BANKS[b].capacity_wh / *avg * 3600);
}

static void write_state(const int *sp, char why[][96], double surplus)
{
	cJSON *st = cJSON_CreateObject(), *o;
	char tmp[264];
	snprintf(tmp, sizeof(tmp), "%s.tmp", STATE_FILE);
	cJSON_AddNumberToObject(st, "ts", (double)time(NULL));
	cJSON_AddStringToObject(st, "version", VERSION);
	o = cJSON_AddObjectToObject(st, "sp");
	for (int p = 0; p < NPH; p++)
		cJSON_AddNumberToObject(o, PH[p], sp[p]);
	o = cJSON_AddObjectToObject(st, "rule");
	for (int p = 0; p < NPH; p++) {
		char r[49];
		snprintf(r, sizeof(r), "%.48s", why[p]);
		cJSON_AddStringToObject(o, PH[p], r);
	}
	if (lead >= 0)
		cJSON_AddStringToObject(st, "balance_lead", BANKS[lead].name);
	else
		cJSON_AddNullToObject(st, "balance_lead");
	cJSON_AddNumberToObject(st, "surplus_w", round(surplus));
	o = cJSON_AddObjectToObject(st, "prot");
	for (int b = 0; b < NBANK; b++)
		cJSON_AddNumberToObject(o, BANKS[b].name, prot[b]);
	o = cJSON_AddObjectToObject(st, "force");
	for (int b = 0; b < NBANK; b++)
		cJSON_AddNumberToObject(o, BANKS[b].name, force_[b]);
	o = cJSON_AddObjectToObject(st, "forecast");
	for (int b = 0; b < NBANK; b++) {
		int has;
		double avg;
		long full;
		full_forecast(b, &has, &avg, &full);
		cJSON *f = cJSON_AddObjectToObject(o, BANKS[b].name);
		if (has)
			cJSON_AddNumberToObject(f, "avg5_w", round(avg));
		else
			cJSON_AddNullToObject(f, "avg5_w");
		if (full)
			cJSON_AddNumberToObject(f, "full_at", (double)full);
		else
			cJSON_AddNullToObject(f, "full_at");
	}
	o = cJSON_AddObjectToObject(st, "ladesperre");
	cJSON_AddNumberToObject(o, "active", ls.active);
	cJSON_AddNumberToObject(o, "dc_expected_w", round(ls.dc));
	cJSON_AddNumberToObject(o, "ratio", round(ls.ratio * 1000) / 1000);
	cJSON_AddNumberToObject(o, "ratio_now", round(ls.ratio_now * 1000) / 1000);
	cJSON_AddNumberToObject(o, "peak_h", ls.peak_h);
	cJSON_AddNumberToObject(o, "win_end_h", ls.win_end_h);
	cJSON_AddNumberToObject(o, "noon_h", round(ls.noon_h * 100) / 100);
	cJSON_AddNumberToObject(o, "peak_today", peak_today);
	cJSON_AddNumberToObject(o, "badweather_today", badweather_today);
	o = cJSON_AddObjectToObject(st, "pi");
	cJSON_AddNumberToObject(o, "armed", PI_ENABLE);
	cJSON_AddNumberToObject(o, "y_w", round(pi.y));
	cJSON_AddNumberToObject(o, "e_w", round(pi.e));
	cJSON_AddNumberToObject(o, "applied_w", round(pi.applied));
	cJSON_AddNumberToObject(o, "u_w", round(pi.u));
	cJSON *ps = cJSON_AddObjectToObject(o, "sp");
	for (int p = 0; p < NPH; p++)
		cJSON_AddNumberToObject(ps, PH[p], pi.sp[p]);
	char *s = cJSON_PrintUnformatted(st);
	FILE *f = fopen(tmp, "w");
	if (!f || fputs(s, f) < 0 || fclose(f) != 0 || rename(tmp, STATE_FILE) != 0)
		LOGE("state file: write failed");
	free(s);
	cJSON_Delete(st);
}

/* discharge w (W, > 0) over the phases in dis[]; SoC balancing: the stack more than SOC_BALANCE_ON ahead
   delivers alone, else equal shares */
static void alloc_discharge(int w, const int *dis, const char *rule, int *sp, char why[][96])
{
	int give[NPH] = {0}, n_give = 0, lead_ph;
	if (lead >= 0)
		for (int i = 0; i < BANKS[lead].nph; i++)
			if (dis[BANKS[lead].ph[i]]) {
				give[BANKS[lead].ph[i]] = 1;
				n_give++;
			}
	lead_ph = n_give > 0;
	if (!lead_ph)
		for (int p = 0; p < NPH; p++)
			if (dis[p]) {
				give[p] = 1;
				n_give++;
			}
	for (int p = 0; p < NPH; p++) {
		if (!dis[p])
			continue;
		sp[p] = give[p] ? -(int)((double)w / n_give) : 0;
		snprintf(why[p], 96, "%s%s", rule, lead_ph && give[p] ? "|BALANCE" : lead_ph ? "|BALANCE_HOLD" : "");
	}
}

/* charge rest (W) over the phases in chg[]: the banks in CHARGE_PRIORITY order up to their real charger capacity,
   the stack behind first; what is left up to CHARGE_MAX_PHASE on all phases alike */
static void alloc_charge(double rest, const int *chg, int n_chg, int *sp, char why[][96])
{
	int order[NBANK], k = 0;
	for (int i = 0; i < NBANK; i++)
		if (CHARGE_PRIORITY[i] != lead)
			order[k++] = CHARGE_PRIORITY[i];
	if (lead >= 0)
		order[k++] = lead;
	for (int i = 0; i < k; i++) {
		int b = order[i], ph[NPH], n = 0;
		for (int j = 0; j < BANKS[b].nph; j++)
			if (chg[BANKS[b].ph[j]])
				ph[n++] = BANKS[b].ph[j];
		if (!n)
			continue;
		double share = fmin(rest, (double)CHARGER_CAP_PHASE * n);
		for (int j = 0; j < n; j++) {
			sp[ph[j]] = (int)(share / n);
			strncat(why[ph[j]], "|CHARGE", 95 - strlen(why[ph[j]]));
		}
		rest -= share;
	}
	if (rest > 0)
		for (int p = 0; p < NPH; p++)
			if (chg[p])
				sp[p] += (int)fmin(rest / n_chg, (double)(CHARGE_MAX_PHASE - sp[p]));
}

static void pi_shadow_log(double pcc, double bat1, double pv, const int *sp, char why[][96])
{
	FILE *f = fopen(PI_SHADOW_FILE, "a");
	if (!f)
		return;
	if (ftell(f) == 0)
		fputs("ts,pcc_w,bat1_w,pv_w,y_w,e_w,applied_w,pi_u_w,pi_min_w,pi_max_w,"
			  "sp_l1,sp_l2,sp_l3,pi_l1,pi_l2,pi_l3,armed,rule_l1,rule_l2,rule_l3\n", f);
	fprintf(f, "%ld,%.0f,%.0f,%.0f,%.0f,%.0f,%.0f,%.0f,%.0f,%.0f,%d,%d,%d,%d,%d,%d,%d,%s,%s,%s\n", (long)time(NULL),
			pcc, bat1, pv, pi.y, pi.e, pi.applied, pi.u, pi.u_min, pi.u_max, sp[0], sp[1], sp[2],
			pi.sp[0], pi.sp[1], pi.sp[2], PI_ENABLE, why[0], why[1], why[2]);
	long size = ftell(f);
	fclose(f);
	if (size > PI_SHADOW_MAX)
		rename(PI_SHADOW_FILE, PI_SHADOW_FILE ".1");
}

/* one cycle: the ESP soyo calculation, per bank */
static void calc(void)
{
	static char last_rules[NPH * 96];
	static double last_log;
	double now = mono();
	time_t tnow = time(NULL);
	struct tm loc;
	localtime_r(&tnow, &loc);
	int month = loc.tm_mon + 1;

	pthread_mutex_lock(&mq_lock);
	int have_pcc = mq.have_pcc, have_pv = mq.have_pv;
	double pcc = mq.pcc, pv = mq.pv, bat1 = mq.bat1, pcc_avg5 = mq.pcc_avg5, bat1_avg5 = mq.bat1_avg5,
		   inv_time = mq.inv_time, r290_time = mq.r290_time, aussen = mq.aussen, aussen_time = mq.aussen_time,
		   wp_w = mq.wp, wp_time = mq.wp_time;
	int r290_hz = mq.r290_hz;
	pthread_mutex_unlock(&mq_lock);

	int stale = inv_time == 0 || now - inv_time > STALE_SECONDS;
	int wp_running = r290_time > 0 && now - r290_time < STALE_SECONDS && r290_hz > 0;
	/* Z2 point = Z1 PCC + heat pump: the discharge serves only the house (0.19) */
	int wp_fresh = wp_time > 0 && now - wp_time < WP_MAX_AGE;
	int winter = !(month >= SUMMER_FROM && month <= SUMMER_TO);
	double wp_eff = winter && wp_fresh && wp_w > 0 ? wp_w : 0.0;   /* summer: WP 100 % from the batteries */
	double z2 = pcc + wp_eff;
	int sp[NPH] = {0}, discharge[NPH] = {0}, charge[NPH] = {0}, n_dis = 0, n_chg = 0;
	int pi_ok[NPH] = {0}, pi_chg[NPH] = {0}, pi_dis[NPH] = {0}, n_pichg = 0, n_pidis = 0;
	char why[NPH][96] = {{0}};

	/* the meter already includes our own charging: use the measured AC-in, not the setpoints */
	const char *vb = vebus();
	double own_charge = 0.0, own_discharge = 0.0, multis = 0.0;
	for (int p = 0; p < NPH; p++) {
		char path[32];
		double a;
		snprintf(path, sizeof(path), "/Ac/ActiveIn/%s/P", PH[p]);
		if (vb && get(vb, path, &a) == 0) {
			multis += a;
			if (a > 0)
				own_charge += a;
			else
				own_discharge -= a;
		}
	}
	/* a Sofar Bat1 discharge is no surplus; its charging counts with BAT1_CHARGE_FACTOR */
	double bat1_eff = bat1 < 0 ? bat1 : bat1 * BAT1_CHARGE_FACTOR;
	double surplus = (have_pcc ? pcc : 0.0) + bat1_eff + own_charge;
	int block = ladesperre(stale, pcc, have_pcc, bat1, pcc_avg5, bat1_avg5, aussen, aussen_time, multis);
	/* surplus includes the own charging: once charging, it holds down to SOYO_HOLD_TH instead of 200 W */
	int charge_mode = surplus > PCC_SURPLUS_TH || (soyo_chg_prev && surplus > SOYO_HOLD_TH);

	for (int b = 0; b < NBANK; b++) {
		const char *s = BANKS[b].service, *bn = BANKS[b].name;
		double conn, v, power;
		soc_ok[b] = get(s, "/Connected", &conn) == 0 && conn == 1.0 && get(s, "/Soc", &v) == 0;
		if (soc_ok[b])
			soc[b] = v;
		int have_power = get(s, "/Dc/0/Power", &power) == 0;
		if (soc_ok[b]) {
			if (soc[b] < SOC_MIN)
				prot[b] = 1;
			else if (soc[b] >= SOC_MIN_RELEASE)
				prot[b] = 0;
			if (soc[b] < SOC_FORCE)
				force_[b] = 1;
			else if (soc[b] >= SOC_FORCE_RELEASE)
				force_[b] = 0;
		}
		for (int i = 0; i < BANKS[b].nph; i++) {
			int p = BANKS[b].ph[i];
			sp[p] = 0;
			/* PI: own gates (the soyo ones depend on its fixed thresholds) */
			if (soc_ok[b] && !force_[b] && !stale) {
				pi_ok[p] = 1;
				if (!block && soc[b] < 100.0) {
					pi_chg[p] = 1;
					n_pichg++;
				}
				if (!prot[b]) {
					pi_dis[p] = 1;
					n_pidis++;
				}
			}
			if (!soc_ok[b])
				snprintf(why[p], 96, "%s:BMS_MISSING", bn);
			else if (force_[b]) {
				sp[p] = FORCE_CHARGE_W;
				snprintf(why[p], 96, "%s:FORCE_CHARGE(%.1f%%)", bn, soc[b]);
			} else if (stale)
				snprintf(why[p], 96, "STALE");
			else if (charge_mode && block)
				snprintf(why[p], 96, "%s:LADESPERRE(peak %dh)", bn, ls.peak_h);
			else if (charge_mode) {
				snprintf(why[p], 96, "%s:PV_SURPLUS", bn);
				if (soc[b] < 100.0) {
					charge[p] = 1;
					n_chg++;
				}
			} else if (prot[b])
				snprintf(why[p], 96, "%s:DISCHARGE_PROTECTION(%.1f%%)", bn, soc[b]);
			else if (have_power && power > CHARGING_TH)
				snprintf(why[p], 96, "%s:CHARGING(%.0fW)", bn, power);
			else {
				discharge[p] = 1;
				n_dis++;
			}
		}
	}
	update_lead();
	if (n_dis) {
		int night = have_pv && pv < NIGHT_PV_TH, w;
		char rule[64];
		/* deficit = what the Multis already give (minus what of it goes into the Sofar battery) + the import still
		   left (0.18; old: -KP * pcc, which dropped back to IDLE as soon as the own discharge covered the import) */
		double deficit = own_discharge - (bat1 > 0 ? bat1 : 0.0) - KP * (z2 - SOYO_TARGET);
		int night_floor = night && wp_eff < WP_ON_TH;   /* the night base load would flow into the heat pump */
		if (z2 < PCC_IMPORT_TH || (soyo_prop_prev && deficit > SOYO_HOLD_TH)) {
			w = deficit > 0 ? (int)deficit : 0;   /* never turn a discharge into charging (Sofar TOU charge) */
			if (night_floor && w < B_NIGHT)
				w = B_NIGHT;
			if (w > W_MAX)
				w = W_MAX;
			strcpy(rule, "PROPORTIONAL");
			soyo_prop_prev = 1;
		} else {
			soyo_prop_prev = 0;
			w = night_floor ? B_NIGHT : B_DAY_IDLE;
			strcpy(rule, "IDLE");
		}
		if (night)
			strcat(rule, "|NIGHT");
		if (wp_eff >= WP_ON_TH)
			strcat(rule, "|Z2");
		if (!wp_fresh && !(month >= SUMMER_FROM && month <= SUMMER_TO) && wp_running && w > WP_CAP) {
			w = WP_CAP;
			strcat(rule, "|WP_CAP");
		}
		alloc_discharge(w, discharge, rule, sp, why);
	}
	if (n_chg) {
		alloc_charge(fmin((int)(own_charge + KP * ((have_pcc ? pcc : 0.0) + bat1_eff - SOYO_TARGET)), (double)CHARGE_MAX_PHASE * n_chg),
					 charge, n_chg, sp, why);
	}
	soyo_chg_prev = n_chg > 0;
	if (!n_dis)
		soyo_prop_prev = 0;

	/* PI prototype. y = PCC + Sofar Bat1: Bat1 charging counts with BAT1_CHARGE_FACTOR (as the soyo surplus);
	   a Bat1 discharge counts while the Multis charge (the Sofar must not feed them) and at night (the Multis take
	   the base load over from the Sofar, soyo: fixed 936 W); by day while they discharge it is ignored (the Sofar
	   battery covers the house first, the Multis only real grid import, as soyo) */
	{
		int night = have_pv && pv < NIGHT_PV_TH;
		double applied = 0;
		for (int p = 0; p < NPH; p++)
			if (pi_ok[p])
				applied += setpoints[p];
		double b1 = bat1 > 0 ? bat1 * BAT1_CHARGE_FACTOR : (applied > 0 || (night && wp_eff < WP_ON_TH)) ? bat1 : 0.0;
		/* discharge side on the Z2 point (0.19), charge side on the Z1 PCC, as soyo */
		double y = (have_pcc ? (applied > 0 ? pcc : z2) : 0.0) + b1, e = y - PI_TARGET;
		if (fabs(e) < PI_DEADBAND)
			e = 0;
		double dt = pi_t_prev > 0 ? fmin(fmax(now - pi_t_prev, 1.0), 30.0) : CYCLE_SECONDS;
		double u = applied + (pi_t_prev > 0 ? PI_KP * (e - pi_e_prev) : 0) + PI_KI * dt * e;
		int dis_cap = (!wp_fresh && !(month >= SUMMER_FROM && month <= SUMMER_TO) && wp_running) ? WP_CAP : W_MAX;
		double u_max = (double)CHARGE_MAX_PHASE * n_pichg, u_min = n_pidis ? -dis_cap : 0;
		/* crossing from discharge (Z2 side) into charging: never more than the real Z1 export, else the Sofar Bat1
		   would feed the charging while the heat pump runs */
		if (applied <= 0 && u > 0)
			u = fmax(0.0, fmin(u, (have_pcc ? pcc : 0.0) + b1 - PI_TARGET));
		u = fmax(u_min, fmin(u_max, u));
		pi_e_prev = e;
		pi_t_prev = now;
		int psp[NPH] = {0};
		char pwhy[NPH][96];
		for (int p = 0; p < NPH; p++)
			snprintf(pwhy[p], 96, "%.95s", why[p]);
		for (int p = 0; p < NPH; p++)
			if (pi_ok[p])
				snprintf(pwhy[p], 96, "PI(%+.0f)", u);
		if (u > 0)
			alloc_charge(u, pi_chg, n_pichg, psp, pwhy);
		else if (u < 0) {
			char rule[32];
			snprintf(rule, sizeof(rule), "PI(%+.0f)", u);
			alloc_discharge((int)-u, pi_dis, rule, psp, pwhy);
		}
		for (int p = 0; p < NPH; p++)
			if (!pi_ok[p])
				psp[p] = sp[p];          /* force charge, BMS missing, stale: as soyo */
		pi.y = y;
		pi.e = e;
		pi.applied = applied;
		pi.u = u;
		pi.u_min = u_min;
		pi.u_max = u_max;
		memcpy(pi.sp, psp, sizeof(psp));
		if (PI_ENABLE) {
			memcpy(sp, psp, sizeof(psp));
			memcpy(why, pwhy, sizeof(pwhy));
		}
	}
	pi_shadow_log(have_pcc ? pcc : 0, bat1, have_pv ? pv : 0, sp, why);

	char rules[NPH * 96] = "";
	for (int p = 0; p < NPH; p++) {
		strcat(rules, why[p]);
		strcat(rules, ";");
	}
	if (strcmp(rules, last_rules) != 0 || now - last_log >= LOG_SECONDS) {
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
			o += snprintf(eta + o, sizeof(eta) - o, "%s%s full %s", b ? "  " : "", BANKS[b].name, hm);
		}
		o = 0;
		for (int p = 0; p < NPH; p++)
			o += snprintf(line + o, sizeof(line) - o, "%s%s %+d W (%s)", p ? "  " : "", PH[p], sp[p], why[p]);
		char spcc[16] = "None", spv[16] = "None";
		if (have_pcc)
			snprintf(spcc, sizeof(spcc), "%.0f", pcc);
		if (have_pv)
			snprintf(spv, sizeof(spv), "%.0f", pv);
		LOG("%spcc %s W wp %.0f W bat1 %.0f W pv %s W -> %s | %s | PI%s u %+.0f W", LIVE ? "" : "DRY ", spcc, wp_eff, bat1, spv,
			line, eta, PI_ENABLE ? "" : " shadow", pi.u);
		strcpy(last_rules, rules);
		last_log = now;
	}
	memcpy(setpoints, sp, sizeof(sp));
	setpoint_time = now;
	write_state(sp, why, surplus);
}

static void send_setpoints(void)
{
	int sp[NPH];
	sample_power();
	const char *vb = vebus();
	if (!vb)
		return;
	memcpy(sp, setpoints, sizeof(sp));
	if (mono() - setpoint_time > SETPOINT_MAX_AGE)
		memset(sp, 0, sizeof(sp));          /* ESP TX watchdog */
	if (!hub4mode_set) {
		set_int("com.victronenergy.settings", "/Settings/CGwacs/Hub4Mode", 3);
		hub4mode_set = LIVE;
	}
	for (int p = 0; p < NPH; p++) {
		char path[48];
		snprintf(path, sizeof(path), "/Hub4/%s/MaxFeedInPower", PH[p]);
		set_int(vb, path, (sp[p] < 0 ? -sp[p] : 0) + 100);
		snprintf(path, sizeof(path), "/Hub4/%s/AcPowerSetpoint", PH[p]);
		set_int(vb, path, sp[p]);
	}
}

static void stop_control(void)
{
	const char *vb = vebus();
	if (vb)
		for (int p = 0; p < NPH; p++) {
			char path[48];
			snprintf(path, sizeof(path), "/Hub4/%s/AcPowerSetpoint", PH[p]);
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
	LADESPERRE_ENABLE = !l || strcmp(l, "1") == 0;
	const char *pe = getenv("BATMONITOR_PI");
	PI_ENABLE = pe && strcmp(pe, "1") == 0;
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
		LIVE ? "LIVE" : "DRY RUN", CYCLE_SECONDS, LADESPERRE_ENABLE ? "on" : "off", PI_ENABLE ? "ARMED" : "shadow");

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
