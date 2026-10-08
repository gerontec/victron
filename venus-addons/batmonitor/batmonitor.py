#!/usr/bin/env python3
"""
batmonitor: battery protection and discharge control for a three-phase MultiPlus system whose phases sit on
SEPARATE battery banks. Replaces the VE.Bus battery monitor (switched off in all units): only the BMS SoC counts.

Template, 1:1: the soyo calculation of the Waveshare ESP32 (gerontec/sofar waveshare/sofar_waveshare.yaml,
"Soyo-Kalkulation", fw 3.14.1, the last state that ran until 2026-10-06), every 60 s:

  stale   inverter data (inverter/power_grid_exchange/json) older than 3 min        -> w = 0
  G2      EBox charger relay state != 0                                             -> no counterpart here
  G3      0 <= SoC < SOC_MIN                                                        -> w = 0 (ESP: 9 %)
  G4      bank charging, BMS power > 200 W                                          -> w = 0 (ESP: ebox/pwr)
  G5      pcc > 200 W (PV surplus; here pcc + own charging of the last cycle)        -> w = 0
  import  pcc < -100 W:   w = min(int(-pcc * 1.01) + (night ? 468 : 0), 900)
  else    w = night ? 468 : 10
  (468, 10, 900 and the 500 W cap below are doubled here, POWER_SCALE)
  night   = measured Sofar PV (Power_PV1 + Power_PV2) < 100 W
  heat pump cap: Oct-Apr and R290 running (r290/heatpump/all comp_freq_actual > 0, fresh < 3 min)
          -> w <= 500 W
  setpoint older than 90 s -> 0 (ESP RS485 watchdog; here: Hub4Mode 3 needs the setpoint re-sent)

Differences, all on purpose:
- per bank (BANKS): the gates are evaluated with each bank's BMS SoC/power; w is shared equally by the
  phases whose bank passed them. Default: Stack1 MUST (can-bus-bms, can0) -> L2, Stack2 Pytes (EBox) -> L1, L3.
- SOC_MIN 5 % (user 2026-10-08), release at 7 % (fox2db DD_CHARGE_TARGET).
- forced charge (user 2026-10-08): bank SoC < 3 % -> FORCE_CHARGE_W per phase from the grid until 5 %.
- charging from PV surplus (pcc > 200 W): KP * (pcc - 200) shared by the phases whose bank is below 100 %.
  Not soyo (that was the EBox charger fox2db); without it Hub4Mode 3 would never charge.
- output: ESS external control (Hub4Mode 3), /Hub4/Lx/AcPowerSetpoint (- = out of the battery).

DRY RUN by default (logs only). Live: BATMONITOR_LIVE=1 in /data/batmonitor/env (sourced by service/run). On exit (live) the setpoints go to 0
and Hub4Mode back to 1 (normal ESS).
"""

import json
import logging
import os
import signal
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import dbus
import paho.mqtt.client as mqtt
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

VERSION = '0.10'
LIVE = os.environ.get('BATMONITOR_LIVE') == '1'
MQTT_HOST = os.environ.get('BATMONITOR_MQTT_HOST', '192.168.178.218')
INVERTER_TOPIC = 'inverter/power_grid_exchange/json'
R290_TOPIC = 'r290/heatpump/all'

BANKS = {
	'STACK1_MUST':  {'service': 'com.victronenergy.battery.socketcan_can0', 'phases': ('L2',)},
	'STACK2_PYTES': {'service': 'com.victronenergy.battery.ebox',           'phases': ('L1', 'L3')},
}

# soyo, 1:1 from sofar_waveshare.yaml; the power values doubled (POWER_SCALE) for the house base load of
# about 1000 W (user 2026-10-08, ran fine with Venus on 2026-10-07 at 3 x 333 W)
POWER_SCALE = 2
KP = 1.01
B_NIGHT = 468 * POWER_SCALE        # W
B_DAY_IDLE = 10 * POWER_SCALE      # W
W_MAX = 900 * POWER_SCALE          # W, total (ESP: Soyo maximum)
PCC_IMPORT_TH = -100.0     # W
PCC_SURPLUS_TH = 200.0     # W
CHARGING_TH = 200.0        # W, ESP: in_ebox > 200
NIGHT_PV_TH = 100.0        # W, ESP: fox::NIGHT_DC_TH
WP_CAP = 500 * POWER_SCALE  # W, ESP: soyo_wp_cap, Oct-Apr with the heat pump running
SUMMER_MONTHS = range(5, 10)
STALE_SECONDS = 180
SETPOINT_MAX_AGE = 90      # s, ESP: TX watchdog
CYCLE_SECONDS = 60
# batmonitor
SOC_MIN = 5.0              # %
SOC_MIN_RELEASE = 7.0      # %
SOC_FORCE = 3.0            # %
SOC_FORCE_RELEASE = 5.0    # %
FORCE_CHARGE_W = 500       # W per phase
CHARGE_MAX_PHASE = 4000    # W per phase, PV surplus charging
CHARGE_PRIORITY = ('STACK1_MUST', 'STACK2_PYTES')   # PV surplus fills the banks in this order (user 2026-10-08):
                                                    # Stack1 has one 70 A charger (~4.3 h for 300 Ah), Stack2 two
                                                    # (140 A, ~2.1 h); Stack1 first, and Stack2 is charged gentler
