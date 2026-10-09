#!/usr/bin/python3
"""
forecast.py - target SoC of the Multi stacks for the next morning (Pi .218, cron hourly), for batmonitor.

Goal (user 2026-10-08): sell less, use more. When the next day's PV surplus will refill the Multi stacks anyway,
they may serve everything tonight (heat pump included, regardless of season); below the target SoC the tariff
rules apply again.

  expected_kwh = clear-sky model (fox2db arrays, both inverters) x weather kt (OWM forecast, wetter_pv_modell
                 coefficients as in fox2db_logic.h) x correction
  correction   = sum(measured) / sum(model x kt) over the last CAL_DAYS complete days, clamped CORR_MIN..CORR_MAX
                 (the models overestimate the low autumn sun, ~2x on clear October days: the correction follows the
                 season and the shading by itself)
  measured     = WR1 Sofar (inverter_data Power_PV1+PV2) + WR2 FoxESS (inverter_data2 pvPower, cloud API), integrated
                 over the real sample intervals (FoxESS only reports while it runs)
  day_load_kwh = mean over LOAD_DAYS of the consumption during PV hours (house + heat pump, energy balance at the
                 PCC: PV + import - export - Sofar Bat1 charge - Multis AC-in)
  free_kwh     = clamp((expected_kwh - day_load_kwh - SOFAR_BAT1_KWH) * RELY, 0, CAPACITY_KWH)
  target_soc   = max(TARGET_MIN, 100 - free_kwh / CAPACITY_KWH * 100)
The forecast day is today before 12:00 local (the coming morning), else tomorrow.

Output: retained MQTT batmonitor/forecast on the local broker:
  {"for_date", "target_soc", "expected_kwh", "kt", "correction", "day_load_kwh", "free_kwh", "kt_slots", "ts"}
kt_slots (0.28-c): [[unix_ts, kt], ...] of the daytime OWM 3 h slots today and tomorrow (same regression per slot),
batmonitor uses them with the correction for the per-stack full_at forecast (clear-sky x kt x correction).

usage: forecast.py            compute + publish
       forecast.py --print    calibration table and result, do not publish
"""
import datetime as dt
import json
import math
import sys
import time
from collections import defaultdict
from zoneinfo import ZoneInfo

sys.path.insert(0, "/home/pi/python")
from db_config import get_db_connection          # wagodb on 192.168.178.218
import wetter_prognose_mqtt as wx                 # DB_CONFIG (heissa weather_data), LAT/LON of the OWM location
import pymysql

TZ = ZoneInfo("Europe/Berlin")
CAL_DAYS = 14
LOAD_DAYS = 7
CORR_MIN, CORR_MAX = 0.2, 1.5
CAPACITY_KWH = 2 * 300 * 51.2 / 1000          # both Multi stacks
SOFAR_BAT1_KWH = 5.0                          # what the Sofar battery takes first (rough, 2026-10-08)
RELY = 0.8                                    # plan with 80 % of the predicted surplus
TARGET_MIN = 5.0                              # = batmonitor SOC_MIN
MQTT_HOST, MQTT_TOPIC = "127.0.0.1", "batmonitor/forecast"

# ---- clear-sky model (fox2db_logic.h / batmonitor bm_logic.c, without the monthly kt) --------------------------
LAT, LON = 47.6811, 11.5732
ARRAYS = ((60, 33, 19430), (68, -12, 7690), (59, -29, 22036), (67, 32, 2781))   # Sofar PV1/PV2, FoxESS pv2/pv1
HOR_AZ_SPLIT, HOR_EAST, HOR_SOUTH, HOR_DIFFUSE = 120.0, 23.0, 15.0, 0.25


def sun_pos(t_utc):
	g = time.gmtime(t_utc)
	hour = g.tm_hour + g.tm_min / 60.0 + g.tm_sec / 3600.0
	gamma = 2.0 * math.pi / 365.0 * (g.tm_yday - 1 + (hour - 12) / 24.0)
	eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(gamma) - 0.032077 * math.sin(gamma)
					   - 0.014615 * math.cos(2 * gamma) - 0.040849 * math.sin(2 * gamma))
	decl = (0.006918 - 0.399912 * math.cos(gamma) + 0.070257 * math.sin(gamma) - 0.006758 * math.cos(2 * gamma)
			+ 0.000907 * math.sin(2 * gamma) - 0.002697 * math.cos(3 * gamma) + 0.00148 * math.sin(3 * gamma))
	ha = math.radians((hour * 60.0 + eqtime + 4.0 * LON) / 4.0 - 180.0)
	latr = math.radians(LAT)
	cosz = math.sin(latr) * math.sin(decl) + math.cos(latr) * math.cos(decl) * math.cos(ha)
	elev = 90.0 - math.degrees(math.acos(max(-1.0, min(1.0, cosz))))
	az_s = math.atan2(math.sin(ha), math.cos(ha) * math.sin(latr) - math.tan(decl) * math.cos(latr))
	return elev, (math.degrees(az_s) + 180.0 + 360.0) % 360.0


