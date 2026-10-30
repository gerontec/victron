#!/usr/bin/env python3
"""
dbus-ebox-battery: publish the EBox battery (16S LFP, Pylontech-style console, no CAN) on the Venus D-Bus
as com.victronenergy.battery.ebox, so DVCC/ESS control the MultiPlus units from its SoC and limits.

Input: MQTT topic ebox/pwr on the local Venus broker, written every 30 s by ebox_mqtt.py on the host:
  {"soc": 84.3, "power_w": 5400, "current_a": 102.4, "voltage_v": 53.1, "vhigh_mv": 3420,
   "vlow_mv": 3301, "volt_st": "Normal", "packs": 3, "ts": "..."}

Behaviour:
- charge end as before: CVL = MAX_CHARGE_VOLTAGE (highest pack voltage seen at 100 % SoC, Jul-Oct 2026)
- cell protection: charge current tapers when the highest cell reaches CELL_TAPER_MV, 0 at CELL_STOP_MV
- pack "High" or "OV" (1.6) voltage event from the EBox console (volt_st, Volt.St column of `ebox pwr`): charging blocked
  (CCL 0, AllowToCharge 0, HighVoltage alarm) until volt_st is back and the highest cell is below
  HIGH_RELEASE_MV, so the charger does not pulse against the pack limit
- low SoC below FORCE_CHARGE_SOC: discharge blocked until FORCE_CHARGE_RELEASE_SOC; forced charge from the grid
  (/Info/ChargeRequest = 1, hub4control goes to Recharge) at any time (the 11-13 h window was removed in 1.4,
  user 2026-10-11); PV surplus charges at any time
- no data for STALE_SECONDS: exit; daemontools restarts it and the service only registers again with
  fresh data, so the MultiPlus fall back to their own charge settings meanwhile

System limits (1.5, user 2026-10-27: "er muss in Zukunft wissen, dass nur an L2 und L3 eine Pytes haengt.
L1 = MUST only"): this service is the DVCC BMS (BmsInstance 513) of the whole three-phase VE.Bus system, but the
Pytes hangs only on L2 + L3, the MUST (can0, its own CAN-bus BMS) alone on L1. DVCC knows one limit set for all
three units, so /Info/* now carries the limits of both banks:
- CCL / DCL = Pytes + MUST: a tapering or blocked Pytes no longer stops the MUST on L1 (and the other way round).
  The per-bank limits are enforced per phase by batmonitor (charge / discharge caps from each BMS); for the Pytes
  it reads /Info/Pack/*, which keep the Pytes-only values.
- CVL = MAX_CHARGE_VOLTAGE (56.6 V) always. 1.5 raised it to the MUST request (57.6 V) while the Pytes was full;
  removed in 1.6 (user 2026-10-27: no raise above 57 V unless it can be limited to the L1 unit): the VE.Bus has one
  charge voltage for all three units (vebus /BatteryOperationalLimits/MaxChargeVoltage, no per-device or per-phase
  setpoint), so a raised CVL also reached the Pytes on L2 + L3. /Info/CvlRaised stays, always 0.
- MUST missing on the D-Bus: the Pytes values alone, as up to 1.4.

Pack alarms (1.6, 2026-10-27: pack 1 SysError + pack 3 Absent with the red ALM LED, Venus showed nothing): ebox_mqtt.py
sends all status columns of `ebox pwr` per present pack ("pack_st"); each one goes to /Module/<n>/* and to a Venus
alarm (0 ok, 1 warning, 2 alarm), worst pack wins. Alarms only, the charge / discharge limits do not change.
- Base.St SysError -> InternalFailure 2; Charge / Dischg / Idle ok
- pack of 1..PACKS_EXPECTED missing (Absent) -> BmsCable 2 (link between the packs), /System/NrOfModulesOffline;
  "pack_st": null (console not readable, CAN fallback) -> BmsCable 1
- Volt.St, B.V.St: High -> HighVoltage 1, OV -> HighVoltage 2
- Curr.St: COCAlm -> HighChargeCurrent 1, other COC* -> 2; and the Pytes charge current is lowered (1.8, see below)
- Curr.St DOC* is no longer passed on (1.7, user 2026-10-30: pack 3 DOCAlm at 06:46 while the Pytes allows 300 A = 1C):
  HighDischargeCurrent comes from the summed stack current "current_a" instead, 1 at >= DISCHARGE_ALARM_CURRENT
  (300 A, about 14.4 kW at 48 V); below that all is ok. /Module/<n>/CurrSt still shows the console state.
- Temp.St, B.T.St: only Normal seen so far

Charge block also on B.V.St (1.9, user 2026-10-30 "OV beachten": 2026-10-29 13:07 + 13:32 pack 1 bvolt OV raised only the
alarm, charging went on): High / OV in the Volt.St or B.V.St column of any pack in pack_st blocks charging like volt_st.

Low voltage force charge (1.10, user 2026-10-30: "Low/UV muss Zwangsladung ausloesen, notfalls auch ohne pv excess"):
Low / UV in volt_st or in the Volt.St / B.V.St column of any pack latches low_volt: Pytes DCL 0, AllowToDischarge 0
(when the MUST gives no DCL either) and /Info/ChargeRequest 1. batmonitor sees /Info/Pack/MaxDischargeCurrent below
DCL_FORCE_A and goes to LOW / FORCE: FORCE_CHARGE_W per phase from the grid, with or without PV surplus, no discharge.
Released once no pack reports Low / UV and the lowest cell is at least LOW_RELEASE_MV (= batmonitor CELL_FORCE_RELEASE_V, 3.20 V in the Pi env).

Charge current derating on COC (1.8, user 2026-10-30: "high charge muss beachtet werden, strom leicht absenken"; COCAlm
packs 1 + 3 at 09:57-11:05 with 44-50 A per pack, only 2 packs present): a COC* state of any pack sets the Pytes CCL
to COC_DERATE x the stack charge current at that moment (at least TAPER_CURRENT); a further COC lowers it again from
the then current. COC_HOLD_SECONDS after the last COC sighting the limit rises by COC_RAMP_A per EBox update (30 s)
and is dropped once it reaches MAX_CHARGE_CURRENT. Acts on /Info/Pack/MaxChargeCurrent and the summed /Info/*.
- seen so far (pv_ebox2 since 2026-07-29): Base.St Charge Dischg Idle SysError Absent, Volt.St Normal High OV,
  Curr.St Normal COCAlm DOCAlm, Temp.St Normal. Not seen, assumed by symmetry: Low / UV -> LowVoltage 1 / 2,
  High / OT -> HighTemperature 1 / 2, Low / UT -> LowTemperature 1 / 2
- any other state -> InternalFailure 1 and a log line, so a new console state never goes unnoticed
- an alarm stays up ALARM_HOLD_SECONDS after its last sighting: a flapping pack (15:27 Idle, 15:28 SysError)
  raises one notification, not one per 30 s
"""

