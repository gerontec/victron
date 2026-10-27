#!/usr/bin/env python3
"""offline check of dbus-ebox-battery 1.5 publish_limits with stubbed dbus / GLib / vedbus"""
import importlib.util, sys, types

class Arr(list):
	pass

class DBusException(Exception):
	pass

dbus = types.ModuleType('dbus')
dbus.Array = Arr
dbus.exceptions = types.SimpleNamespace(DBusException=DBusException)
dbus.SystemBus = lambda: None
sys.modules['dbus'] = dbus
ml = types.ModuleType('dbus.mainloop.glib'); ml.DBusGMainLoop = None
sys.modules['dbus.mainloop'] = types.ModuleType('dbus.mainloop'); sys.modules['dbus.mainloop.glib'] = ml
gi = types.ModuleType('gi'); gir = types.ModuleType('gi.repository'); gir.GLib = None
sys.modules['gi'] = gi; sys.modules['gi.repository'] = gir
paho = types.ModuleType('paho'); pm = types.ModuleType('paho.mqtt'); pmc = types.ModuleType('paho.mqtt.client')
sys.modules['paho'] = paho; sys.modules['paho.mqtt'] = pm; sys.modules['paho.mqtt.client'] = pmc


class Svc(dict):
	def __enter__(self):
		return self

	def __exit__(self, *a):
		return False

	def add_mandatory_paths(self, *a, **k):
		pass

	def add_path(self, p, v=None, **k):
		self[p] = v

	def register(self):
		pass


vd = types.ModuleType('vedbus'); vd.VeDbusService = lambda *a, **k: Svc()
sys.modules['vedbus'] = vd

spec = importlib.util.spec_from_file_location('eb', sys.argv[1])
eb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(eb)

MUST = {}


class Bus:
	def call_blocking(self, svc, path, iface, method, sig, args, timeout=0):
		if path not in MUST:
			raise DBusException('missing')
		return MUST[path]


fails = 0


def check(cond, msg):
	global fails
	print(('ok   ' if cond else 'FAIL ') + msg)
	fails += not cond


b = eb.EboxBattery(None)
b.bus = Bus()
pwr = {'soc': 80, 'current_a': 50, 'power_w': 2700, 'voltage_v': 54.0, 'vhigh_mv': 3400, 'vlow_mv': 3350}

# MUST missing: as 1.4
b.update(dict(pwr))
s = b.service
check(s['/Info/MaxChargeVoltage'] == 56.6 and s['/Info/MaxChargeCurrent'] == 586 and s['/Info/MustIncluded'] == 0,
	'no MUST: Pytes values %s %s' % (s['/Info/MaxChargeVoltage'], s['/Info/MaxChargeCurrent']))

MUST.update({'/Connected': 1, '/Info/MaxChargeVoltage': 57.6, '/Info/MaxChargeCurrent': 15.0,
	'/Info/MaxDischargeCurrent': 200.0})
b.publish_limits()
check(s['/Info/MaxChargeCurrent'] == 601 and s['/Info/MaxDischargeCurrent'] == 786 and s['/Info/Pack/MaxChargeCurrent'] == 586,
	'MUST there: CCL/DCL summed %s %s, Pack %s' % (s['/Info/MaxChargeCurrent'], s['/Info/MaxDischargeCurrent'],
	s['/Info/Pack/MaxChargeCurrent']))
check(s['/Info/MaxChargeVoltage'] == 56.6 and s['/Info/CvlRaised'] == 0, 'Pytes at 80 %: CVL 56.6')

b.update(dict(pwr, vhigh_mv=3655))                   # Pytes cell stop: MUST still charges
check(s['/Info/Pack/MaxChargeCurrent'] == 0 and s['/Info/MaxChargeCurrent'] == 15 and s['/Io/AllowToCharge'] == 1,
	'Pytes cell 3655 mV: Pack CCL 0, system CCL 15 (MUST)')

b.update(dict(pwr, soc=100, voltage_v=55.2))       # Pytes full: CVL follows the MUST
check(s['/Info/MaxChargeVoltage'] == 57.6 and s['/Info/CvlRaised'] == 1 and s['/Info/Pack/MaxChargeCurrent'] == 586,
	'Pytes 100 %%, 55.2 V: CVL %s, Pack CCL %s' % (s['/Info/MaxChargeVoltage'], s['/Info/Pack/MaxChargeCurrent']))
b.update(dict(pwr, soc=100, voltage_v=56.45))
check(s['/Info/Pack/MaxChargeCurrent'] == 10, 'raised, Pytes 56.45 V: Pack CCL taper 10')
b.update(dict(pwr, soc=100, voltage_v=56.6))
check(s['/Info/Pack/MaxChargeCurrent'] == 0, 'raised, Pytes 56.6 V: Pack CCL 0')
b.update(dict(pwr, soc=98, voltage_v=55.0))
check(s['/Info/CvlRaised'] == 1, 'Pytes 98 %: stays raised (hysteresis)')
b.update(dict(pwr, soc=96.5, voltage_v=55.0))
check(s['/Info/MaxChargeVoltage'] == 56.6 and s['/Info/CvlRaised'] == 0, 'Pytes 96.5 %: back to 56.6')

b.update(dict(pwr, soc=100))
MUST['/Info/MaxChargeCurrent'] = 0.0                # MUST full (CCL 0): no reason to raise
b.publish_limits()
check(s['/Info/MaxChargeVoltage'] == 56.6, 'MUST CCL 0: CVL 56.6')
MUST['/Info/MaxChargeCurrent'] = Arr()              # invalid value
b.publish_limits()
check(s['/Info/MustIncluded'] == 0 and s['/Info/MaxChargeCurrent'] == 586, 'MUST invalid: Pytes alone')

b.update(dict(pwr, soc=3))                           # Pytes low: DCL 0, MUST still discharges
MUST['/Info/MaxChargeCurrent'] = 15.0
b.publish_limits()
check(s['/Info/Pack/MaxDischargeCurrent'] == 0 and s['/Info/MaxDischargeCurrent'] == 200 and s['/Io/AllowToDischarge'] == 1,
	'Pytes 3 %: Pack DCL 0, system DCL 200 (MUST)')
print('%d failed' % fails)
sys.exit(1 if fails else 0)
