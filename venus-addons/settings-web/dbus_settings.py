#!/usr/bin/env python3
"""
dbus_settings.py - helper of the settings web page (index.php): reads and writes com.victronenergy.settings over dbus.
Runs inside Venus as the php-fpm user (the dbus policy allows every user to talk to the settings service).

  dbus_settings.py list                     -> JSON {path: {value, type, default, min, max}}
  dbus_settings.py set <path> <value>       -> JSON {ok, value, error}; type taken from the current value,
                                               localsettings itself rejects values outside min/max
"""
import json
import math
import sys

import dbus

SERVICE = 'com.victronenergy.settings'
IFACE = 'com.victronenergy.BusItem'


def type_of(v):
	if isinstance(v, (dbus.Int16, dbus.Int32, dbus.Int64, dbus.UInt16, dbus.UInt32, dbus.UInt64, dbus.Byte, dbus.Boolean)):
		return 'i'
	if isinstance(v, dbus.Double):
		return 'f'
	return 's'


def plain(v):
	"""JSON-safe python value; inf/nan as text (json.dumps would write invalid Infinity)"""
	t = type_of(v)
	if t == 'f':
		f = float(v)
		return str(f) if math.isinf(f) or math.isnan(f) else f
	return int(v) if t == 'i' else str(v)


def items(bus):
	d = bus.get_object(SERVICE, '/').GetItems(dbus_interface=IFACE)
	out = {}
	for path, p in d.items():
		v = p.get('Value')
		out[str(path)] = {
			'value': plain(v),
			'type': type_of(v),
			'default': plain(p['Default']) if 'Default' in p else None,
			'min': plain(p['Min']) if 'Min' in p else None,
			'max': plain(p['Max']) if 'Max' in p else None,
		}
	return out


def set_value(bus, path, text):
	obj = bus.get_object(SERVICE, path)
	cur = obj.GetValue(dbus_interface=IFACE)
	t = type_of(cur)
	try:
		v = dbus.Int32(int(float(text)), variant_level=1) if t == 'i' else \
			dbus.Double(float(text), variant_level=1) if t == 'f' else dbus.String(text, variant_level=1)
	except ValueError:
		return {'ok': False, 'error': f'not a number: {text}', 'value': plain(cur)}
	rc = obj.SetValue(v, dbus_interface=IFACE)
	back = plain(obj.GetValue(dbus_interface=IFACE))
	return {'ok': rc == 0, 'error': None if rc == 0 else f'SetValue returned {int(rc)} (out of range or not allowed)',
			'value': back}


def main():
	bus = dbus.SystemBus()
	if len(sys.argv) == 2 and sys.argv[1] == 'list':
		print(json.dumps(items(bus)))
	elif len(sys.argv) == 4 and sys.argv[1] == 'set' and sys.argv[2].startswith('/Settings/'):
		print(json.dumps(set_value(bus, sys.argv[2], sys.argv[3])))
	else:
		print(json.dumps({'ok': False, 'error': 'usage: list | set /Settings/<path> <value>'}))
		sys.exit(2)


if __name__ == '__main__':
	try:
		main()
	except dbus.exceptions.DBusException as e:
		print(json.dumps({'ok': False, 'error': e.get_dbus_message() or str(e)}))
		sys.exit(1)