SOC_BALANCE_ON = 3.0      # % SoC difference between the stacks that starts balancing (user 2026-10-08: keep within +-3 %)
SOC_BALANCE_OFF = 1.0     # % back to normal (equal discharge share / CHARGE_PRIORITY) below this difference
BAT1_CHARGE_FACTOR = 0.5  # share of the Sofar Bat1 charging that counts as surplus (fox2db default)
SEND_SECONDS = 10
STATE_FILE = '/data/batmonitor/state.json'   # read by victron2db.py on the Pi host (-> wagodb.pv_victron bm_*)
TIMEZONE = ZoneInfo('Europe/Berlin')

log = logging.getLogger('batmonitor')


class BatMonitor:
	def __init__(self):
		self.bus = dbus.SystemBus()
		self.pcc = self.pv = None
		self.bat1 = 0.0
		self.inv_time = 0.0
		self.r290_hz = 0
		self.r290_time = 0.0
		self.prot = {b: False for b in BANKS}
		self.soc = {b: None for b in BANKS}
		self.lead = None        # bank more than SOC_BALANCE_ON ahead (hysteresis down to SOC_BALANCE_OFF)
		self.force = {b: False for b in BANKS}
		self.setpoints = {p: 0 for c in BANKS.values() for p in c['phases']}
		self.setpoint_time = 0.0
		self.hub4mode_set = False

	# MQTT thread: only plain assignments
	def on_inverter(self, d):
		self.pcc = d.get('ActivePower_PCC_Total', 0) * 1000.0
		self.bat1 = (d.get('Power_Bat1') or 0) * 1000.0
		self.pv = (d.get('Power_PV1', 0) + d.get('Power_PV2', 0)) * 1000.0
		self.inv_time = time.monotonic()

	def on_r290(self, d):
		self.r290_hz = int(d.get('comp_freq_actual', 0) or 0)
		self.r290_time = time.monotonic()

	def get(self, service, path):
		try:
			v = self.bus.get_object(service, path, introspect=False).GetValue(dbus_interface='com.victronenergy.BusItem')
		except dbus.exceptions.DBusException:
			return None
		if isinstance(v, dbus.Array) and len(v) == 0:
			return None
		return float(v) if isinstance(v, (dbus.Double, dbus.Int32, dbus.UInt32, dbus.Int64, dbus.Byte)) else v

	def set(self, service, path, value):
		if not LIVE:
			return
		try:
			v = dbus.Int32(value, variant_level=1) if isinstance(value, int) else dbus.Double(value, variant_level=1)
			self.bus.get_object(service, path, introspect=False).SetValue(v, dbus_interface='com.victronenergy.BusItem')
		except dbus.exceptions.DBusException as e:
			log.error('SetValue %s %s: %s', service, path, e)

	def vebus(self):
		for name in self.bus.list_names():
			if str(name).startswith('com.victronenergy.vebus.'):
				return str(name)
		return None

	def calc(self):
		"""60 s cycle: the ESP soyo calculation, per bank."""
		now = time.monotonic()
		stale = self.inv_time == 0 or now - self.inv_time > STALE_SECONDS
		month = datetime.now(TIMEZONE).month
		wp_running = self.r290_time and now - self.r290_time < STALE_SECONDS and self.r290_hz > 0
		sp, why = {}, {}
		discharge, charge = [], []
		# the meter already includes our own charging: use the measured AC-in, not the setpoints (the AC-in
		# current limit caps the real power, setpoints would wind up to CHARGE_MAX_PHASE)
		vb = self.vebus()
		own_charge = 0.0
		for p in self.setpoints:
			a = self.get(vb, '/Ac/ActiveIn/%s/P' % p) if vb else None
			if a is not None and a > 0:
				own_charge += a
		# the Sofar holds its PCC at 0 with its own battery Bat1: a Bat1 discharge is no surplus (as dbus-pcc-grid,
		# fox2db: discharge counts fully, charging with BAT1_CHARGE_FACTOR)
		bat1_eff = self.bat1 if self.bat1 < 0 else self.bat1 * BAT1_CHARGE_FACTOR
		surplus = (self.pcc or 0.0) + bat1_eff + own_charge
		for bank, cfg in BANKS.items():
			s = cfg['service']
			soc = self.get(s, '/Soc') if self.get(s, '/Connected') in (1, 1.0) else None
			self.soc[bank] = soc
			power = self.get(s, '/Dc/0/Power')
			if soc is not None:
				if soc < SOC_MIN:
					self.prot[bank] = True
				elif soc >= SOC_MIN_RELEASE:
					self.prot[bank] = False
				if soc < SOC_FORCE:
					self.force[bank] = True
				elif soc >= SOC_FORCE_RELEASE:
					self.force[bank] = False
			for p in cfg['phases']:
				sp[p] = 0
				if soc is None:
					why[p] = '%s:BMS_MISSING' % bank
				elif self.force[bank]:
					sp[p] = FORCE_CHARGE_W
					why[p] = '%s:FORCE_CHARGE(%.1f%%)' % (bank, soc)
				elif stale:
					why[p] = 'STALE'
				elif surplus > PCC_SURPLUS_TH:
					why[p] = '%s:PV_SURPLUS' % bank
					if soc < 100.0:
						charge.append(p)
				elif self.prot[bank]:
					why[p] = '%s:DISCHARGE_PROTECTION(%.1f%%)' % (bank, soc)
				elif power is not None and power > CHARGING_TH:
					why[p] = '%s:CHARGING(%.0fW)' % (bank, power)
				else:
					discharge.append(p)
		self.update_lead()
		if discharge:
			night = self.pv is not None and self.pv < NIGHT_PV_TH
			if self.pcc < PCC_IMPORT_TH:
				w = min(int(-self.pcc * KP) + (B_NIGHT if night else 0), W_MAX)
				rule = 'PROPORTIONAL'
			else:
				w = B_NIGHT if night else B_DAY_IDLE
				rule = 'IDLE'
			rule += '|NIGHT' if night else ''
			if month not in SUMMER_MONTHS and wp_running and w > WP_CAP:
				w = WP_CAP
				rule += '|WP_CAP'
			# SoC balancing: the stack more than SOC_BALANCE_ON ahead delivers alone, else equal shares
			lead_ph = [p for p in BANKS[self.lead]['phases'] if p in discharge] if self.lead else []
			give = lead_ph or discharge
			for p in discharge:
				sp[p] = -int(w / len(give)) if p in give else 0
				why[p] = rule + ('|BALANCE' if lead_ph and p in give else '|BALANCE_HOLD' if lead_ph else '')
		if charge:
			# the meter sees our own charging: surplus = pcc + charging of the last cycle (else it toggles every minute)
			# fill the banks in CHARGE_PRIORITY order: each phase of a bank up to CHARGE_MAX_PHASE, the rest to the next bank
			rest = min(int((surplus - PCC_SURPLUS_TH) * KP), CHARGE_MAX_PHASE * len(charge))
			# SoC balancing: the stack behind gets priority when the other is more than SOC_BALANCE_ON ahead
			order = CHARGE_PRIORITY
			if self.lead:
				order = tuple(b for b in CHARGE_PRIORITY if b != self.lead) + (self.lead,)
			for bank in order:
				ph = [p for p in BANKS[bank]['phases'] if p in charge]
				if not ph:
					continue
				share = min(rest, CHARGE_MAX_PHASE * len(ph))
				for p in ph:
					sp[p] = int(share / len(ph))
					why[p] += '|CHARGE'
				rest -= share
		log.info('%spcc %s W bat1 %s W pv %s W -> %s', '' if LIVE else 'DRY ',
			None if self.pcc is None else round(self.pcc), round(self.bat1), None if self.pv is None else round(self.pv),
			'  '.join('%s %+d W (%s)' % (p, sp[p], why[p]) for p in sorted(sp)))
		self.setpoints = sp
		self.setpoint_time = now
		self.write_state(sp, why, surplus)
		return True

	def write_state(self, sp, why, surplus):
		"""decisions of this cycle for victron2db.py; written to a temp file and renamed (never half a file)"""
		s1, s2 = list(BANKS)[:2]
		state = {'ts': int(time.time()), 'version': VERSION,
				 'sp': {p: sp[p] for p in sorted(sp)}, 'rule': {p: why[p][:48] for p in sorted(why)},
				 'balance_lead': self.lead, 'surplus_w': None if surplus is None else round(surplus),
				 'prot': {s1: int(self.prot[s1]), s2: int(self.prot[s2])},
				 'force': {s1: int(self.force[s1]), s2: int(self.force[s2])}}
		try:
			with open(STATE_FILE + '.tmp', 'w') as f:
				json.dump(state, f)
			os.replace(STATE_FILE + '.tmp', STATE_FILE)
		except OSError as e:
			log.error('state file: %s', e)

	def update_lead(self):
		"""bank whose SoC is more than SOC_BALANCE_ON above the other one; cleared below SOC_BALANCE_OFF"""
		a, b = list(BANKS)[:2]
		if self.soc[a] is None or self.soc[b] is None:
			self.lead = None
			return
		diff = self.soc[a] - self.soc[b]
		if self.lead is None and abs(diff) > SOC_BALANCE_ON:
			self.lead = a if diff > 0 else b
			log.info('SoC balance: %s ahead by %.1f %%', self.lead, abs(diff))
		elif self.lead is not None and abs(diff) < SOC_BALANCE_OFF:
			log.info('SoC balance: back within %.1f %%', SOC_BALANCE_OFF)
			self.lead = None

	def send(self):
		vb = self.vebus()
		if vb is None:
			return True
		sp = self.setpoints
		if time.monotonic() - self.setpoint_time > SETPOINT_MAX_AGE:
			sp = {p: 0 for p in sp}       # ESP TX watchdog
		if not self.hub4mode_set:
			self.set('com.victronenergy.settings', '/Settings/CGwacs/Hub4Mode', 3)
			self.hub4mode_set = LIVE
		for p, w in sp.items():
			self.set(vb, '/Hub4/%s/MaxFeedInPower' % p, max(0, -w) + 100)
			self.set(vb, '/Hub4/%s/AcPowerSetpoint' % p, w)
		return True

	def stop(self):
		vb = self.vebus()
		if vb:
			for p in self.setpoints:
				self.set(vb, '/Hub4/%s/AcPowerSetpoint' % p, 0)
		self.set('com.victronenergy.settings', '/Settings/CGwacs/Hub4Mode', 1)
		log.info('stopped%s', ', back to Hub4Mode 1' if LIVE else '')