def clear_w(t_utc):
	elev, az = sun_pos(t_utc)
	if elev <= 0:
		return 0.0
	tr = 0.7 ** (min(1.0 / math.sin(math.radians(elev)), 37.0) ** 0.678)
	shade = 1.0 if elev >= (HOR_EAST if az < HOR_AZ_SPLIT else HOR_SOUTH) else HOR_DIFFUSE
	e, s = math.radians(elev), 0.0
	for tilt, az_south, power in ARRAYS:
		b, da = math.radians(tilt), math.radians((az - 180.0) - az_south)
		s += power * tr * max(0.0, math.sin(e) * math.cos(b) + math.cos(e) * math.sin(b) * math.cos(da))
	return s * shade


def clear_day(day):
	"""clear-sky energy of a local day (kWh) and its PV hours (local datetimes with model > 200 W)"""
	t0 = dt.datetime.combine(day, dt.time(0), TZ)
	e, hours = 0.0, []
	for k in range(24 * 12):
		t = t0 + dt.timedelta(minutes=5 * k)
		w = clear_w(t.timestamp())
		e += w * 5 / 60 / 1000
		if w > 200:
			hours.append(t)
	return e, (hours[0], hours[-1] + dt.timedelta(minutes=5)) if hours else (None, None)


# ---- weather kt (fox2db_logic.h wx_kt, wetter_pv_modell.py: 227 days, R2 0.87) -----------------------------------
def weather_kt(cur, day):
	cur.execute("SELECT COUNT(*), AVG(cloudiness), SUM(rain_3h), AVG(pop), AVG(temperature), AVG(humidity),"
				" AVG(visibility), AVG(wind_speed) FROM weather_data WHERE latitude=%s AND longitude=%s"
				" AND part_of_day='d' AND timestamp >= %s AND timestamp < %s",
				(wx.LAT, wx.LON, day, day + dt.timedelta(days=1)))
	r = cur.fetchone()
	if not r or not r[0] or r[1] is None:
		return None
	return kt_formula(day, *r[1:])


def kt_formula(day, cloud, rain, pop, temp, hum, vis, wind):
	cloud, rain, pop, temp, hum, vis, wind = [float(x) if x is not None else 0.0 for x in (cloud, rain, pop, temp, hum,
																							vis, wind)]
	doy = day.timetuple().tm_yday
	kt = (0.88148703 - 0.0027887033 * cloud - 0.0027742720 * rain - 0.0655744400 * pop + 0.0028976317 * temp
		  - 0.0058142134 * hum + 0.0000338368 * (vis or 10000) + 0.0007884399 * wind
		  + 0.0434150265 * math.sin(2 * math.pi * doy / 365) + 0.0400069807 * math.cos(2 * math.pi * doy / 365))
	return max(0.03, min(1.05, kt))


def weather_slots(cur, day0, days=2):
	"""[[unix_ts, kt], ...] of the daytime 3 h slots from day0 on (weather_data timestamps are local time)"""
	cur.execute("SELECT timestamp, cloudiness, rain_3h, pop, temperature, humidity, visibility, wind_speed FROM weather_data"
				" WHERE latitude=%s AND longitude=%s AND part_of_day='d' AND timestamp >= %s AND timestamp < %s"
				" ORDER BY timestamp", (wx.LAT, wx.LON, day0, day0 + dt.timedelta(days=days)))
	return [[int(t.replace(tzinfo=TZ).timestamp()), round(kt_formula(t.date(), *r), 3)] for t, *r in cur.fetchall()]


# ---- measurements ---------------------------------------------------------------------------------------------
def integrate(rows, t_from=None, t_to=None, max_gap=900, sign=0):
	"""energy (kWh) of (time, kW) samples, trapezoid over real intervals; sign +1/-1: only that part"""
	e, prev = 0.0, None
	for t, p in rows:
		if p is None:
			continue
		p = float(p)
		if sign:
			p = max(0.0, p * sign)
		if prev and (t - prev[0]).total_seconds() <= max_gap and (t_from is None or t_from <= prev[0] and t <= t_to):
			e += (p + prev[1]) / 2 * (t - prev[0]).total_seconds() / 3600
		prev = (t, p)
	return e


