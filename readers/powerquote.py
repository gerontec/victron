#!/usr/bin/env python3
"""Power quote (efficiency) of the three MultiPlus-II, integrated every second, published every minute.

Reads the local Venus OS MQTT broker (nspawn subsystem on this host, 127.0.0.1:1883, keepalive like victron2db.py)
and integrates the energy of each bank every second (last value held, zero-order hold):
  S1   Stack1 MUST  = L1 only   AC: vebus Ac/ActiveIn/L1/P - Ac/Out/L1/P        DC: battery CAN-bus BMS Dc/0/Power
  S2   Stack2 Pytes = L2 + L3   AC: the same for L2 + L3                          DC: battery EBox Dc/0/Power
  ALL  both banks               AC: L1-L3                                         DC: S1 + S2
  plus multi_dc_wh: vebus Dc/0/Power, the DC the three Multis measure themselves (cross-check of the BMS shunts)
The MUST sends U and I every 2.6 s (CAN 0x356), Venus every ~1.3 s; the EBox value changes only every ~30 s
(ebox_mqtt.py), so the S2 quote of a single minute is noisy, its day sum is not.
Signs: + = charging (AC in -> battery), - = discharging. quote = DC / AC charging, AC / DC discharging (the eta);
none when the minute's AC is below MIN_W on average or AC and DC point different ways.
Day totals per bank and direction (only the minutes with a quote), reset at local midnight; "since" = start of
the day totals (service start or midnight).

Every minute, retained, JSON to MQTT_OUT_HOST (Pi .218 kellertreppe broker) topic victron/powerquote:
  {"ts", "minute": {"S1": {"ac_wh", "dc_wh", "dir", "quote", "loss_w", "rule"}, "S2": ..., "ALL": ...,
   "multi_dc_wh"}, "day": {"S1": {"charge": {"ac_wh", "dc_wh", "quote", "minutes"}, "discharge": ...}, ...},
   "since", "stale"}
rule = the batmonitor split tag of the bank's phase (1PH / STBY / EQ ...) from /data/batmonitor/state.json.
Every minute the day totals also go to wagodb.pv_victron_quote (sql/pv_victron_quote.sql) as an upsert, one row per
day, bank and direction; a minute belongs to the day it starts in. On start the rows of today are read back, so a
restart continues the day instead of starting it again. A DB error is logged and retried next minute.
Runs as powerquote.service (host/powerquote.service) as user pi.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta

import paho.mqtt.client as mqtt
import pymysql

MQTT_HOST = '127.0.0.1'
MQTT_OUT_HOST = '192.168.178.218'
TOPIC_OUT = 'victron/powerquote'
MIN_W = 100            # mean |AC| of a minute below this: no quote (standby)
MAX_AGE = 60           # s: a value older than this does not count, the minute is marked stale
KEEPALIVE_S = 30
BM_STATE = '/data/batmonitor/state.json'
SPLIT_TAGS = ('1PH', 'STBY', 'EQ', 'BAL', 'SPILL', 'WAIT')
BANKS = ('S1', 'S2', 'ALL')
DB_CNF = os.path.expanduser('~/.victron2db.cnf')
TABLE = 'pv_victron_quote'

# (service, path suffix) -> key; battery instances are told apart by ProductName
AC_KEYS = {f'Ac/ActiveIn/L{p}/P': f'in{p}' for p in (1, 2, 3)}
AC_KEYS.update({f'Ac/Out/L{p}/P': f'out{p}' for p in (1, 2, 3)})


def fallback_portal_id():
	try:
		with open('/sys/class/net/eth0/address') as f:
			return f.read().strip().replace(':', '')
	except OSError:
		return 'unknown'


class Values:
	"""last value + receive time of every input"""

	def __init__(self):
		self.v, self.t = {}, {}
		self.battery_kind = {}            # battery instance -> 'S1' / 'S2'

	def set(self, key, value):
		self.v[key] = value
		self.t[key] = time.monotonic()

	def get(self, key, now):
		if key in self.v and now - self.t[key] <= MAX_AGE and self.v[key] is not None:
			return float(self.v[key])
		return None


def on_message(vals):
	def handler(client, userdata, msg):
		parts = msg.topic.split('/', 4)          # N, portal, service, instance, path
		if len(parts) < 5:
			return
		_, _, service, inst, path = parts
		try:
			value = json.loads(msg.payload).get('value')
		except (ValueError, AttributeError):
			return
		if service == 'vebus':
			if path in AC_KEYS:
				vals.set(AC_KEYS[path], value)
			elif path == 'Dc/0/Power':
				vals.set('multi_dc', value)
		elif service == 'battery':
			if path == 'ProductName' and isinstance(value, str):
				vals.battery_kind[inst] = 'S1' if 'CAN' in value else 'S2' if 'EBox' in value else None
			elif path == 'Dc/0/Power' and vals.battery_kind.get(inst):
				vals.set('dc_' + vals.battery_kind[inst], value)
	return handler


def sample(vals, now):
	"""instantaneous W per bank: {bank: (ac, dc)}, multi_dc; None where an input is missing or stale"""
	i = [vals.get(f'in{p}', now) for p in (1, 2, 3)]
	o = [vals.get(f'out{p}', now) or 0.0 for p in (1, 2, 3)]
	d1, d2 = vals.get('dc_S1', now), vals.get('dc_S2', now)
	s1 = (i[0] - o[0], d1) if i[0] is not None and d1 is not None else None
	s2 = (i[1] - o[1] + i[2] - o[2], d2) if None not in (i[1], i[2], d2) else None
	s = {'S1': s1, 'S2': s2, 'ALL': (s1[0] + s2[0], s1[1] + s2[1]) if s1 and s2 else None}
	return s, vals.get('multi_dc', now)


def rule_tags():
	"""split tag per bank from batmonitor's state.json (S1 = L1, S2 = L2)"""
	try:
		with open(BM_STATE) as f:
			rule = json.load(f).get('rule', {})
	except (OSError, ValueError):
		return {}
	out = {}
	for bank, ph in (('S1', 'L1'), ('S2', 'L2')):
		words = (rule.get(ph) or '').split()
		tag = next((t for t in SPLIT_TAGS if t in words), re.sub(r'\(.*', '', words[0]) if words else None)
		out[bank] = tag
	return out


