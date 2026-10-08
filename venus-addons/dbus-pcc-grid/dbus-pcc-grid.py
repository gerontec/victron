#!/usr/bin/env python3
"""
dbus-pcc-grid: publish the grid balance measured by the Sofar inverter at the point of common coupling (PCC)
on the Venus D-Bus as com.victronenergy.grid.pcc, so ESS has a grid meter.

Input: MQTT topic inverter/power_grid_exchange/json on the house broker, about once a minute:
  {"ActivePower_PCC_Total": 0.58, "PCC_substituted": 0, ..., "timestamp": "..."}
ActivePower_PCC_Total is in kW, positive = export (same convention as fox2db_logic.h).
Victron grid meters count positive = import.

The Sofar regulates its own PCC to zero with its house battery (Bat1, max 2.5 kW): when the MultiPlus draw,
the Sofar discharges Bat1 and the PCC stays at 0, so ESS on the raw PCC charged the Victron batteries out of
Bat1 (2026-10-08: 6.5 kW AC-in at PCC +0.05 kW, Bat1 -2.5 kW). As in fox2db_logic.h decide():
  excess = pcc + bat1_eff,   bat1_eff = bat1 if bat1 < 0 (discharge counts fully)
                                        bat1 * BAT1_CHARGE_FACTOR if bat1 > 0 (MQTT sofar/bat1_factor, ESP default 0.5)
  /Ac/Power = -1000 * excess
Observer (v1.2): the Sofar value arrives once a minute, ESS regulates every second; on the stale value ESS
integrated to +-6.5 kW and swung with a 2 min period (2026-10-08 09:33-09:37). The grid balance moves 1:1
with the MultiPlus AC-in power (via PCC or via Bat1), so between two Sofar values /Ac/Power is published
every second as
  measured + (AC-in now - AC-in at the measurement)
AC-in at the measurement = value MEAS_LAG_SECONDS before the message arrived (ring buffer, 1/s).
SOC1 gate (fox2db_logic.h, 1:1): the Sofar battery has priority. Until SOC_Bat1 was above SOC1_FULL_TH once
today, ESS charging is capped (/Settings/CGwacs/MaxChargePower) at max(SOC1_GATE_MAX_W, export + MultiPlus
AC-in power); afterwards -1 (no limit). Daily latch, closes again at local midnight.

Limits:
- only the total is measured; /Ac/L1..L3/Power carry a third each (estimate, not a per-phase measurement)
- one value per minute: ESS regulates on this with up to a minute of delay
- no data for STALE_SECONDS: exit; daemontools restarts it and the service only registers again with
  fresh data, so ESS sees no grid meter meanwhile
"""

import json
import logging
import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import dbus
import paho.mqtt.client as mqtt
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

sys.path.insert(1, '/opt/victronenergy/dbus-systemcalc-py/ext/velib_python')
from vedbus import VeDbusService  # noqa: E402

VERSION = '1.2'
DEVICE_INSTANCE = 30
MQTT_HOST = os.environ.get('PCC_MQTT_HOST', '192.168.178.218')
MQTT_TOPIC = 'inverter/power_grid_exchange/json'
STALE_SECONDS = 180
PHASES = ('L1', 'L2', 'L3')
FACTOR_TOPIC = 'sofar/bat1_factor'
BAT1_CHARGE_FACTOR = 0.5     # ESP default (bat1_charge_factor)
SOC1_FULL_TH = 99.0          # %
SOC1_GATE_MAX_W = 5000.0     # W, lower bound of the cap while the gate is closed
MEAS_LAG_SECONDS = 5          # assumed age of the Sofar sample when the MQTT message arrives
GATE_STEP_W = 500            # MaxChargePower is a persisted setting: written in steps, only on change
TIMEZONE = ZoneInfo('Europe/Berlin')

log = logging.getLogger('dbus-pcc-grid')


