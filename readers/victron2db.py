#!/usr/bin/env python3
"""
victron2db.py - one row per minute of the Victron system into wagodb.pv_victron (sql/pv_victron.sql).

Reads the local Venus OS MQTT broker (nspawn subsystem on this host, 127.0.0.1:1883): sends the keepalive,
collects the N/<portal>/{vebus,battery,system,settings}/... values for COLLECT_SECONDS and writes them with
INSERT ... ON DUPLICATE KEY UPDATE (unique portal_id + ts), so a second run in the same minute only updates.

DB credentials: ~/.victron2db.cnf in my.cnf format ([client] host, user, password, database).
Cron (pi):  * * * * * /usr/bin/python3 /home/pi/python/victron2db.py >/tmp/victron2db.log 2>&1
Options:    --print   show the row, do not write
batmonitor: its decisions of the last cycle come from /data/batmonitor/state.json (Venus /data = host /data)
into the bm_* columns (setpoints, rules, SoC balancing lead, protection / forced charge flags, surplus,
5 min average DC power and forecast time of 100 % SoC per stack);
NULL if the file is missing or older than BM_STATE_MAX_AGE.
EBox pack counters (cycle count per Pytes pack) from the cache of ebox_mqtt.py (EBOX_STAT, `ebox stat <n>`, refreshed
every 6 h) into ebox_p1..p3_cycles; NULL if missing or older than EBOX_STAT_MAX_AGE.
Venus notification list (platform/0/Notifications, the last 20 alarms/warnings) into wagodb.pv_victron_notifications
(python/create_pv_victron_notifications.py): upsert on portal + time + device + text, so each notification is stored once
and only its active / acknowledged state follows (2026-10-20).
"""

import json
import hashlib
import os
import sys
import time
from datetime import datetime

import paho.mqtt.client as mqtt
import pymysql

MQTT_HOST = '127.0.0.1'
COLLECT_SECONDS = 5
DB_CNF = os.path.expanduser('~/.victron2db.cnf')
BM_STATE = '/data/batmonitor/state.json'
BM_DIR = '/data/batmonitor'
# 2026-11-05 (user: observed states only within one logic): short hash of what decides the rule texts
BM_LOGIC_FILES = ('bm_logic.py', 'bm_tables.h', 'bm_decisions.dmn', 'batmonitor.py', 'env')
BM_STATE_MAX_AGE = 120   # s
BM_STACKS = (('s1', 'BATTERY'), ('s2', 'STACK2_PYTES'))   # 0.77-py: one parallel bank -> s1, s2 stays NULL
EBOX_STAT = '/home/pi/sofar/ebox_stat.json'
EBOX_STAT_MAX_AGE = 2 * 86400   # s
EBOX_PACKS = 3