def quote(ac_wh, dc_wh, seconds):
	"""(dir, quote, loss_w) of energies over `seconds`; dir None = standby / mixed"""
	if seconds <= 0 or abs(ac_wh) * 3600 / seconds < MIN_W or (ac_wh > 0) != (dc_wh > 0) or dc_wh == 0:
		return None, None, None
	if ac_wh > 0:
		return 'charge', dc_wh / ac_wh, (ac_wh - dc_wh) * 3600 / seconds
	return 'discharge', ac_wh / dc_wh, (ac_wh - dc_wh) * 3600 / seconds   # both negative: |AC| / |DC|, |DC| - |AC|


def new_day():
	return {b: {d: {'ac_wh': 0.0, 'dc_wh': 0.0, 'minutes': 0} for d in ('charge', 'discharge')} for b in BANKS}


def day_quote(d, z):
	if not z['ac_wh'] or not z['dc_wh']:
		return None
	return z['dc_wh'] / z['ac_wh'] if d == 'charge' else z['ac_wh'] / z['dc_wh']


def db_load(today):
	"""(day totals, since) of today from the table; (None, None) when there is no row or the DB is away"""
	try:
		con = pymysql.connect(read_default_file=DB_CNF, connect_timeout=10)
		with con.cursor() as c:
			c.execute(f"SELECT bank, direction, ac_wh, dc_wh, minutes, since FROM {TABLE} WHERE day = %s", (today,))
			rows = c.fetchall()
		con.close()
	except pymysql.Error as e:
		print(f"db_load: {e}", file=sys.stderr, flush=True)
		return None, None
	if not rows:
		return None, None
	day, since = new_day(), None
	for bank, d, ac, dc, n, s in rows:
		day[bank][d] = {'ac_wh': float(ac), 'dc_wh': float(dc), 'minutes': int(n)}
		since = s if s and (since is None or s < since) else since
	return day, since.isoformat(timespec='seconds') if since else None