def main():
	logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
	DBusGMainLoop(set_as_default=True)
	mainloop = GLib.MainLoop()
	bm = BatMonitor()
	log.info('batmonitor %s %s, banks %s', VERSION, 'LIVE' if LIVE else 'DRY RUN',
		', '.join('%s=%s' % (b, '+'.join(c['phases'])) for b, c in BANKS.items()))

	def on_connect(client, userdata, flags, reason_code, properties):
		log.info('MQTT connected to %s (%s)', MQTT_HOST, reason_code)
		client.subscribe([(INVERTER_TOPIC, 0), (R290_TOPIC, 0)])

	def on_message(client, userdata, msg):
		try:
			d = json.loads(msg.payload)
		except ValueError:
			log.warning('bad payload on %s', msg.topic)
			return
		if msg.topic == INVERTER_TOPIC:
			bm.on_inverter(d)
		elif msg.topic == R290_TOPIC:
			bm.on_r290(d)

	client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='batmonitor')
	client.on_connect = on_connect
	client.on_message = on_message
	client.connect_async(MQTT_HOST, 1883, 60)
	client.loop_start()

	# Python signal handlers do not run while GLib waits in C: let GLib deliver them
	for sig in (signal.SIGTERM, signal.SIGINT):
		GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, mainloop.quit)
	GLib.timeout_add_seconds(5, lambda: bm.calc() and False)   # first cycle once the retained MQTT data is in
	GLib.timeout_add_seconds(CYCLE_SECONDS, bm.calc)
	GLib.timeout_add_seconds(SEND_SECONDS, bm.send)
	mainloop.run()
	client.loop_stop()
	bm.stop()
	sys.exit(0)


if __name__ == '__main__':
	main()
