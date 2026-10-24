#!/usr/bin/env python3
"""
victron_dbus2db.py - all Venus OS settings (com.victronenergy.settings) into wagodb.pv_victron_dbus as a
slowly changing dimension (type 2): one row per version of a path, valid_to NULL = current version.

Source: /data/conf/settings.xml of localsettings (the Venus /data bind on this host). The file holds every setting
the services have registered, each with type, default, min, max and silent, so the table shows all possible settings,
not only the changed ones. dbus path = /Settings/<xml path>; xml names starting with '_' + digit are numeric dbus
names (localsettings prefixes them, e.g. Schedule/Charge/_0 = /Settings/CGwacs/BatteryLife/Schedule/Charge/0).

Each run compares the file with the current versions and writes only the differences: a changed path (value,
default, min, max, type or silent) closes its version (valid_to = now) and opens a new one, a path gone from the
file is closed, a new path is opened. No change = no write. Change timestamp (valid_from / valid_to) = mtime of
settings.xml: localsettings writes the file seconds after a change, so it is exact to a few seconds, not to the
5 min cron; several changes of one path between two runs collapse into the last one. valid_from of the first
load is the mtime at that time (2026-10-24 15:50), not the time each setting was made.

active = 1 when the value differs from the default (numbers compared as numbers), i.e. the setting was set on purpose.

DB credentials: ~/.victron2db.cnf (same as victron2db.py).
Cron (pi):  */5 * * * * /usr/bin/python3 /home/pi/python/victron_dbus2db.py >/tmp/victron_dbus2db.log 2>&1

Examples:
  SELECT path, value, default_value FROM pv_victron_dbus_current WHERE active = 1 ORDER BY path;
  SELECT path, value, min_value, max_value FROM pv_victron_dbus_current WHERE section = 'CGwacs';
  SELECT path, value, valid_from, valid_to FROM pv_victron_dbus WHERE path LIKE '%/Hub4Mode' ORDER BY valid_from;
"""
import os
import sys
import xml.etree.ElementTree as ET
from datetime import datetime

import pymysql

SETTINGS_XML = '/data/conf/settings.xml'
DB_CNF = os.path.expanduser('~/.victron2db.cnf')
TABLE = 'pv_victron_dbus'

VERSION_COLS = ('type', 'value', 'default_value', 'min_value', 'max_value', 'silent')

DDL = (f"""CREATE TABLE IF NOT EXISTS {TABLE} (
  path          VARCHAR(255) NOT NULL COMMENT 'dbus path in com.victronenergy.settings',
  valid_from    DATETIME     NOT NULL COMMENT 'change timestamp: mtime of settings.xml when this version was seen first',
  valid_to      DATETIME     NULL COMMENT 'change timestamp of the next version or of the path gone, NULL = current',
  section       VARCHAR(64)  NOT NULL COMMENT 'first element below /Settings (CGwacs, SystemSetup, ...)',
  type          CHAR(1)      NOT NULL COMMENT 'localsettings type: i = int, f = float, s = string',
  value         TEXT         NULL,
  default_value TEXT         NULL,
  min_value     VARCHAR(32)  NULL,
  max_value     VARCHAR(32)  NULL,
  silent        TINYINT      NOT NULL DEFAULT 0 COMMENT '1 = changes are not logged by localsettings',
  active        TINYINT      NOT NULL COMMENT '1 = value differs from the default (set on purpose)',
  portal_id     VARCHAR(16)  NOT NULL,
  PRIMARY KEY (path, valid_from),
  KEY (valid_to),
  KEY (active)
) COMMENT 'Venus OS settings from /data/conf/settings.xml, SCD type 2, filled by victron_dbus2db.py'""",
	f"""CREATE OR REPLACE VIEW {TABLE}_current AS
  SELECT path, section, type, value, default_value, min_value, max_value, silent, active, valid_from, portal_id
  FROM {TABLE} WHERE valid_to IS NULL""")


def dbus_name(tag):
	return tag[1:] if tag.startswith('_') and tag[1:2].isdigit() else tag


def differs(typ, value, default):
	if typ in ('i', 'f'):
		try:
			return float(value) != float(default)
		except (TypeError, ValueError):
			pass
	return (value or '') != (default or '')


def read_settings(fn):
	root = ET.parse(fn).getroot()
	rows = []

	def walk(el, parts):
		for child in el:
			p = parts + [dbus_name(child.tag)]
			if 'type' in child.attrib:
				a = child.attrib
				value = child.text or ''
				rows.append({
					'path': '/Settings/' + '/'.join(p),
					'section': p[0],
					'type': a['type'],
					'value': value,
					'default_value': a.get('default'),
					'min_value': a.get('min'),
					'max_value': a.get('max'),
					'silent': 1 if a.get('silent') == 'True' else 0,
					'active': 1 if differs(a['type'], value, a.get('default')) else 0,
				})
			else:
				walk(child, p)

	walk(root, [])
	return root.get('unique-id') or 'unknown', rows


def write(portal_id, rows, changed):
	"""changed = change timestamp of this state (mtime of settings.xml)"""
	conn = pymysql.connect(read_default_file=DB_CNF, connect_timeout=10)
	try:
		with conn.cursor() as cur:
			for stmt in DDL:
				cur.execute(stmt)
			cur.execute(f"SELECT path, {', '.join(VERSION_COLS)} FROM {TABLE} WHERE valid_to IS NULL")
			current = {r[0]: r[1:] for r in cur.fetchall()}
			new = {r['path']: r for r in rows}
			opened = [r for p, r in new.items() if current.get(p) != tuple(r[c] for c in VERSION_COLS)]
			opened_paths = {r['path'] for r in opened}
			closed = [p for p in current if p not in new or p in opened_paths]
			if closed:
				cur.executemany(f"UPDATE {TABLE} SET valid_to = %s WHERE path = %s AND valid_to IS NULL",
								[(changed, p) for p in closed])
			if opened:
				cols = list(rows[0]) + ['valid_from', 'portal_id']
				cur.executemany(f"INSERT INTO {TABLE} ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})",
								[[r[c] for c in cols[:-2]] + [changed, portal_id] for r in opened])
		conn.commit()
	finally:
		conn.close()
	return opened, [p for p in closed if p not in new]


def main():
	portal_id, rows = read_settings(SETTINGS_XML)
	if not rows:
		sys.exit(f'{SETTINGS_XML}: no settings found')
	changed = datetime.fromtimestamp(int(os.path.getmtime(SETTINGS_XML)))
	opened, gone = write(portal_id, rows, changed)
	print(f'{len(rows)} settings, {sum(r["active"] for r in rows)} active, '
		  f'{len(opened)} new versions, {len(gone)} removed')
	for r in opened[:20]:
		print(f'  {r["path"]} = {r["value"]}')


if __name__ == '__main__':
	main()
