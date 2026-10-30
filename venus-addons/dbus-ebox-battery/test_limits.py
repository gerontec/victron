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

b.update(dict(pwr, soc=100, voltage_v=55.2))       # 1.6: Pytes full, MUST asks 57.6 V: CVL stays 56.6 (VE.Bus-wide)
check(s['/Info/MaxChargeVoltage'] == 56.6 and s['/Info/CvlRaised'] == 0 and s['/Info/Pack/MaxChargeCurrent'] == 586,
	'Pytes 100 %%, MUST 57.6 V: CVL %s, Pack CCL %s' % (s['/Info/MaxChargeVoltage'], s['/Info/Pack/MaxChargeCurrent']))
b.update(dict(pwr, soc=100, voltage_v=56.45))
check(s['/Info/MaxChargeVoltage'] == 56.6 and s['/Info/Pack/MaxChargeCurrent'] == 586, 'Pytes 56.45 V: no raise, no pack taper')

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

# 1.6 pack alarms
t = [1000.0]
b.clock = lambda: t[0]
b.alarm_seen = {}                                     # sightings of the real clock above
b.update(dict(pwr))
ok = {'base': 'Dischg', 'volt': 'Normal', 'curr': 'Normal', 'temp': 'Normal', 'bvolt': 'Normal', 'btemp': 'Normal'}
three = [dict(ok, n=n) for n in (1, 2, 3)]
b.update(dict(pwr, pack_st=three))
check(all(s[p] == 0 for p in eb.ALARM_PATHS) and s['/System/NrOfModulesOffline'] == 0 and s['/Module/3/Online'] == 1,
	'3 packs Normal: no alarm, 0 offline')
now27 = [dict(ok, n=1, base='SysError'), dict(ok, n=2)]          # 2026-10-27 15:34
b.update(dict(pwr, pack_st=now27))
check(s['/Alarms/InternalFailure'] == 2 and s['/Alarms/BmsCable'] == 2 and s['/System/NrOfModulesOffline'] == 1
	and s['/Module/1/BaseSt'] == 'SysError' and s['/Module/3/Online'] == 0 and s['/Module/3/BaseSt'] == 'Absent',
	'pack 1 SysError + pack 3 absent: InternalFailure 2, BmsCable 2')
check(s['/Info/Pack/MaxChargeCurrent'] == 586 and s['/Info/Pack/MaxDischargeCurrent'] == 586, 'alarms do not change limits')
t[0] += 60
b.update(dict(pwr, pack_st=three))
check(s['/Alarms/InternalFailure'] == 2 and s['/Alarms/BmsCable'] == 2, 'back to normal after 60 s: still held')
t[0] += eb.ALARM_HOLD_SECONDS
b.update(dict(pwr, pack_st=three))
check(s['/Alarms/InternalFailure'] == 0 and s['/Alarms/BmsCable'] == 0, 'clear after hold')
t[0] += 1000
b.update(dict(pwr, pack_st=[dict(ok, n=1, curr='COCAlm', volt='OV'), dict(ok, n=2, curr='DOCAlm'), dict(ok, n=3, curr='COCPro')]))
check(s['/Alarms/HighChargeCurrent'] == 2 and s['/Alarms/HighDischargeCurrent'] == 0 and s['/Alarms/HighVoltage'] == 2,
	'COCAlm+COCPro -> HighChargeCurrent 2, DOCAlm not passed on (1.7), OV -> HighVoltage 2')
check(b.coc_limit == 45.0, 'COC at 50 A: Pytes CCL 45 A')
t[0] += 1000
b.update(dict(pwr, current_a=96.0, pack_st=[dict(ok, n=1, curr='COCAlm'), dict(ok, n=3)]))
check(b.coc_limit == 45.0, 'COC at 96 A: limit not raised')
b.coc_limit = None
b.update(dict(pwr, current_a=96.0, pack_st=[dict(ok, n=1, curr='COCAlm'), dict(ok, n=3)]))
check(b.coc_limit == 86.4 and s['/Info/Pack/MaxChargeCurrent'] == 86.4, 'COC at 96 A: Pytes CCL 86.4 A')
b.update(dict(pwr, current_a=86.0, pack_st=[dict(ok, n=1, curr='COCAlm'), dict(ok, n=3)]))
check(b.coc_limit == 77.4, 'COC again at 86 A: 77.4 A')
t[0] += eb.COC_HOLD_SECONDS - 1
b.update(dict(pwr, current_a=77.0, pack_st=three))
check(b.coc_limit == 77.4, 'within hold: unchanged')
t[0] += 1
b.update(dict(pwr, current_a=77.0, pack_st=three))
check(b.coc_limit == 78.4 and s['/Info/Pack/MaxChargeCurrent'] == 78.4, 'after hold: +1 A per update')
b.coc_limit = eb.MAX_CHARGE_CURRENT - 0.5
b.update(dict(pwr, pack_st=three))
check(b.coc_limit is None and s['/Info/Pack/MaxChargeCurrent'] == 586, 'ramp reaches max: derating ended')
t[0] += 1000
b.update(dict(pwr, pack_st=[dict(ok, n=1, temp='Weird'), dict(ok, n=2), dict(ok, n=3)]))
check(s['/Alarms/InternalFailure'] == 1, 'unknown state -> InternalFailure 1')
t[0] += 1000
b.update(dict(pwr, pack_st=None))
check(s['/Alarms/BmsCable'] == 1 and s['/System/NrOfModulesOffline'] is None and s['/Module/1/Online'] is None,
	'console not readable: BmsCable 1, modules unknown')
t[0] += 1000
b.update(dict(pwr))
check(all(s[p] == 0 for p in eb.ALARM_PATHS), 'payload without pack_st (old ebox_mqtt): no pack alarm')
t[0] += 1000
b.update(dict(pwr, volt_st='OV', vhigh_mv=3500))
check(b.high_block and s['/Info/Pack/MaxChargeCurrent'] == 0 and s['/Alarms/HighVoltage'] == 2, 'volt_st OV: Pytes charging blocked')
b.update(dict(pwr, vhigh_mv=3500))
check(b.high_block, 'OV gone, cell 3500 mV: still blocked')
b.update(dict(pwr, vhigh_mv=3400))
check(not b.high_block and s['/Info/Pack/MaxChargeCurrent'] == 586, 'cell 3400 mV: released')
print('%d failed' % fails)
sys.exit(1 if fails else 0)