import json
import logging
import os
import sys
import time

import dbus
import paho.mqtt.client as mqtt
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

sys.path.insert(1, '/opt/victronenergy/dbus-systemcalc-py/ext/velib_python')
from vedbus import VeDbusService  # noqa: E402

VERSION = '1.10'
# can-bus-bms (Speicher A on can0) registers as battery instance 512; 513 keeps the MQTT topics
# N/<portal>/battery/<instance>/... of the two batteries apart
DEVICE_INSTANCE = 513
MQTT_HOST = os.environ.get('EBOX_MQTT_HOST', '127.0.0.1')
MQTT_TOPIC = 'ebox/pwr'

# Stack2 = Pytes EBox, 15 kWh, 3 modules of 100 Ah at 51.2 V (all visible on the console); Stack1 (MUST) is separate
MAX_CHARGE_VOLTAGE = 56.6       # V, highest pack voltage seen at 100 % SoC (Jul-Oct 2026)
MAX_CHARGE_CURRENT = 586.0      # A, 30 kW / 51.2 V: 1C, allowed by Pytes
MAX_DISCHARGE_CURRENT = 586.0   # A, 1C as for charging
CELL_TAPER_MV = 3600            # highest cell: charge current down to TAPER_CURRENT
CELL_STOP_MV = 3650             # highest cell: no charge current
TAPER_CURRENT = 10.0            # A
HIGH_RELEASE_MV = 3450          # highest cell: release the charge block after a "High" event
LOW_RELEASE_MV = 3200           # lowest cell: release the Low / UV force charge (1.10), = CELL_FORCE_RELEASE_V in the Pi env
FORCE_CHARGE_SOC = 5.0          # %
FORCE_CHARGE_RELEASE_SOC = 8.0  # %
STALE_SECONDS = 120
CAPACITY_AH = 300               # 15 kWh at 51.2 V nominal (was 600 = both stacks, corrected 2026-10-08)
MUST_SERVICE = 'com.victronenergy.battery.socketcan_can0'   # Stack1 MUST on L1 (1.5)
REFRESH_SECONDS = 5             # system limits follow the MUST BMS this often (the EBox data comes every 30 s)
PACKS_EXPECTED = 3              # modules 1..3 of the Pytes stack (1.6)
ALARM_HOLD_SECONDS = 300        # alarm stays up this long after its last sighting (1.6)
DISCHARGE_ALARM_CURRENT = 300.0 # A, summed discharge current of the stack: HighDischargeCurrent 1 from here (1.7)
COC_DERATE = 0.9                # on COC*: Pytes CCL = this x stack charge current (1.8)
COC_HOLD_SECONDS = 1800         # derated CCL held this long after the last COC sighting (1.8)
COC_RAMP_A = 1.0                # A per EBox update (30 s) back up after the hold (1.8)

