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
into the bm_* columns (setpoints, rules, SoC balancing lead, protection / forced charge flags, surplus);
NULL if the file is missing or older than BM_STATE_MAX_AGE.
EBox pack counters (cycle count per Pytes pack) from the cache of ebox_mqtt.py (EBOX_STAT, `ebox stat <n>`, refreshed
every 6 h) into ebox_p1..p3_cycles; NULL if missing or older than EBOX_STAT_MAX_AGE.
"""

import json
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
BM_STATE_MAX_AGE = 120   # s
BM_STACKS = (('s1', 'STACK1_MUST'), ('s2', 'STACK2_PYTES'))
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
	'cvl_v': ('battery', '/Info/MaxChargeVoltage'),
	'ccl_a': ('battery', '/Info/MaxChargeCurrent'),
	'dcl_a': ('battery', '/Info/MaxDischargeCurrent'),
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
	COLUMNS[f'ac_out_l{ph}_v'] = ('vebus', f'/Ac/Out/L{ph}/V')
	COLUMNS[f'ac_out_l{ph}_w'] = ('vebus', f'/Ac/Out/L{ph}/P')
	COLUMNS[f'grid_l{ph}_w'] = ('system', f'/Ac/Grid/L{ph}/Power')
	COLUMNS[f'consumption_l{ph}_w'] = ('system', f'/Ac/Consumption/L{ph}/Power')

WANTED = {}  # (service, path) -> column
for col, key in COLUMNS.items():
	WANTED[key] = col
BATTERY_PATHS = {path for (service, path) in WANTED if service.startswith('battery')}


def collect():
	"""Return (portal_id, {(service, path): value})."""
	values = {}
	instances = {}
	batteries = {}  # instance -> {path: value}
	portal = {'id': None}

	def on_connect(client, userdata, flags, reason_code, properties):
		client.subscribe('N/+/system/0/Serial')
		client.subscribe('N/+/vebus/#')
		client.subscribe('N/+/battery/#')
		client.subscribe('N/+/system/0/#')
		client.subscribe('N/+/settings/0/Settings/CGwacs/AcPowerSetPoint')
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
		if service == 'battery':
			if path != '/ProductName' and path not in BATTERY_PATHS:
				return
		else:
			if service == 'vebus':
				first = instances.setdefault(service, instance)
				if instance != first:
					return
			if (service, path) not in WANTED:
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
	return portal['id'] or fallback_portal_id(), values


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
		row[col] = v
	return row


def batmonitor_columns():
	"""bm_* columns from the batmonitor state file, all None if it is missing or stale"""
	cols = {f'bm_l{ph}_{k}': None for ph in (1, 2, 3) for k in ('sp_w', 'rule')}
	cols.update({'bm_balance_lead': None, 'bm_surplus_w': None})
	cols.update({f'bm_{s}_{k}': None for s, _ in BM_STACKS for k in ('prot', 'force')})
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
	for s, name in BM_STACKS:
		cols[f'bm_{s}_prot'] = st.get('prot', {}).get(name)
		cols[f'bm_{s}_force'] = st.get('force', {}).get(name)
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


def main():
	portal_id, values = collect()
	row = build_row(portal_id, values)
	filled = sum(1 for k, v in row.items() if v is not None and k not in ('ts', 'portal_id'))
	row.update(batmonitor_columns())
	row.update(ebox_stat_columns())
	if '--print' in sys.argv:
		for k, v in row.items():
			print(f'{k:26} {v}')
		print(f'{filled} of {len(WANTED)} values')
		return
	if filled == 0:
		print(f'{row["ts"]} {portal_id}: no Venus values received, nothing written')
		sys.exit(1)
	write(row)
	print(f'{row["ts"]} {portal_id}: {filled} of {len(WANTED)} values written '
		  f'(soc={row["soc"]} vebus_state={row["vebus_state"]} ac_in_l1_w={row["ac_in_l1_w"]})')


if __name__ == '__main__':
	main()