def measured_day(cur, day, window):
	"""(pv_kwh, load_kwh during the PV window) of a local day; None if data is missing"""
	d0, d1 = dt.datetime.combine(day, dt.time(0)), dt.datetime.combine(day + dt.timedelta(days=1), dt.time(0))
	cur.execute("SELECT timestamp, Power_PV1 + Power_PV2, ActivePower_PCC_Total, Power_Bat1 FROM inverter_data"
				" WHERE device_id = 1 AND timestamp >= %s AND timestamp < %s ORDER BY timestamp", (d0, d1))
	sof = cur.fetchall()
	cur.execute("SELECT timestamp, pvPower FROM inverter_data2 WHERE timestamp >= %s AND timestamp < %s ORDER BY timestamp",
				(d0, d1))
	fox = cur.fetchall()
	if len(sof) < 100:
		return None
	pv = integrate([(t, a) for t, a, _, _ in sof]) + integrate(fox)
	w0, w1 = [x.replace(tzinfo=None) for x in window]
	cur.execute("SELECT ts, (COALESCE(ac_in_l1_w,0) + COALESCE(ac_in_l2_w,0) + COALESCE(ac_in_l3_w,0)) / 1000 FROM pv_victron"
				" WHERE ts >= %s AND ts < %s ORDER BY ts", (w0, w1))
	multis = cur.fetchall()
	pv_win = integrate([(t, a) for t, a, _, _ in sof], w0, w1) + integrate(fox, w0, w1)
	export = integrate([(t, c) for t, _, c, _ in sof], w0, w1, sign=+1)
	imp = integrate([(t, c) for t, _, c, _ in sof], w0, w1, sign=-1)
	bat1 = integrate([(t, b) for t, _, _, b in sof], w0, w1)
	m = integrate(multis, w0, w1) if multis else 0.0
	return pv, pv_win + imp - export - bat1 - m


def main():
	now = dt.datetime.now(TZ)
	for_date = now.date() if now.hour < 12 else now.date() + dt.timedelta(days=1)
	con = get_db_connection()
	wcon = pymysql.connect(**wx.DB_CONFIG)
	rows = []
	try:
		cur, wcur = con.cursor(), wcon.cursor()
		for i in range(CAL_DAYS, 0, -1):
			day = now.date() - dt.timedelta(days=i)
			clear, window = clear_day(day)
			kt = weather_kt(wcur, day)
			m = measured_day(cur, day, window) if window[0] else None
			rows.append((day, clear, kt, m))
		clear_t, window_t = clear_day(for_date)
		kt_t = weather_kt(wcur, for_date)
		slots = weather_slots(wcur, now.date())
	finally:
		con.close()
		wcon.close()
	cal = [(c, k, m[0]) for _, c, k, m in rows if k is not None and m is not None]
	model_sum = sum(c * k for c, k, _ in cal)
	corr = max(CORR_MIN, min(CORR_MAX, sum(x for _, _, x in cal) / model_sum)) if model_sum > 0 else 1.0
	loads = [m[1] for _, _, _, m in rows[-LOAD_DAYS:] if m is not None]
	day_load = sum(loads) / len(loads) if loads else 0.0
	expected = clear_t * (kt_t if kt_t is not None else 0.5) * corr
	free = max(0.0, min(CAPACITY_KWH, (expected - day_load - SOFAR_BAT1_KWH) * RELY))
	target = max(TARGET_MIN, 100.0 - free / CAPACITY_KWH * 100.0)
	msg = {"for_date": str(for_date), "target_soc": round(target, 1), "expected_kwh": round(expected, 1),
		   "kt": None if kt_t is None else round(kt_t, 3), "correction": round(corr, 3), "day_load_kwh": round(day_load, 1),
		   "free_kwh": round(free, 1), "kt_slots": slots, "ts": int(time.time())}
	if "--print" in sys.argv:
		print("day         clear_kWh   kt    model_kWh  measured_kWh  day_load_kWh")
		for day, c, k, m in rows:
			print("%s  %9.1f  %5s  %9s  %12s  %12s" % (day, c, "-" if k is None else "%.2f" % k,
				  "-" if k is None else "%.1f" % (c * k), "-" if m is None else "%.1f" % m[0], "-" if m is None else "%.1f" % m[1]))
		print("for %s: clear %.1f kWh x kt %s x correction %.2f = %.1f kWh, day load %.1f kWh -> free %.1f kWh -> target SoC %.0f %%"
			  % (for_date, clear_t, kt_t, corr, expected, day_load, free, target))
		print("kt slots:", ", ".join("%s %.2f" % (dt.datetime.fromtimestamp(t, TZ).strftime("%d. %H:%M"), k) for t, k in slots))
		return 0
	import paho.mqtt.publish as publish
	publish.single(MQTT_TOPIC, json.dumps(msg), qos=1, retain=True, hostname=MQTT_HOST)
	print(json.dumps(msg))
	return 0


if __name__ == "__main__":
	sys.exit(main())