# column -> (service, path); 'vebus' takes the first instance seen; the batteries are told apart by /ProductName:
# 'battery' = Speicher B (EBox, dbus-ebox-battery), 'battery_a' = Speicher A (CAN-bus BMS on can0)
BATTERY_ROLES = (
	('battery', 'EBox'),
	('battery_a', 'CAN-bus BMS'),
)
COLUMNS = {
	'vebus_state': ('vebus', '/State'),
	'vebus_mode': ('vebus', '/Mode'),
	'vebus_error': ('vebus', '/VebusError'),
	'active_input': ('vebus', '/Ac/ActiveIn/ActiveInput'),
	'ac_in_limit_a': ('vebus', '/Ac/In/1/CurrentLimit'),
	'ac_in_f_hz': ('vebus', '/Ac/ActiveIn/L1/F'),
	'dc_v': ('vebus', '/Dc/0/Voltage'),
	'dc_a': ('vebus', '/Dc/0/Current'),
	'charge_state': ('vebus', '/VebusChargeState'),
	'e_acin_to_inverter_kwh': ('vebus', '/Energy/AcIn1ToInverter'),
	'e_inverter_to_acin_kwh': ('vebus', '/Energy/InverterToAcIn1'),
	'e_inverter_to_acout_kwh': ('vebus', '/Energy/InverterToAcOut'),
	'e_acin_to_acout_kwh': ('vebus', '/Energy/AcIn1ToAcOut'),
	'soc': ('battery', '/Soc'),
	'bat_v': ('battery', '/Dc/0/Voltage'),
	'bat_a': ('battery', '/Dc/0/Current'),
	'bat_w': ('battery', '/Dc/0/Power'),
	'cell_max_v': ('battery', '/System/MaxCellVoltage'),
	'cell_min_v': ('battery', '/System/MinCellVoltage'),
	'cvl_v': ('battery', '/Info/Pack/MaxChargeVoltage'),          # dbus-ebox-battery 1.5: Pytes alone, /Info/* = both banks
	'ccl_a': ('battery', '/Info/Pack/MaxChargeCurrent'),
	'dcl_a': ('battery', '/Info/Pack/MaxDischargeCurrent'),
	'charge_request': ('battery', '/Info/ChargeRequest'),
	'a_soc': ('battery_a', '/Soc'),
	'a_soh': ('battery_a', '/Soh'),
	'a_bat_v': ('battery_a', '/Dc/0/Voltage'),
	'a_bat_a': ('battery_a', '/Dc/0/Current'),
	'a_bat_w': ('battery_a', '/Dc/0/Power'),
	'a_temp_c': ('battery_a', '/Dc/0/Temperature'),
	'a_cell_max_v': ('battery_a', '/System/MaxCellVoltage'),
	'a_cell_min_v': ('battery_a', '/System/MinCellVoltage'),
	'a_cvl_v': ('battery_a', '/Info/MaxChargeVoltage'),
	'a_ccl_a': ('battery_a', '/Info/MaxChargeCurrent'),
	'a_dcl_a': ('battery_a', '/Info/MaxDischargeCurrent'),
	'system_state': ('system', '/SystemState/State'),
	'ess_setpoint_w': ('settings', '/Settings/CGwacs/AcPowerSetPoint'),
}
for ph in (1, 2, 3):
	COLUMNS[f'ac_in_l{ph}_v'] = ('vebus', f'/Ac/ActiveIn/L{ph}/V')
	COLUMNS[f'ac_in_l{ph}_w'] = ('vebus', f'/Ac/ActiveIn/L{ph}/P')
	COLUMNS[f'ac_in_l{ph}_i'] = ('vebus', f'/Ac/ActiveIn/L{ph}/I')  # + = from the grid (2026-10-27)
	COLUMNS[f'ac_out_l{ph}_v'] = ('vebus', f'/Ac/Out/L{ph}/V')
	COLUMNS[f'ac_out_l{ph}_w'] = ('vebus', f'/Ac/Out/L{ph}/P')
	COLUMNS[f'ac_out_l{ph}_s'] = ('vebus', f'/Ac/Out/L{ph}/S')     # apparent power = V x I (coarse below ~1 A)
	COLUMNS[f'ac_out_l{ph}_i'] = ('vebus', f'/Ac/Out/L{ph}/I')
	COLUMNS[f'grid_l{ph}_w'] = ('system', f'/Ac/Grid/L{ph}/Power')
	COLUMNS[f'consumption_l{ph}_w'] = ('system', f'/Ac/Consumption/L{ph}/Power')
	# NA-Schutz (grid code interface protection) of the unit on this phase: VE.Bus device ph-1 (0 = L1 master),
	# its newest log entry (flags: dim_vebus_ip_flag) and the VE.Bus network quality counter (2026-10-20)
	COLUMNS[f'ac_in_l{ph}_f_hz'] = ('vebus', f'/Ac/ActiveIn/L{ph}/F')
	COLUMNS[f'na_l{ph}_flags'] = ('vebus', f'/Devices/{ph - 1}/InterfaceProtectionLog/0/ErrorFlags')
	COLUMNS[f'na_l{ph}_at'] = ('vebus', f'/Devices/{ph - 1}/InterfaceProtectionLog/0/Time')
	COLUMNS[f'vebus_nq_l{ph}'] = ('vebus', f'/Devices/{ph - 1}/ExtendStatus/VeBusNetworkQualityCounter')
# front panel LEDs; Venus publishes them for the VE.Bus master only (dev0 = L1): 0 off, 1 on, 2 blink, 3 blink inverted
for led, path in (('mains', 'Mains'), ('bulk', 'Bulk'), ('absorption', 'Absorption'), ('float', 'Float'),
				  ('inverter', 'Inverter'), ('overload', 'Overload'), ('low_battery', 'LowBattery'), ('temperature', 'Temperature')):
	COLUMNS[f'led_{led}'] = ('vebus', f'/Leds/{path}')
EPOCH_COLUMNS = {f'na_l{ph}_at' for ph in (1, 2, 3)}   # unix time -> DATETIME