def db_store(today, day, since):
	try:
		con = pymysql.connect(read_default_file=DB_CNF, connect_timeout=10)
		with con.cursor() as c:
			for b in BANKS:
				for d, z in day[b].items():
					q = day_quote(d, z)
					c.execute(f"""INSERT INTO {TABLE} (day, bank, direction, ac_wh, dc_wh, quote, minutes, since)
						VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
						ON DUPLICATE KEY UPDATE ac_wh = VALUES(ac_wh), dc_wh = VALUES(dc_wh), quote = VALUES(quote),
							minutes = VALUES(minutes), since = VALUES(since)""",
						(today, b, d, round(z['ac_wh'], 1), round(z['dc_wh'], 1),
						 round(q, 4) if q is not None else None, z['minutes'], since))
		con.commit()
		con.close()
	except pymysql.Error as e:
		print(f"db_store: {e}", file=sys.stderr, flush=True)


def main():
	vals = Values()
	portal = fallback_portal_id()
	src = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f'powerquote-{portal}-{int(time.time())}')
	src.on_connect = lambda c, u, f, rc, p: (c.subscribe('N/+/vebus/+/Ac/#'), c.subscribe('N/+/vebus/+/Dc/0/Power'),
											  c.subscribe('N/+/battery/+/Dc/0/Power'),
											  c.subscribe('N/+/battery/+/ProductName'),
											  c.publish(f'R/{portal}/keepalive', ''))
	src.on_message = on_message(vals)
	src.connect(MQTT_HOST, 1883, 60)
	src.loop_start()
	out = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f'powerquote-out-{portal}-{int(time.time())}')
	out.connect_async(MQTT_OUT_HOST, 1883, 60)
	out.loop_start()

	today = datetime.now().date()
	day, since = db_load(today)
	if day is None:
		day, since = new_day(), datetime.now().isoformat(timespec='seconds')
	acc = {b: [0.0, 0.0, 0] for b in BANKS}      # ac Ws, dc Ws, valid seconds
	multi_dc, stale = 0.0, 0
	last_keepalive = 0.0
	minute = int(time.time() // 60)
	nxt = time.monotonic()
	while True:
		nxt += 1.0
		time.sleep(max(0.0, nxt - time.monotonic()))
		now = time.monotonic()
		if now - last_keepalive >= KEEPALIVE_S:
			src.publish(f'R/{portal}/keepalive', '')
			last_keepalive = now
		s, mdc = sample(vals, now)
		for b in BANKS:
			if s[b]:
				acc[b][0] += s[b][0]
				acc[b][1] += s[b][1]
				acc[b][2] += 1
			else:
				stale = 1
		multi_dc += mdc or 0.0
		m = int(time.time() // 60)
		if m == minute:
			continue
		minute = m
		ts = datetime.now().replace(second=0, microsecond=0)
		start = ts - timedelta(minutes=1)                  # the minute just integrated belongs to its start day
		if start.date() != today:
			day, since, today = new_day(), ts.replace(hour=0).isoformat(timespec='seconds'), start.date()
		tags = rule_tags()
		msg = {'ts': ts.isoformat(timespec='seconds'), 'minute': {}, 'day': {}, 'since': since, 'stale': stale}
		for b in BANKS:
			ac_wh, dc_wh, n = acc[b][0] / 3600, acc[b][1] / 3600, acc[b][2]
			d, q, loss = quote(ac_wh, dc_wh, n)
			msg['minute'][b] = {'ac_wh': round(ac_wh, 1), 'dc_wh': round(dc_wh, 1), 'dir': d,
								'quote': round(q, 4) if q is not None else None,
								'loss_w': round(loss) if loss is not None else None,
								'rule': tags.get(b) if b != 'ALL' else None}
			if d:
				z = day[b][d]
				z['ac_wh'] += abs(ac_wh)
				z['dc_wh'] += abs(dc_wh)
				z['minutes'] += 1
		msg['minute']['multi_dc_wh'] = round(multi_dc / 3600, 1)
		for b in BANKS:
			msg['day'][b] = {}
			for d, z in day[b].items():
				q = day_quote(d, z)
				msg['day'][b][d] = {'ac_wh': round(z['ac_wh']), 'dc_wh': round(z['dc_wh']),
									'quote': round(q, 4) if q is not None else None, 'minutes': z['minutes']}
		out.publish(TOPIC_OUT, json.dumps(msg, separators=(',', ':')), retain=True)
		db_store(today, day, since)
		acc = {b: [0.0, 0.0, 0] for b in BANKS}
		multi_dc, stale = 0.0, 0


if __name__ == '__main__':
	main()