# console state -> (alarm path, level) per status column of `ebox pwr` (1.6); 'Normal' and BASE_OK are 0
BASE_OK = ('Charge', 'Dischg', 'Idle')
STATE_ALARMS = {
	'base':  {'SysError': ('/Alarms/InternalFailure', 2)},
	'volt':  {'High': ('/Alarms/HighVoltage', 1), 'OV': ('/Alarms/HighVoltage', 2),
	          'Low': ('/Alarms/LowVoltage', 1), 'UV': ('/Alarms/LowVoltage', 2)},
	'curr':  {'COCAlm': ('/Alarms/HighChargeCurrent', 1)},
	'temp':  {'High': ('/Alarms/HighTemperature', 1), 'OT': ('/Alarms/HighTemperature', 2),
	          'Low': ('/Alarms/LowTemperature', 1), 'UT': ('/Alarms/LowTemperature', 2)},
}
STATE_ALARMS['bvolt'] = STATE_ALARMS['volt']
STATE_ALARMS['btemp'] = STATE_ALARMS['temp']
ALARM_PATHS = ('/Alarms/HighVoltage', '/Alarms/LowVoltage', '/Alarms/HighTemperature', '/Alarms/LowTemperature',
	'/Alarms/HighChargeCurrent', '/Alarms/HighDischargeCurrent', '/Alarms/InternalFailure', '/Alarms/BmsCable')
MODULE_FIELDS = (('base', 'BaseSt'), ('volt', 'VoltSt'), ('curr', 'CurrSt'), ('temp', 'TempSt'),
	('bvolt', 'BVoltSt'), ('btemp', 'BTempSt'))


def state_alarm(field, state):
	"""(path, level) of one console state, (None, 0) if ok; unknown states -> InternalFailure 1"""
	if state in (None, 'Normal') or (field == 'base' and state in BASE_OK):
		return None, 0
	hit = STATE_ALARMS[field].get(state)
	if hit:
		return hit
	if field == 'curr' and state[:3] == 'DOC':        # 1.7: from the summed current, see update_alarms
		return None, 0
	if field == 'curr' and state[:3] == 'COC':
		return '/Alarms/HighChargeCurrent', 2
	return '/Alarms/InternalFailure', 1