WANTED = {}  # (service, path) -> column
for col, key in COLUMNS.items():
	WANTED[key] = col
BATTERY_PATHS = {path for (service, path) in WANTED if service.startswith('battery')}

# 2026-11-05 (user: "keine mail nur in db loggen, dass eine fremde aenderung entdeckt wurde"): Venus settings our
# software keeps at one value. Another value = a change from outside (Venus GUI, settings web, ...): one row in
# pv_victron_foreign_change when it leaves the value ("foreign") and when it is back ("restored"). Nothing is reset.
# Not watched: /Settings/CGwacs/MaxChargePower, dbus-pcc-grid changes it itself (SOC1 gate).
WATCH = {
	('settings', '/Settings/CGwacs/Hub4Mode'): 3,                 # batmonitor sets 3 once at its start
	('settings', '/Settings/SystemSetup/SharedVoltageSense'): 0,  # off since 2026-10-09 (with SVS L1 was throttled)
}
WATCH_STATE = os.path.expanduser('~/.victron2db_watch.json')   # path -> last seen value


NOTIFICATION_FIELDS = {	# platform/0/Notifications/<n>/<field> -> pv_victron_notifications column
	'DateTime': 'notified_at', 'Type': 'type', 'DeviceName': 'device_name', 'Description': 'description',
	'Value': 'value', 'AlarmValue': 'alarm_value', 'Service': 'service', 'Trigger': 'trigger',
	'Active': 'active', 'Acknowledged': 'acknowledged', 'Silenced': 'silenced',
}


def collect():
	"""Return (portal_id, {(service, path): value}, {n: {field: value}} of the Venus notification list)."""
	values = {}
	instances = {}
	batteries = {}  # instance -> {path: value}
	notes = {}      # notification slot -> {field: value}
	portal = {'id': None}

	def on_connect(client, userdata, flags, reason_code, properties):
		client.subscribe('N/+/system/0/Serial')
		client.subscribe('N/+/vebus/#')
		client.subscribe('N/+/battery/#')
		client.subscribe('N/+/system/0/#')
		client.subscribe('N/+/settings/0/Settings/CGwacs/AcPowerSetPoint')
		for _, path in WATCH:
			client.subscribe('N/+/settings/0' + path)
		client.subscribe('N/+/platform/0/Notifications/+/+')
		# Venus only publishes after a keepalive; its portal id is the eth0 MAC (the container shares
		# the host network), so the keepalive can go out right away
		client.publish(f'R/{fallback_portal_id()}/keepalive', '')

	def on_message(client, userdata, msg):
		parts = msg.topic.split('/', 4)          # N, portal, service, instance, path
		if len(parts) < 5:
			return
		_, pid, service, instance, path = parts
		path = '/' + path
		if service == 'system' and path == '/Serial':
			portal['id'] = pid
			return
		if service == 'platform':
			p = path.split('/')                  # '', 'Notifications', <n>, <field>
			if len(p) == 4 and p[2].isdigit() and p[3] in NOTIFICATION_FIELDS:
				try:
					notes.setdefault(int(p[2]), {})[p[3]] = json.loads(msg.payload).get('value')
				except (ValueError, AttributeError):
					pass
			return
		if service == 'battery':
			if path != '/ProductName' and path not in BATTERY_PATHS:
				return
		else:
			if service == 'vebus':
				first = instances.setdefault(service, instance)
				if instance != first:
					return
			if (service, path) not in WANTED and (service, path) not in WATCH:
				return
		try:
			v = json.loads(msg.payload).get('value')
		except (ValueError, AttributeError):
			return
		if service == 'battery':
			batteries.setdefault(instance, {})[path] = v
		else:
			values[(service, path)] = v

	client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f'victron2db-{os.getpid()}')
	client.on_connect = on_connect
	client.on_message = on_message
	client.connect(MQTT_HOST, 1883, 30)
	client.loop_start()
	time.sleep(COLLECT_SECONDS)
	client.loop_stop()
	client.disconnect()
	for role, prefix in BATTERY_ROLES:
		for bat in batteries.values():
			if str(bat.get('/ProductName') or '').startswith(prefix):
				for path, v in bat.items():
					values[(role, path)] = v
				break
	return portal['id'] or fallback_portal_id(), values, notes


def fallback_portal_id():
	try:
		with open('/sys/class/net/eth0/address') as f:
			return f.read().strip().replace(':', '')
	except OSError:
		return 'unknown'


