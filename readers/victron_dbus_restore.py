#!/usr/bin/env python3
"""
victron_dbus_restore.py - restore Venus OS settings to the state of a past time from wagodb.pv_victron_dbus
(SCD type 2, filled by victron_dbus2db.py), path by path via dbus SetValue, without restarting Venus.

Dry run by default: lists the differences between the state at --at and the live settings (/data/conf/settings.xml).
--apply writes the normal ones and reads each back. Never written:
  - Venus-managed values (EXCLUDE, and every silent setting): BatteryLife state, gauge maxima, ...
  - critical paths (CRITICAL: ESS mode, DVCC, battery / BMS selection, CAN, security) unless named one by one
    with --critical <path>; Hub4Mode changes while batmonitor runs caused the 12 kW grid draw of 2026-10-11
  - paths missing now (SetValue cannot create a setting; the owning service registers it) and paths that did not
    exist at --at (new since): both only listed
Runs on the Venus host as pi (sudo for nsenter into the container), DB credentials ~/.victron2db.cnf.

Examples:
  victron_dbus_restore.py --at '2026-10-24 15:50:33'
  victron_dbus_restore.py --at '2026-10-24 15:50:33' --path '/Settings/CGwacs/%' --apply
  victron_dbus_restore.py --at '2026-10-24 15:50:33' --apply --critical /Settings/CGwacs/Hub4Mode
"""
import argparse
import fnmatch
import os
import subprocess
import sys

import pymysql

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from victron_dbus2db import DB_CNF, SETTINGS_XML, TABLE, differs, read_settings

SERVICE = 'com.victronenergy.settings'

# maintained by Venus itself, a restore would only fight it
EXCLUDE = (
	'/Settings/CGwacs/BatteryLife/State',
	'/Settings/CGwacs/BatteryLife/SocLimit',
	'/Settings/CGwacs/BatteryLife/DischargedTime',
	'/Settings/CGwacs/BatteryLife/Flags',
	'/Settings/Gui/Gauges/*',
	'/Settings/Gui2/OnBoarding',
	'/Settings/System/ActiveNetworkConnection',
	'/Settings/Vecan/*/FreeIdentityNumber',
)

# changes the control of the Multis or what Venus sees as battery: only with --critical <path>
CRITICAL = (
	'/Settings/CGwacs/Hub4Mode',
	'/Settings/CGwacs/MaxChargePower',
	'/Settings/CGwacs/MaxDischargePower',
	'/Settings/CGwacs/AcPowerSetPoint',
	'/Settings/CGwacs/BatteryLife/MinimumSocLimit',
	'/Settings/Services/Bol',
	'/Settings/SystemSetup/*',
	'/Settings/Canbus/*',
	'/Settings/CanBms/*',
	'/Settings/System/SecurityProfile',
)


def matches(path, patterns):
	return any(fnmatch.fnmatchcase(path, p) for p in patterns)


def state_at(at, like):
	conn = pymysql.connect(read_default_file=DB_CNF, connect_timeout=10)
	try:
		with conn.cursor(pymysql.cursors.DictCursor) as cur:
			cur.execute(f"SELECT path, type, value, silent FROM {TABLE} "
						"WHERE valid_from <= %s AND (valid_to IS NULL OR valid_to > %s) AND path LIKE %s",
						(at, at, like))
			rows = {r['path']: r for r in cur.fetchall()}
			cur.execute(f"SELECT MIN(valid_from) FROM {TABLE}")
			first = cur.fetchone()['MIN(valid_from)']
	finally:
		conn.close()
	return rows, first


def venus(*cmd):
	leader = subprocess.run(['sudo', 'machinectl', 'show', 'venus', '-p', 'Leader', '--value'],
							capture_output=True, text=True, timeout=10).stdout.strip()
	if not leader:
		sys.exit('Venus container not running')
	return subprocess.run(['sudo', 'nsenter', '-t', leader, '-a', *cmd], capture_output=True, text=True, timeout=15)


def dbus_arg(typ, value):
	"""value as for the Venus dbus tool: %<number> for numbers, plain text for strings"""
	if typ == 'i':
		return f'%{int(float(value))}'
	if typ == 'f':
		return f'%{float(value)}'
	return value


def set_value(path, typ, value):
	r = venus('dbus', '-y', SERVICE, path, 'SetValue', dbus_arg(typ, value))
	back = venus('dbus', '-y', SERVICE, path, 'GetValue').stdout.strip().splitlines()
	return r.returncode, (back[-1] if back else '?')


def main():
	ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
	ap.add_argument('--at', required=True, help="target time 'YYYY-MM-DD HH:MM:SS'")
	ap.add_argument('--path', default='/Settings/%', help="SQL LIKE filter on the path (default all)")
	ap.add_argument('--apply', action='store_true', help='write (default: dry run)')
	ap.add_argument('--critical', action='append', default=[], metavar='PATH',
					help='also write this critical path (exact path, repeat for more)')
	a = ap.parse_args()

	target, first = state_at(a.at, a.path)
	if not target:
		sys.exit(f'no settings at {a.at} (history starts {first})')
	_, live_rows = read_settings(SETTINGS_XML)
	live = {r['path']: r for r in live_rows if fnmatch.fnmatchcase(r['path'], a.path.replace('%', '*').replace('_', '?'))}

	todo, skipped, missing = [], [], []
	for path, t in sorted(target.items()):
		now = live.get(path)
		if now is None:
			missing.append(path)
			continue
		if not differs(t['type'], now['value'], t['value']):
			continue
		if matches(path, EXCLUDE) or t['silent']:
			skipped.append((path, now['value'], t['value'], 'venus-managed' + (' (silent)' if t['silent'] else '')))
		elif matches(path, CRITICAL) and path not in a.critical:
			skipped.append((path, now['value'], t['value'], 'critical, needs --critical'))
		else:
			todo.append((path, t['type'], now['value'], t['value']))
	new = sorted(p for p in live if p not in target)

	print(f"state at {a.at} vs live {SETTINGS_XML}: {len(todo)} to set, {len(skipped)} skipped, "
		  f"{len(missing)} missing now, {len(new)} new since")
	for path, typ, now, want in todo:
		print(f"  SET   {path}: {now} -> {want}")
	for path, now, want, why in skipped:
		print(f"  SKIP  {path}: {now} -> {want} ({why})")
	for path in missing:
		print(f"  GONE  {path} = {target[path]['value']} (not registered now, cannot be set)")
	for path in new:
		print(f"  NEW   {path} = {live[path]['value']} (did not exist at {a.at}, left as is)")
	unknown = [p for p in a.critical if p not in [t[0] for t in todo]]
	if unknown:
		print(f"  note: --critical without a difference: {', '.join(unknown)}")

	if not a.apply:
		if todo:
			print('dry run, nothing written; --apply to write')
		return
	failed = 0
	for path, typ, now, want in todo:
		rc, back = set_value(path, typ, want)
		ok = rc == 0 and not differs(typ, back.strip("'"), want)
		failed += not ok
		print(f"  {'ok ' if ok else 'ERR'} {path} = {back}")
	sys.exit(1 if failed else 0)


if __name__ == '__main__':
	main()
