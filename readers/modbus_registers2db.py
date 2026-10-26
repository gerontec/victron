#!/usr/bin/env python3
"""Load the Venus OS Modbus-TCP register map (dbus-modbustcp attributes.csv) into wagodb.pv_victron_modbus.

One row per Venus version, service and register (sql/pv_victron_modbus.sql); a reload of the same version replaces
its rows, so the table holds every version loaded so far and their differences are a join.

    modbus_registers2db.py ATTRIBUTES_CSV VERSION      # e.g. attributes.csv v3.81
On the Pi the file is root-only inside the Venus rootfs:
    sudo cat /var/lib/machines/venus/opt/victronenergy/dbus-modbustcp/attributes.csv > /tmp/attributes.csv
    python3 modbus_registers2db.py /tmp/attributes.csv "$(sudo head -1 /var/lib/machines/venus/opt/victronenergy/version)"
DB login: ~/.victron2db.cnf (as victron2db.py).
"""

import csv
import os
import re
import sys

import pymysql

DB_CNF = os.path.expanduser('~/.victron2db.cnf')


def words(data_type):
	m = re.match(r'(string|reserved)\[(\d+)\]', data_type)
	if m:
		return int(m.group(2))
	return 2 if data_type in ('uint32', 'int32') else 1


def rows(path, version):
	with open(path, newline='') as f:
		for service, dbus_path, dbus_type, desc, reg, data_type, scale, access in csv.reader(f):
			yield (version, service, int(reg), dbus_path, dbus_type or None, desc or None, data_type,
				   words(data_type), float(scale) if scale else None, access)


def main():
	if len(sys.argv) != 3:
		sys.exit(__doc__)
	path, version = sys.argv[1], sys.argv[2].strip()
	data = list(rows(path, version))
	con = pymysql.connect(read_default_file=DB_CNF, connect_timeout=10)
	with con.cursor() as c:
		c.execute("DELETE FROM pv_victron_modbus WHERE venus_version = %s", (version,))
		c.executemany("""INSERT INTO pv_victron_modbus (venus_version, service, reg_address, dbus_path, dbus_type,
			description, data_type, words, scale, access) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""", data)
	con.commit()
	con.close()
	print(f"{version}: {len(data)} registers from {path}")


if __name__ == '__main__':
	main()