def build_row(portal_id, values):
	row = {'ts': datetime.now().replace(second=0, microsecond=0), 'portal_id': portal_id}
	for key, col in WANTED.items():
		v = values.get(key)
		if isinstance(v, (list, dict)) or v == '':
			v = None
		if col in EPOCH_COLUMNS and v is not None:
			v = datetime.fromtimestamp(v)
		row[col] = v
	return row


def bm_logic_id():
	"""sha256[:12] over BM_LOGIC_FILES in BM_DIR (a missing file counts as empty), None without bm_logic.py.
	Every change of the code, the tables or the env gives a new id, so docs/gen_docs.py shows only states seen
	with the logic that runs now."""
	h = hashlib.sha256()
	found = False
	for name in BM_LOGIC_FILES:
		try:
			with open(os.path.join(BM_DIR, name), 'rb') as f:
				data = f.read()
			found = found or name == 'bm_logic.py'
		except OSError:
			data = b''
		h.update(name.encode() + b'\0' + data + b'\0')
	return h.hexdigest()[:12] if found else None


def batmonitor_columns():
	"""bm_* columns from the batmonitor state file, all None if it is missing or stale"""
	cols = {f'bm_l{ph}_{k}': None for ph in (1, 2, 3) for k in ('sp_w', 'rule')}
	cols.update({'bm_balance_lead': None, 'bm_surplus_w': None, 'bm_d1_id': None, 'bm_logic': None})
	cols.update({f'bm_{s}_{k}': None for s, _ in BM_STACKS for k in ('prot', 'force', 'chg_avg5_w', 'full_at')})
	try:
		with open(BM_STATE) as f:
			st = json.load(f)
	except (OSError, ValueError):
		return cols
	if time.time() - st.get('ts', 0) > BM_STATE_MAX_AGE:
		return cols
	for ph in (1, 2, 3):
		cols[f'bm_l{ph}_sp_w'] = st.get('sp', {}).get(f'L{ph}')
		cols[f'bm_l{ph}_rule'] = st.get('rule', {}).get(f'L{ph}')
	cols['bm_balance_lead'] = st.get('balance_lead')
	cols['bm_surplus_w'] = st.get('surplus_w')
	cols['bm_d1_id'] = st.get('d1_id')                          # batmonitor >= 0.60-c: D1 cell as a bit mask
	cols['bm_logic'] = bm_logic_id()
	for s, name in BM_STACKS:
		cols[f'bm_{s}_prot'] = st.get('prot', {}).get(name)
		cols[f'bm_{s}_force'] = st.get('force', {}).get(name)
		fc = st.get('forecast', {}).get(name, {})
		cols[f'bm_{s}_chg_avg5_w'] = fc.get('avg5_w')
		cols[f'bm_{s}_full_at'] = datetime.fromtimestamp(fc['full_at']) if fc.get('full_at') else None
	return cols


def ebox_stat_columns():
	"""ebox_p<n>_cycles from the ebox_mqtt.py cache, all None if it is missing or stale"""
	cols = {f'ebox_p{n}_cycles': None for n in range(1, EBOX_PACKS + 1)}
	try:
		with open(EBOX_STAT) as f:
			st = json.load(f)
	except (OSError, ValueError):
		return cols
	if time.time() - st.get('ts', 0) > EBOX_STAT_MAX_AGE:
		return cols
	for n in range(1, EBOX_PACKS + 1):
		cols[f'ebox_p{n}_cycles'] = st.get('packs', {}).get(str(n), {}).get('cycles')
	return cols


def write(row):
	cols = list(row)
	sql = (f"INSERT INTO pv_victron ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) "
		   f"ON DUPLICATE KEY UPDATE {', '.join(f'{c}=VALUES({c})' for c in cols if c not in ('ts', 'portal_id'))}")
	conn = pymysql.connect(read_default_file=DB_CNF, connect_timeout=10)
	try:
		with conn.cursor() as cur:
			cur.execute(sql, [row[c] for c in cols])
		conn.commit()
	finally:
		conn.close()


def notification_rows(portal_id, notes):
	"""pv_victron_notifications rows of the slots that carry a notification (DateTime set)"""
	rows = []
	for n in sorted(notes):
		f = notes[n]
		if not f.get('DateTime'):
			continue
		row = {'portal_id': portal_id}
		for field, col in NOTIFICATION_FIELDS.items():
			v = f.get(field)
			if col == 'notified_at':
				v = datetime.fromtimestamp(v)
			elif col in ('device_name', 'description'):
				v = '' if v is None else str(v)
			elif col in ('value', 'alarm_value', 'service', 'trigger') and v is not None:
				v = str(v)
			row[col] = v
		rows.append(row)
	return rows


