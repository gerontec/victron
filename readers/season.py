#!/usr/bin/python3
"""
season.py - summer/transition/winter mode for batmonitor from measured energy (Pi .218, cron hourly).

Rule (user 2026-10-08): the heat pump may be served from the batteries once the PV surplus is large enough that the
batteries refill anyway. Per day:
    export_kwh  = sold energy at the Sofar PCC (Z1): time-weighted mean of max(ActivePower_PCC_Total, 0) * 24 h,
                  wagodb.inverter_data (device_id 1)
    wp_kwh      = heat pump energy: max - min of sdm72d.total_import_active_energy
Over a sliding window of WINDOW_DAYS (today included), ratio = export_7d / wp_7d:
    summer       ratio above SUMMER_FACTOR (2x)              heat pump 100 % from the batteries
    transition   between WINTER_FACTOR and SUMMER_FACTOR     heat pump at most 1900 W from the batteries (user 2026-10-08)
    winter       ratio below WINTER_FACTOR (1x)              heat pump from the Z1 grid only
A band is left only HYST beyond its threshold (against flapping); the modes are replayed over all days in the DB
(inverter_data keeps ~90 days), so the script needs no state file; the first day starts in the band of its ratio.

Output: retained MQTT batmonitor/season on the local broker (batmonitor in Venus subscribes .218):
    {"mode": "summer"|"transition"|"winter", "export_7d_kwh": .., "wp_7d_kwh": .., "ratio": .., "since": "YYYY-MM-DD", "ts": unix}
batmonitor falls back to the month rule (Oct-Apr = winter) when the message is missing or older than 2 days.

usage: season.py            compute + publish
       season.py --print    show the daily table, do not publish
"""
import json
import sys
import time

sys.path.insert(0, "/home/pi/python")
from db_config import get_db_connection   # wagodb on 192.168.178.218

WINDOW_DAYS = 7
SUMMER_FACTOR = 2.0
WINTER_FACTOR = 1.0
HYST = 0.1
MQTT_HOST, MQTT_TOPIC = "127.0.0.1", "batmonitor/season"


def daily_energy(cur):
	cur.execute("""SELECT DATE(timestamp), AVG(GREATEST(ActivePower_PCC_Total, 0)) * 24
				   FROM inverter_data WHERE device_id = 1 AND ActivePower_PCC_Total IS NOT NULL
				   GROUP BY DATE(timestamp)""")
	export = {d: float(v) for d, v in cur.fetchall()}
	cur.execute("""SELECT DATE(timestamp), MAX(total_import_active_energy) - MIN(total_import_active_energy)
				   FROM sdm72d GROUP BY DATE(timestamp)""")
	wp = {d: float(v or 0) for d, v in cur.fetchall()}
	return export, wp


def replay(export, wp):
	"""hysteresis over all common days; returns the rows (day, export, wp, e7, w7, mode) and the day of the last switch"""
	days = sorted(set(export) & set(wp))
	rows, mode, since = [], None, None
	for i, d in enumerate(days):
		win = days[max(0, i - WINDOW_DAYS + 1): i + 1]
		e7 = sum(export[x] for x in win)
		w7 = sum(wp[x] for x in win)
		r = e7 / w7 if w7 > 0 else float("inf")
		new = mode
		if mode is None:
			new = "summer" if r > SUMMER_FACTOR else "winter" if r < WINTER_FACTOR else "transition"
		elif mode == "winter" and r > WINTER_FACTOR + HYST:
			new = "summer" if r > SUMMER_FACTOR + HYST else "transition"
		elif mode == "transition" and r > SUMMER_FACTOR + HYST:
			new = "summer"
		elif mode == "transition" and r < WINTER_FACTOR - HYST:
			new = "winter"
		elif mode == "summer" and r < SUMMER_FACTOR - HYST:
			new = "winter" if r < WINTER_FACTOR - HYST else "transition"
		if new != mode:
			since = d
		mode = new
		rows.append((d, export[d], wp[d], e7, w7, mode))
	return rows, since


def main():
	con = get_db_connection()
	try:
		rows, since = replay(*daily_energy(con.cursor()))
	finally:
		con.close()
	if not rows:
		print("no common days in inverter_data and sdm72d")
		return 1
	if "--print" in sys.argv:
		print("day         export_kWh  wp_kWh   7d_export  7d_wp  mode")
		for d, e, w, e7, w7, m in rows:
			print("%s  %9.1f  %6.1f  %10.1f %6.1f  %s" % (d, e, w, e7, w7, m))
		return 0
	d, e, w, e7, w7, mode = rows[-1]
	msg = {"mode": mode, "export_7d_kwh": round(e7, 1), "wp_7d_kwh": round(w7, 1),
		   "ratio": round(e7 / w7, 2) if w7 > 0 else None, "since": str(since), "ts": int(time.time())}
	import paho.mqtt.publish as publish
	publish.single(MQTT_TOPIC, json.dumps(msg), qos=1, retain=True, hostname=MQTT_HOST)
	print(json.dumps(msg))
	return 0


if __name__ == "__main__":
	sys.exit(main())
