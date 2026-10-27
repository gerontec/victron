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
- pack "High" voltage event from the EBox console (volt_st, Volt.St column of `ebox pwr`): charging blocked
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
- CVL = the MUST request (57.6 V) while the Pytes is full (SoC >= CVL_RAISE_SOC, back below CVL_LOWER_SOC) and
  the MUST takes charge; else MAX_CHARGE_VOLTAGE (56.6 V) as before, so the Pytes keeps the fast voltage limit of
  the chargers whenever it can charge. While raised, a pack voltage guard on the Pytes CCL (TAPER_CURRENT from
  PACK_TAPER_V, 0 from MAX_CHARGE_VOLTAGE) keeps batmonitor from charging the Pytes above its own CVL.
- MUST missing on the D-Bus: the Pytes values alone, as up to 1.4.
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

VERSION = '1.5'
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
FORCE_CHARGE_SOC = 5.0          # %
FORCE_CHARGE_RELEASE_SOC = 8.0  # %
STALE_SECONDS = 120
CAPACITY_AH = 300               # 15 kWh at 51.2 V nominal (was 600 = both stacks, corrected 2026-10-08)
MUST_SERVICE = 'com.victronenergy.battery.socketcan_can0'   # Stack1 MUST on L1 (1.5)
CVL_RAISE_SOC = 99.0            # Pytes SoC from which the system CVL may follow the MUST
CVL_LOWER_SOC = 97.0            # ... back to MAX_CHARGE_VOLTAGE below
PACK_TAPER_V = 56.4             # V, raised CVL: Pytes CCL down to TAPER_CURRENT, 0 from MAX_CHARGE_VOLTAGE
REFRESH_SECONDS = 5             # system limits follow the MUST BMS this often (the EBox data comes every 30 s)

log = logging.getLogger('dbus-ebox-battery')


class EboxBattery:
	def __init__(self, mainloop):
		self.mainloop = mainloop
		self.service = None
		self.last_update = 0.0
		self.low_soc = False
		self.high_block = False
		self.bus = dbus.SystemBus()
		self.pack = None                # last Pytes values from update(): soc, voltage, vhigh (1.5)
		self.cvl_raised = False

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
		s.add_path('/Info/MaxChargeVoltage', MAX_CHARGE_VOLTAGE)
		s.add_path('/Info/MaxChargeCurrent', None)
		s.add_path('/Info/MaxDischargeCurrent', None)
		s.add_path('/Info/Pack/MaxChargeVoltage', MAX_CHARGE_VOLTAGE)   # 1.5: the Pytes alone (batmonitor, logging)
		s.add_path('/Info/Pack/MaxChargeCurrent', None)
		s.add_path('/Info/Pack/MaxDischargeCurrent', None)
		s.add_path('/Info/MustIncluded', 0)                            # 1.5: /Info/* include the MUST on L1
		s.add_path('/Info/CvlRaised', 0)                               # 1.5: CVL follows the MUST, Pytes full
		s.add_path('/Info/ChargeRequest', 0)
		s.add_path('/Io/AllowToCharge', 1)
		s.add_path('/Io/AllowToDischarge', 1)
		s.add_path('/Alarms/HighVoltage', 0)
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
		if volt_st.lower().startswith('high'):
			if not self.high_block:
				log.warning('EBox reports %s voltage (cell max %s mV): charging blocked', volt_st, vhigh)
			self.high_block = True
		elif self.high_block and vhigh is not None and vhigh < HIGH_RELEASE_MV:
			log.info('EBox voltage %s, cell max %s mV: charging released', volt_st, vhigh)
			self.high_block = False

		if soc < FORCE_CHARGE_SOC:
			self.low_soc = True
		elif soc >= FORCE_CHARGE_RELEASE_SOC:
			self.low_soc = False
		force_charge = self.low_soc

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
			ctx['/Alarms/HighVoltage'] = 2 if self.high_block else (1 if (vhigh or 0) >= CELL_STOP_MV else 0)
			ctx['/Alarms/LowSoc'] = 1 if self.low_soc else 0
		self.pack = {'soc': soc, 'voltage': voltage, 'vhigh': vhigh}
		self.last_update = time.monotonic()
		self.publish_limits()

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
		pack_dcl = 0.0 if self.low_soc else MAX_DISCHARGE_CURRENT
		must = self.must_limits()
		raise_cvl = (must is not None and must[0] > MAX_CHARGE_VOLTAGE and must[1] > 0 and
					 p['soc'] >= (CVL_LOWER_SOC if self.cvl_raised else CVL_RAISE_SOC))
		if raise_cvl != self.cvl_raised:
			log.info('system CVL %.2f V (%s), Pytes SoC %.1f %%', must[0] if raise_cvl else MAX_CHARGE_VOLTAGE,
				'MUST request, Pytes full' if raise_cvl else 'Pytes', p['soc'])
			self.cvl_raised = raise_cvl
		if raise_cvl and p['voltage']:                      # the chargers no longer hold the Pytes at its CVL
			if p['voltage'] >= MAX_CHARGE_VOLTAGE:
				pack_ccl = 0.0
			elif p['voltage'] >= PACK_TAPER_V:
				pack_ccl = min(pack_ccl, TAPER_CURRENT)
		cvl = must[0] if raise_cvl else MAX_CHARGE_VOLTAGE
		ccl = pack_ccl + (must[1] if must else 0.0)
		dcl = pack_dcl + (must[2] if must else 0.0)
		with self.service as ctx:
			ctx['/Info/Pack/MaxChargeCurrent'] = pack_ccl
			ctx['/Info/Pack/MaxDischargeCurrent'] = pack_dcl
			ctx['/Info/MaxChargeVoltage'] = cvl
			ctx['/Info/MaxChargeCurrent'] = ccl
			ctx['/Info/MaxDischargeCurrent'] = dcl
			ctx['/Info/MustIncluded'] = 1 if must else 0
			ctx['/Info/CvlRaised'] = 1 if raise_cvl else 0
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
