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
- forced charge below FORCE_CHARGE_SOC: /Info/ChargeRequest = 1 (hub4control goes to Recharge and charges
  from the grid), discharge blocked, released at FORCE_CHARGE_RELEASE_SOC
- no data for STALE_SECONDS: exit; daemontools restarts it and the service only registers again with
  fresh data, so the MultiPlus fall back to their own charge settings meanwhile
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

VERSION = '1.0'
MQTT_HOST = os.environ.get('EBOX_MQTT_HOST', '127.0.0.1')
MQTT_TOPIC = 'ebox/pwr'

# limits for both EBoxes together: 2 x 15 kWh Pytes, 6 modules of 100 Ah at 51.2 V (3 modules visible)
MAX_CHARGE_VOLTAGE = 56.6       # V, highest pack voltage seen at 100 % SoC (Jul-Oct 2026)
MAX_CHARGE_CURRENT = 586.0      # A, 30 kW / 51.2 V: 1C, allowed by Pytes
MAX_DISCHARGE_CURRENT = 586.0   # A, 1C as for charging
CELL_TAPER_MV = 3600            # highest cell: charge current down to TAPER_CURRENT
CELL_STOP_MV = 3650             # highest cell: no charge current
TAPER_CURRENT = 10.0            # A
FORCE_CHARGE_SOC = 5.0          # %
FORCE_CHARGE_RELEASE_SOC = 8.0  # %
STALE_SECONDS = 120
CAPACITY_AH = 600               # 2 x 15 kWh at 51.2 V nominal

log = logging.getLogger('dbus-ebox-battery')


class EboxBattery:
	def __init__(self, mainloop):
		self.mainloop = mainloop
		self.service = None
		self.last_update = 0.0
		self.force_charge = False

	def register(self):
		s = VeDbusService('com.victronenergy.battery.ebox', dbus.SystemBus(), register=False)
		s.add_mandatory_paths(__file__, VERSION, 'MQTT ' + MQTT_TOPIC, deviceinstance=512,
			productid=0xB004, productname='EBox LFP (via ebox_mqtt)', firmwareversion=VERSION,
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

		if soc < FORCE_CHARGE_SOC:
			self.force_charge = True
		elif soc >= FORCE_CHARGE_RELEASE_SOC:
			self.force_charge = False

		if self.service is None:
			self.register()
		s = self.service
		ccl = self.charge_current(vhigh)
		dcl = 0.0 if self.force_charge else MAX_DISCHARGE_CURRENT
		with s as ctx:
			ctx['/Soc'] = round(soc, 1)
			ctx['/Dc/0/Voltage'] = round(voltage, 2) if voltage else None
			ctx['/Dc/0/Current'] = round(current, 1)
			ctx['/Dc/0/Power'] = round(power)
			ctx['/System/MaxCellVoltage'] = vhigh / 1000.0 if vhigh else None
			ctx['/System/MinCellVoltage'] = vlow / 1000.0 if vlow else None
			ctx['/System/NrOfModulesOnline'] = d.get('packs')
			ctx['/Info/MaxChargeCurrent'] = ccl
			ctx['/Info/MaxDischargeCurrent'] = dcl
			ctx['/Info/ChargeRequest'] = 1 if self.force_charge else 0
			ctx['/Io/AllowToCharge'] = 1 if ccl > 0 else 0
			ctx['/Io/AllowToDischarge'] = 0 if self.force_charge else 1
			ctx['/Alarms/HighVoltage'] = 1 if (vhigh or 0) >= CELL_STOP_MV else 0
			ctx['/Alarms/LowSoc'] = 1 if self.force_charge else 0
		self.last_update = time.monotonic()

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
	mainloop.run()
	client.loop_stop()
	sys.exit(1)


if __name__ == '__main__':
	main()