class PccGrid:
	def __init__(self, mainloop):
		self.mainloop = mainloop
		self.service = None
		self.last_update = 0.0
		self.bat1_factor = BAT1_CHARGE_FACTOR
		self.soc1_full_today = False
		self.day = None
		self.max_charge_power = None
		self.measured = None          # W, + = import, from the last Sofar value
		self.acin_ref = 0.0           # W, MultiPlus AC-in at that measurement
		self.acin_hist = []           # last AC-in values, one per second
		self.substituted = None

	def register(self):
		s = VeDbusService('com.victronenergy.grid.pcc', dbus.SystemBus(), register=False)
		s.add_mandatory_paths(__file__, VERSION, 'MQTT ' + MQTT_TOPIC, deviceinstance=DEVICE_INSTANCE,
			productid=0xB005, productname='Sofar PCC (via MQTT)', firmwareversion=VERSION,
			hardwareversion=None, connected=1)
		s.add_path('/CustomName', 'PCC', writeable=True)
		s.add_path('/Ac/Power', None)
		for p in PHASES:
			s.add_path('/Ac/%s/Power' % p, None)
		s.add_path('/Ac/NumberOfPhases', len(PHASES))
		s.add_path('/Substituted', None)
		s.register()
		self.service = s
		log.info('registered com.victronenergy.grid.pcc')

	def update(self, d):
		pcc_kw = d.get('ActivePower_PCC_Total')
		if pcc_kw is None:
			log.warning('ActivePower_PCC_Total is null, skipped')
			return
		pcc = float(pcc_kw)
		bat1 = d.get('Power_Bat1')
		bat1_eff = 0.0
		if bat1 is not None:
			bat1 = float(bat1)
			bat1_eff = bat1 if bat1 < 0 else bat1 * self.bat1_factor
		self.measured = -1000.0 * (pcc + bat1_eff)
		h = self.acin_hist
		self.acin_ref = h[-1 - MEAS_LAG_SECONDS] if len(h) > MEAS_LAG_SECONDS else (h[0] if h else self.acin())
		self.substituted = d.get('PCC_substituted')
		self.soc1_gate(d.get('SOC_Bat1'), pcc)
		if self.service is None:
			self.register()
		self.last_update = time.monotonic()
		self.publish()

	def acin(self):
		try:
			bus = dbus.SystemBus()
			vb = next(str(n) for n in bus.list_names() if str(n).startswith('com.victronenergy.vebus.'))
			return float(bus.get_object(vb, '/Ac/ActiveIn/P', introspect=False).GetValue(dbus_interface='com.victronenergy.BusItem'))
		except (StopIteration, dbus.exceptions.DBusException, TypeError, ValueError):
			return None

	def tick(self):
		a = self.acin()
		if a is not None:
			self.acin_hist = (self.acin_hist + [a])[-(MEAS_LAG_SECONDS + 2):]
		self.publish()
		return True

	def publish(self):
		if self.service is None or self.measured is None or not self.acin_hist:
			return
		power = self.measured + (self.acin_hist[-1] - self.acin_ref)
		with self.service as ctx:
			ctx['/Ac/Power'] = round(power)
			for p in PHASES:
				ctx['/Ac/%s/Power' % p] = round(power / len(PHASES))
			ctx['/Substituted'] = self.substituted

	def soc1_gate(self, soc1, pcc_kw):
		day = datetime.now(TIMEZONE).date()
		if day != self.day:
			self.day, self.soc1_full_today = day, False      # gate closes again every day
		if soc1 is not None and float(soc1) > SOC1_FULL_TH:
			self.soc1_full_today = True
		if self.soc1_full_today:
			limit = -1.0
		else:
			try:
				vb = next(str(n) for n in dbus.SystemBus().list_names() if str(n).startswith('com.victronenergy.vebus.'))
				acin = float(dbus.SystemBus().get_object(vb, '/Ac/ActiveIn/P', introspect=False).GetValue(dbus_interface='com.victronenergy.BusItem'))
			except (StopIteration, dbus.exceptions.DBusException, TypeError, ValueError):
				acin = 0.0
			w = max(SOC1_GATE_MAX_W, max(0.0, 1000.0 * pcc_kw) + max(0.0, acin))
			limit = float(int(w // GATE_STEP_W) * GATE_STEP_W)
		if limit != self.max_charge_power:
			try:
				dbus.SystemBus().get_object('com.victronenergy.settings', '/Settings/CGwacs/MaxChargePower',
					introspect=False).SetValue(limit, dbus_interface='com.victronenergy.BusItem')
				log.info('SOC1 gate %s: MaxChargePower %s W', 'open' if limit < 0 else 'closed', limit)
				self.max_charge_power = limit
			except dbus.exceptions.DBusException as e:
				log.error('MaxChargePower: %s', e)

	def set_factor(self, payload):
		s = payload.decode(errors='replace')
		i = s.find('FACTOR')
		i = s.find(':', i) if i >= 0 else s.find(':')
		try:
			v = float(s[i + 1:].strip(' }"\n') if i >= 0 else s)
		except ValueError:
			return
		if 0.0 <= v <= 1.0:
			self.bat1_factor = v
			log.info('bat1 charge factor %.2f', v)

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
	grid = PccGrid(mainloop)

	def on_connect(client, userdata, flags, reason_code, properties):
		log.info('MQTT connected to %s (%s)', MQTT_HOST, reason_code)
		client.subscribe([(MQTT_TOPIC, 0), (FACTOR_TOPIC, 0)])

	def on_message(client, userdata, msg):
		if msg.topic == FACTOR_TOPIC:
			GLib.idle_add(lambda: grid.set_factor(msg.payload) and False)
			return
		try:
			d = json.loads(msg.payload)
		except ValueError:
			log.warning('bad payload on %s', msg.topic)
			return
		# D-Bus is not thread safe: hand the update to the GLib main loop
		GLib.idle_add(lambda: grid.update(d) and False)

	client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='dbus-pcc-grid')
	client.on_connect = on_connect
	client.on_message = on_message
	client.connect_async(MQTT_HOST, 1883, 60)
	client.loop_start()

	GLib.timeout_add_seconds(1, grid.tick)
	GLib.timeout_add_seconds(10, grid.check_stale)
	mainloop.run()
	client.loop_stop()
	sys.exit(1)


if __name__ == '__main__':
	main()
