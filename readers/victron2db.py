#!/usr/bin/env python3
"""
victron2db.py - one row per minute of the Victron system into wagodb.pv_victron (sql/pv_victron.sql).

Reads the local Venus OS MQTT broker (nspawn subsystem on this host, 127.0.0.1:1883): sends the keepalive,
collects the N/<portal>/{vebus,battery,system,settings}/... values for COLLECT_SECONDS and writes them with
INSERT ... ON DUPLICATE KEY UPDATE (unique portal_id + ts), so a second run in the same minute only updates.

DB credentials: ~/.victron2db.cnf in my.cnf format ([client] host, user, password, database).
Cron (pi):  * * * * * /usr/bin/python3 /home/pi/python/victron2db.py >/tmp/victron2db.log 2>&1
Options:    --print   show the row, do not write
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

# column -> (service, path); service 'vebus' / 'battery' take the first instance seen
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


def collect():
	"""Return (portal_id, {(service, path): value})."""
	values = {}
	instances = {}
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
		if service in ('vebus', 'battery'):
			first = instances.setdefault(service, instance)
			if instance != first:
				return
		if (service, path) not in WANTED:
			return
		try:
			v = json.loads(msg.payload).get('value')
		except (ValueError, AttributeError):
			return
		values[(service, path)] = v

	client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f'victron2db-{os.getpid()}')
	client.on_connect = on_connect
	client.on_message = on_message
	client.connect(MQTT_HOST, 1883, 30)
	client.loop_start()
	time.sleep(COLLECT_SECONDS)
	client.loop_stop()
	client.disconnect()
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