def write_notifications(rows):
	"""upsert: a notification already stored only gets its active / acknowledged / silenced state updated"""
	if not rows:
		return
	cols = list(rows[0])
	sql = (f"INSERT INTO pv_victron_notifications ({', '.join(f'`{c}`' for c in cols)}) "
		   f"VALUES ({', '.join(['%s'] * len(cols))}) ON DUPLICATE KEY UPDATE "
		   f"{', '.join(f'`{c}`=VALUES(`{c}`)' for c in ('active', 'acknowledged', 'silenced', 'value'))}")
	conn = pymysql.connect(read_default_file=DB_CNF, connect_timeout=10)
	try:
		with conn.cursor() as cur:
			cur.executemany(sql, [[r[c] for c in cols] for r in rows])
		conn.commit()
	finally:
		conn.close()


def same(a, b):
	try:
		return float(a) == float(b)
	except (TypeError, ValueError):
		return a == b


def watch_rows(values):
	"""(event rows, new state): a row when a watched setting differs from its expected value for the first time,
	changes again while off, or comes back; a setting not received this minute is skipped"""
	try:
		with open(WATCH_STATE) as f:
			last = json.load(f)
	except (OSError, ValueError):
		last = {}
	state, rows = dict(last), []
	ts = datetime.now().replace(second=0, microsecond=0)
	for key, want in WATCH.items():
		if key not in values:
			continue
		path, v = key[1], values[key]
		known = path in last
		prev = last.get(path)
		if (not known and not same(v, want)) or (known and not same(v, prev)):
			rows.append({'ts': ts, 'path': path, 'expected': str(want), 'seen': None if v is None else str(v),
						 'previous': None if prev is None else str(prev),
						 'kind': 'restored' if same(v, want) else 'foreign'})
		state[path] = v
	return rows, state


def write_watch(rows, state):
	if rows:
		cols = list(rows[0])
		sql = (f"INSERT INTO pv_victron_foreign_change ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})")
		conn = pymysql.connect(read_default_file=DB_CNF, connect_timeout=10)
		try:
			with conn.cursor() as cur:
				cur.executemany(sql, [[r[c] for c in cols] for r in rows])
			conn.commit()
		finally:
			conn.close()
	with open(WATCH_STATE + '.tmp', 'w') as f:      # only after the rows are stored: else again next minute
		json.dump(state, f)
	os.replace(WATCH_STATE + '.tmp', WATCH_STATE)


def main():
	portal_id, values, notes = collect()
	notifications = notification_rows(portal_id, notes)
	row = build_row(portal_id, values)
	filled = sum(1 for k, v in row.items() if v is not None and k not in ('ts', 'portal_id'))
	row.update(batmonitor_columns())
	row.update(ebox_stat_columns())
	if '--print' in sys.argv:
		for k, v in row.items():
			print(f'{k:26} {v}')
		print(f'{filled} of {len(WANTED)} values')
		for r in notifications:
			print(f"notification {r['notified_at']} type {r['type']} {r['device_name']}: {r['description']}")
		for key, want in WATCH.items():
			print(f"watch {key[1]}: {values.get(key, 'not received')} (expected {want})")
		print('watch rows:', watch_rows(values)[0])
		return
	if filled == 0:
		print(f'{row["ts"]} {portal_id}: no Venus values received, nothing written')
		sys.exit(1)
	write(row)
	try:
		write_notifications(notifications)
	except pymysql.MySQLError as e:
		print(f'notifications not written: {e}')
	try:
		rows, state = watch_rows(values)
		write_watch(rows, state)
		for r in rows:
			print(f"foreign change {r['kind']}: {r['path']} = {r['seen']} (expected {r['expected']}, was {r['previous']})")
	except (pymysql.MySQLError, OSError) as e:
		print(f'watch not written: {e}')
	print(f'{row["ts"]} {portal_id}: {filled} of {len(WANTED)} values written '
		  f'(soc={row["soc"]} vebus_state={row["vebus_state"]} ac_in_l1_w={row["ac_in_l1_w"]}), '
		  f'{len(notifications)} notifications')


if __name__ == '__main__':
	main()