def pack_alarms(pack_st):
	"""raw alarm levels {path: level} and the causes {path: ['pack 1 base SysError', ...]} from pack_st"""
	raw, why = {}, {}

	def put(path, level, cause):
		if level > raw.get(path, 0):
			raw[path] = level
		why.setdefault(path, []).append(cause)

	if pack_st is None:
		put('/Alarms/BmsCable', 1, 'EBox console not readable')
		return raw, why
	present = {p.get('n') for p in pack_st}
	for n in range(1, PACKS_EXPECTED + 1):
		if n not in present:
			put('/Alarms/BmsCable', 2, 'pack %d absent' % n)
	for p in pack_st:
		for field in STATE_ALARMS:
			path, level = state_alarm(field, p.get(field))
			if path:
				put(path, level, 'pack %s %s %s' % (p.get('n'), field, p.get(field)))
	return raw, why

log = logging.getLogger('dbus-ebox-battery')


class EboxBattery:
	def __init__(self, mainloop):
		self.mainloop = mainloop
		self.service = None
		self.last_update = 0.0
		self.low_soc = False
		self.high_block = False
		self.low_volt = False           # Low / UV latched: DCL 0 + charge request (1.10)
		self.bus = dbus.SystemBus()
		self.pack = None                # last Pytes values from update(): soc, voltage, vhigh (1.5)
		self.clock = time.monotonic
		self.alarm_seen = {}            # path -> {level: last sighting} (1.6)
		self.alarm_shown = {}           # path -> level on the D-Bus (1.6)
		self.coc_limit = None           # derated Pytes CCL after a COC* state, None = not derated (1.8)
		self.coc_seen = 0.0             # last COC sighting (1.8)

	def register(self):
		s = VeDbusService('com.victronenergy.battery.ebox', self.bus, register=False)
		# productid 0xB004 = LG RESU: systemcalc's LgCircuitBreakerDetect then switched the VE.Bus to Mode 4 (off)
		# whenever the EBox current was 0 and the Multi DC voltage differed > 10 % (2026-10-11 04:05:57). 0xBA77 = generic
		s.add_mandatory_paths(__file__, VERSION, 'MQTT ' + MQTT_TOPIC, deviceinstance=DEVICE_INSTANCE,
			productid=0xBA77, productname='EBox LFP (via ebox_mqtt)', firmwareversion=VERSION,
			hardwareversion=None, connected=1)
		s.add_path('/CustomName', 'EBox', writeable=True)
		s.add_path('/Soc', None)
		s.add_path('/Capacity', CAPACITY_AH)
		s.add_path('/InstalledCapacity', CAPACITY_AH)
		s.add_path('/Dc/0/Voltage', None)
		s.add_path('/Dc/0/Current', None)
		s.add_path('/Dc/0/Power', None)
		s.add_path('/System/MaxCellVoltage', None)
		s.add_path('/System/MinCellVoltage', None)
		s.add_path('/System/NrOfModulesOnline', None)
		s.add_path('/System/NrOfModulesOffline', None)                  # 1.6
		for n in range(1, PACKS_EXPECTED + 1):                          # 1.6: console states per pack
			s.add_path('/Module/%d/Online' % n, None)
			for _, name in MODULE_FIELDS:
				s.add_path('/Module/%d/%s' % (n, name), None)
		s.add_path('/Info/MaxChargeVoltage', MAX_CHARGE_VOLTAGE)
		s.add_path('/Info/MaxChargeCurrent', None)
		s.add_path('/Info/MaxDischargeCurrent', None)
		s.add_path('/Info/Pack/MaxChargeVoltage', MAX_CHARGE_VOLTAGE)   # 1.5: the Pytes alone (batmonitor, logging)
		s.add_path('/Info/Pack/MaxChargeCurrent', None)
		s.add_path('/Info/Pack/MaxDischargeCurrent', None)
		s.add_path('/Info/MustIncluded', 0)                            # 1.5: /Info/* include the MUST on L1
		s.add_path('/Info/CvlRaised', 0)                               # 1.5, always 0 since 1.6 (no CVL raise)
		s.add_path('/Info/ChargeRequest', 0)
		s.add_path('/Io/AllowToCharge', 1)
		s.add_path('/Io/AllowToDischarge', 1)
		for path in ALARM_PATHS:
			s.add_path(path, 0)
		s.add_path('/Alarms/LowSoc', 0)
		s.register()
		self.service = s
		log.info('registered com.victronenergy.battery.ebox')

	def charge_current(self, vhigh_mv):
		if vhigh_mv is None:
			return MAX_CHARGE_CURRENT
		if vhigh_mv >= CELL_STOP_MV:
			return 0.0
		if vhigh_mv >= CELL_TAPER_MV:
			return TAPER_CURRENT
		return MAX_CHARGE_CURRENT

	def update(self, d):
		soc = float(d['soc'])
		current = float(d['current_a'])
		power = float(d['power_w'])
		voltage = d.get('voltage_v')
		if voltage is None and current:
			voltage = power / current
		vhigh = d.get('vhigh_mv')
		vlow = d.get('vlow_mv')

		volt_st = str(d.get('volt_st') or 'Normal')
		# 1.9: also Volt.St / B.V.St of each pack (ebox_mqtt's volt_st carries only the Volt.St column)
		bad = ['pack %s %s %s' % (pk.get('n'), f, pk.get(f)) for pk in d.get('pack_st') or [] for f in ('volt', 'bvolt')
			if str(pk.get(f) or '').lower().startswith('high') or pk.get(f) == 'OV']
		if volt_st.lower().startswith('high') or volt_st == 'OV' or bad:   # 1.6: OV (protection) is worse than High
			if bad and not (volt_st.lower().startswith('high') or volt_st == 'OV'):
				volt_st = ', '.join(bad)
			if not self.high_block:
				log.warning('EBox reports %s voltage (cell max %s mV): charging blocked', volt_st, vhigh)
			self.high_block = True
		elif self.high_block and vhigh is not None and vhigh < HIGH_RELEASE_MV:
			log.info('EBox voltage %s, cell max %s mV: charging released', volt_st, vhigh)
			self.high_block = False

		# 1.10: Low / UV -> force charge
		low = ['pack %s %s %s' % (pk.get('n'), f, pk.get(f)) for pk in d.get('pack_st') or [] for f in ('volt', 'bvolt')
			if str(pk.get(f) or '').lower().startswith('low') or pk.get(f) == 'UV']
		if volt_st.lower().startswith('low') or volt_st == 'UV':
			low.append('volt_st ' + volt_st)
		if low:
			if not self.low_volt:
				log.warning('EBox reports %s (cell min %s mV): discharge blocked, force charge', ', '.join(low), vlow)
			self.low_volt = True
		elif self.low_volt and vlow is not None and vlow >= LOW_RELEASE_MV:
			log.info('EBox voltage no longer low, cell min %s mV: force charge released', vlow)
			self.low_volt = False

		if soc < FORCE_CHARGE_SOC:
			self.low_soc = True
		elif soc >= FORCE_CHARGE_RELEASE_SOC:
			self.low_soc = False
		force_charge = self.low_soc or self.low_volt

		if self.service is None:
			self.register()
		s = self.service
		with s as ctx:
			ctx['/Soc'] = round(soc, 1)
			ctx['/Dc/0/Voltage'] = round(voltage, 2) if voltage else None
			ctx['/Dc/0/Current'] = round(current, 1)
			ctx['/Dc/0/Power'] = round(power)
			ctx['/System/MaxCellVoltage'] = vhigh / 1000.0 if vhigh else None
			ctx['/System/MinCellVoltage'] = vlow / 1000.0 if vlow else None
			ctx['/System/NrOfModulesOnline'] = d.get('packs')
			ctx['/Info/ChargeRequest'] = 1 if force_charge else 0
			ctx['/Alarms/LowSoc'] = 1 if self.low_soc else 0
		self.update_alarms(d, 2 if self.high_block else (1 if (vhigh or 0) >= CELL_STOP_MV else 0))
		self.update_coc(d.get('pack_st'), current)
		self.pack = {'soc': soc, 'voltage': voltage, 'vhigh': vhigh}
		self.last_update = time.monotonic()
		self.publish_limits()

	def update_alarms(self, d, high_voltage):
		"""/Module/<n>/*, /System/NrOfModulesOffline and the alarms from pack_st (1.6); high_voltage = the cell /
		block alarm of up to 1.5. A payload without "pack_st" (ebox_mqtt.py before 1.6) leaves the pack alarms 0."""
		if 'pack_st' in d:
			raw, why = pack_alarms(d['pack_st'])
		else:
			raw, why = {}, {}
		raw['/Alarms/HighVoltage'] = max(raw.get('/Alarms/HighVoltage', 0), high_voltage)
		discharge = -float(d['current_a'])          # 1.7: summed stack current, negative = discharge
		if discharge >= DISCHARGE_ALARM_CURRENT:
			raw['/Alarms/HighDischargeCurrent'] = 1
			why.setdefault('/Alarms/HighDischargeCurrent', []).append('stack discharge %.0f A' % discharge)
		now = self.clock()
		with self.service as ctx:
			pack_st = d.get('pack_st')
			by_n = {p.get('n'): p for p in pack_st} if pack_st is not None else None
			ctx['/System/NrOfModulesOffline'] = (PACKS_EXPECTED - len(set(by_n) & set(range(1, PACKS_EXPECTED + 1)))
				if by_n is not None else None)
			for n in range(1, PACKS_EXPECTED + 1):
				p = by_n.get(n) if by_n is not None else None
				ctx['/Module/%d/Online' % n] = None if by_n is None else (1 if p else 0)
				for field, name in MODULE_FIELDS:
					ctx['/Module/%d/%s' % (n, name)] = p.get(field) if p else (None if by_n is None else 'Absent')
			for path in ALARM_PATHS:
				level = raw.get(path, 0)
				seen = self.alarm_seen.setdefault(path, {})
				if level:
					seen[level] = now
				shown = max([lv for lv, t in seen.items() if now - t < ALARM_HOLD_SECONDS] + [level])
				if shown != self.alarm_shown.get(path, 0):
					if shown > self.alarm_shown.get(path, 0):
						log.warning('%s %d: %s', path, shown, ', '.join(why.get(path, [])) or 'held')
					else:
						log.info('%s %d (clear for %d s)', path, shown, ALARM_HOLD_SECONDS)
				self.alarm_shown[path] = shown
				ctx[path] = shown

	def update_coc(self, pack_st, current):
		"""COC derating (1.8): lower the Pytes CCL on a COC* state, ramp it back after COC_HOLD_SECONDS"""
		now = self.clock()
		coc = [p.get('n') for p in pack_st or [] if str(p.get('curr') or '')[:3] == 'COC']
		if coc:
			limit = max(TAPER_CURRENT, round(COC_DERATE * max(current, 0.0), 1))
			if self.coc_limit is None or limit < self.coc_limit:
				log.warning('COC on pack %s at %.1f A: Pytes charge current limited to %.1f A',
					','.join(map(str, coc)), current, limit)
				self.coc_limit = limit
			self.coc_seen = now
		elif self.coc_limit is not None and now - self.coc_seen >= COC_HOLD_SECONDS:
			self.coc_limit += COC_RAMP_A
			if self.coc_limit >= MAX_CHARGE_CURRENT:
				log.info('COC derating ended')
				self.coc_limit = None

	def must_limits(self):
		"""CVL, CCL, DCL of the MUST BMS on L1 from the D-Bus (its CAN-bus BMS values, same Venus), None if it is
		missing or sends no valid limits"""
		def get(path):
			return self.bus.call_blocking(MUST_SERVICE, path, 'com.victronenergy.BusItem', 'GetValue', '', [],
				timeout=2)
		vals = []
		try:
			if get('/Connected') != 1:
				return None
			for p in ('/Info/MaxChargeVoltage', '/Info/MaxChargeCurrent', '/Info/MaxDischargeCurrent'):
				v = get(p)
				if isinstance(v, dbus.Array):                   # [] = invalid
					return None
				vals.append(float(v))
		except (dbus.exceptions.DBusException, TypeError, ValueError):
			return None
		cvl, ccl, dcl = vals
		if not 40.0 < cvl < 60.0 or ccl < 0 or dcl < 0:
			return None
		return cvl, ccl, dcl

	def publish_limits(self):
		"""/Info/Pack/* = the Pytes alone, /Info/* = the DVCC limits of the whole VE.Bus system (both banks, 1.5)"""
		if self.service is None or self.pack is None:
			return True
		p = self.pack
		pack_ccl = 0.0 if self.high_block else self.charge_current(p['vhigh'])
		if self.coc_limit is not None:                      # 1.8
			pack_ccl = min(pack_ccl, self.coc_limit)
		pack_dcl = 0.0 if self.low_soc or self.low_volt else MAX_DISCHARGE_CURRENT
		must = self.must_limits()
		cvl = MAX_CHARGE_VOLTAGE                            # 1.6: never the MUST request, it would reach L2 + L3 too
		ccl = pack_ccl + (must[1] if must else 0.0)
		dcl = pack_dcl + (must[2] if must else 0.0)
		with self.service as ctx:
			ctx['/Info/Pack/MaxChargeCurrent'] = pack_ccl
			ctx['/Info/Pack/MaxDischargeCurrent'] = pack_dcl
			ctx['/Info/MaxChargeVoltage'] = cvl
			ctx['/Info/MaxChargeCurrent'] = ccl
			ctx['/Info/MaxDischargeCurrent'] = dcl
			ctx['/Info/MustIncluded'] = 1 if must else 0
			ctx['/Info/CvlRaised'] = 0
			ctx['/Io/AllowToCharge'] = 1 if ccl > 0 else 0
			ctx['/Io/AllowToDischarge'] = 1 if dcl > 0 else 0
		return True

	def check_stale(self):
		if self.service is not None and time.monotonic() - self.last_update > STALE_SECONDS:
			log.error('no data on %s for %d s, exiting', MQTT_TOPIC, STALE_SECONDS)
			# sys.exit() inside a GLib callback is swallowed by PyGObject: stop the loop instead
			self.mainloop.quit()
			return False
		return True


def main():
	logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
	DBusGMainLoop(set_as_default=True)
	mainloop = GLib.MainLoop()
	battery = EboxBattery(mainloop)

	def on_connect(client, userdata, flags, reason_code, properties):
		log.info('MQTT connected to %s (%s)', MQTT_HOST, reason_code)
		client.subscribe(MQTT_TOPIC)

	def on_message(client, userdata, msg):
		try:
			d = json.loads(msg.payload)
		except ValueError:
			log.warning('bad payload on %s', msg.topic)
			return
		# D-Bus is not thread safe: hand the update to the GLib main loop
		GLib.idle_add(lambda: battery.update(d) and False)

	client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='dbus-ebox-battery')
	client.on_connect = on_connect
	client.on_message = on_message
	client.connect_async(MQTT_HOST, 1883, 60)
	client.loop_start()

	GLib.timeout_add_seconds(10, battery.check_stale)
	GLib.timeout_add_seconds(REFRESH_SECONDS, battery.publish_limits)
	mainloop.run()
	client.loop_stop()
	sys.exit(1)


if __name__ == '__main__':
	main()
