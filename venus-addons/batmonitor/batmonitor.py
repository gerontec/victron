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
  phases whose bank passed them. Default: Stack1 MUST (can-bus-bms, can0) -> L1 (middle unit), Stack2 Pytes (EBox) -> L2, L3.
- SOC_MIN 5 % (user 2026-10-08), release at 7 % (fox2db DD_CHARGE_TARGET).
- forced charge (user 2026-10-08): bank SoC < 3 % -> FORCE_CHARGE_W per phase from the grid until 5 %.
- charging from PV surplus (pcc > 200 W): KP * (pcc - 200) shared by the phases whose bank is below 100 %.
  Not soyo (that was the EBox charger fox2db); without it Hub4Mode 3 would never charge.
- output: ESS external control (Hub4Mode 3), /Hub4/Lx/AcPowerSetpoint (- = out of the battery).
- soyo optimised to PCC 0 W (0.16, user 2026-10-08, same in c/batmonitor.c 0.18): the own power of the Multis is
  kept and only the new part is scaled with KP (a PCC held at 0 by the Sofar holds the setpoint, no 1 %/cycle growth):
    charge     own charge + KP * (pcc + bat1_eff - SOYO_TARGET)       (old: KP * (surplus - 200 W) -> +200 W export)
    discharge  own discharge - Bat1 charge - KP * (pcc - SOYO_TARGET)  (old: -KP * pcc, back to IDLE once covered)
  both hold down to SOYO_HOLD_TH; entry thresholds unchanged (+200 W surplus, -100 W import).
- discharge on the Z2 point in winter (0.17, same in c/batmonitor.c 0.19): the Sofar PCC sits at Z1 and includes the
  heat pump; the heat pump shall take the cheap Z1 grid power, the batteries the expensive Z2 house power:
  z2 = pcc + WP (SDM72D em0/power) replaces pcc in the discharge rule, no night floor while the WP runs, WP_CAP only
  as fallback when em0/power is stale. Charging stays on the Z1 PCC. Summer mode (SUMMER_MONTHS): the heat pump is
  served 100 % from the batteries, discharge on the Z1 PCC as before.
- winter/summer from measured energy (0.19, same in c/ 0.21-c): batmonitor/season from readers/season.py on .218
  (summer when the export exceeds 2 x the heat pump energy over 7 days, winter below 1 x); the months Oct-Apr only
  when that message is missing or older than 2 days.
- charge block (0.15, user 2026-10-08): fox2db_logic.h Ladesperre with the clear-sky DC model (Meinel, NOAA sun
  position, strings fitted per field, tree horizon, temperature derating from aussen/temp), 1:1. Summer only
  (LADESPERRE_MONTHS, ESP: May-Aug): on a forecast 20 kW day PV surplus charging waits for the peak window
  (pcc > 20 kW, peak hour or solar noon, whatever comes first); measured clouds release it for the day.
  The rest of the year every watt of surplus charges at once. Measured proxy: pcc_avg5 + Bat1_avg5 + AC-in of
  the Multis (ESP: + ebox_w).

DRY RUN by default (logs only). Live: BATMONITOR_LIVE=1 in /data/batmonitor/env (sourced by service/run). On exit (live) the setpoints go to 0
and Hub4Mode back to 1 (normal ESS).
"""

import json
import logging
import math
import os
import signal
import sys
import time
from collections import deque
from datetime import datetime
from zoneinfo import ZoneInfo

import dbus
import paho.mqtt.client as mqtt
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

VERSION = '0.19'
LIVE = os.environ.get('BATMONITOR_LIVE') == '1'
MQTT_HOST = os.environ.get('BATMONITOR_MQTT_HOST', '192.168.178.218')
INVERTER_TOPIC = 'inverter/power_grid_exchange/json'
R290_TOPIC = 'r290/heatpump/all'
AUSSEN_TOPIC = 'aussen/temp'
WP_TOPIC = 'em0/power'     # SDM72D, heat pump electrical power in W (sdm72d.py on .218, once a minute)
WP_MAX_AGE = 150           # s, older: no Z2 correction, WP_CAP as before
WP_ON_TH = 300.0           # W: heat pump counts as running (no night floor)
SEASON_TOPIC = 'batmonitor/season'   # season.py on .218, hourly: {"mode": "summer"|"winter", ..., "ts"}
SEASON_MAX_AGE = 2 * 86400           # s: older -> month rule

BANKS = {
	# Stack1 MUST feeds the middle unit (device 2 on the wall) = HQ2606P4NCH = Devices/0 = L1 (user photo 2026-10-08)
	'STACK1_MUST':  {'service': 'com.victronenergy.battery.socketcan_can0', 'phases': ('L1',), 'capacity_wh': 300 * 51.2},
	'STACK2_PYTES': {'service': 'com.victronenergy.battery.ebox',           'phases': ('L2', 'L3'), 'capacity_wh': 300 * 51.2},
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
SOYO_TARGET = 0.0          # W at the PCC (0.16: was +200 W export in effect)
SOYO_HOLD_TH = 20.0        # W: charging / proportional discharging continues while surplus / deficit is above
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
CHARGE_MAX_PHASE = 4900    # W per phase, PV surplus charging (5000 VA rating, for sun peaks)
CHARGER_CAP_PHASE = 4200   # W per phase the 70 A charger really takes (~70 A x 56 V / 0.94); priority fills up to this
CHARGE_PRIORITY = ('STACK1_MUST', 'STACK2_PYTES')   # PV surplus fills the banks in this order (user 2026-10-08):
                                                    # Stack1 has one 70 A charger (~4.3 h for 300 Ah), Stack2 two
                                                    # (140 A, ~2.1 h); Stack1 first, and Stack2 is charged gentler
SOC_BALANCE_ON = 3.0      # % SoC difference between the stacks that starts balancing (user 2026-10-08: keep within +-3 %)
SOC_BALANCE_OFF = 1.0     # % back to normal (equal discharge share / CHARGE_PRIORITY) below this difference
BAT1_CHARGE_FACTOR = 0.5  # share of the Sofar Bat1 charging that counts as surplus (fox2db default)
SEND_SECONDS = 10
STATE_FILE = '/data/batmonitor/state.json'
ETA_WINDOW = 300          # s, average of the DC charge power for the full-charge forecast
ETA_MIN_W = 50            # W, below this average no forecast (not charging)   # read by victron2db.py on the Pi host (-> wagodb.pv_victron bm_*)
TIMEZONE = ZoneInfo('Europe/Berlin')
# charge block, 1:1 from waveshare/fox2db_logic.h (fox2db v2.9) and sofar_waveshare.yaml
LADESPERRE_ENABLE = os.environ.get('BATMONITOR_LADESPERRE', '1') == '1'
LADESPERRE_MONTHS = range(5, 9)   # ESP: LADESPERRE_MONTH_FROM 5 .. LADESPERRE_MONTH_TO 8
LADESPERRE_RATIO = 0.50           # ESP yaml: ladesperre_ratio (good weather while the ratio is <= this)
LADESPERRE_HYST = 0.25            # release only at ratio >= LADESPERRE_RATIO + LADESPERRE_HYST
LADESPERRE_NOW_RATIO = 0.20       # unaveraged ratio at which clouds count as proven -> release for the day
PCC_PEAK_TH = 20000.0             # W, DO4 limit; a forecast above it opens the window, a measured one ends it
DC_RATIO_MIN = 5000.0             # W, below this clear-sky power no ratio
AUSSEN_MAX_AGE = 900              # s, ESP: in_aussen_ms < 900000
LAT, LON = 47.6811, 11.5732
# (tilt, azimuth from south, nominal W), fitted per string (gen_pv_strings.py, 14 clear days):
# Sofar PV1 west / PV2 south, FoxESS pv2 east / pv1 west
ARRAYS = ((60, 33, 19430), (68, -12, 7690))
ARRAYS_EAST = ((59, -29, 22036), (67, 32, 2781))
# tall trees in the east sector: below the tree line only diffuse light (clearsky.tex)
HOR_AZ_SPLIT, HOR_EAST, HOR_SOUTH, HOR_DIFFUSE = 120.0, 23.0, 15.0, 0.25
KT_MONTH = (0, .331, .402, .563, .838, .909, .880, .840, .820, .760, .600, .350, .134)
TEMP_COEFF, NOCT, T_STC = -0.0030, 45.0, 25.0

log = logging.getLogger('batmonitor')


def sun_pos(t_utc):
	"""NOAA sun position: (elevation, azimuth from north) in degrees for unix UTC"""
	g = time.gmtime(t_utc)
	hour = g.tm_hour + g.tm_min / 60.0 + g.tm_sec / 3600.0
	gamma = 2.0 * math.pi / 365.0 * (g.tm_yday - 1 + (hour - 12) / 24.0)   # C tm_yday is 0-based
	eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(gamma) - 0.032077 * math.sin(gamma)
					   - 0.014615 * math.cos(2 * gamma) - 0.040849 * math.sin(2 * gamma))
	decl = (0.006918 - 0.399912 * math.cos(gamma) + 0.070257 * math.sin(gamma)
			- 0.006758 * math.cos(2 * gamma) + 0.000907 * math.sin(2 * gamma)
			- 0.002697 * math.cos(3 * gamma) + 0.00148 * math.sin(3 * gamma))
	ha = math.radians((hour * 60.0 + eqtime + 4.0 * LON) / 4.0 - 180.0)
	latr = math.radians(LAT)
	cosz = math.sin(latr) * math.sin(decl) + math.cos(latr) * math.cos(decl) * math.cos(ha)
	elev = 90.0 - math.degrees(math.acos(max(-1.0, min(1.0, cosz))))
	az_s = math.atan2(math.sin(ha), math.cos(ha) * math.sin(latr) - math.tan(decl) * math.cos(latr))
	return elev, (math.degrees(az_s) + 180.0 + 360.0) % 360.0


def calc_arrays(arrays, t, month):
	elev, az = sun_pos(t)
	if elev <= 0:
		return 0.0
	am = min(1.0 / math.sin(math.radians(elev)), 37.0)
	tr = 0.7 ** (am ** 0.678)
	kt = KT_MONTH[month] if 1 <= month <= 12 else 0.60
	shade = 1.0 if elev >= (HOR_EAST if az < HOR_AZ_SPLIT else HOR_SOUTH) else HOR_DIFFUSE
	e, s = math.radians(elev), 0.0
	for tilt, az_south, power in arrays:
		b, da = math.radians(tilt), math.radians((az - 180.0) - az_south)
		s += power * tr * kt * max(0.0, math.sin(e) * math.cos(b) + math.cos(e) * math.sin(b) * math.cos(da))
	return s * shade


def dc_now(t, month):
	"""clear-sky DC power of all four strings (W) at unix UTC t"""
	return calc_arrays(ARRAYS, t, month) + calc_arrays(ARRAYS_EAST, t, month)


def solar_noon_utc(t):
	"""astronomical local noon (hour angle 0) of the UTC day of t, unix UTC"""
	midnight = int(t // 86400) * 86400
	gamma = 2.0 * math.pi / 365.0 * (time.gmtime(midnight + 43200).tm_yday - 1)
	eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(gamma) - 0.032077 * math.sin(gamma)
					   - 0.014615 * math.cos(2 * gamma) - 0.040849 * math.sin(2 * gamma))
	return midnight + (720.0 - eqtime - 4.0 * LON) * 60.0


def dc_temp_factor(elev, ambient):
	"""defensive temperature derating (-0.30 %/K, NOCT cell temperature), clamped to 0.85..1"""
	cell = ambient + (NOCT - 20.0) / 800.0 * 1000.0 * max(0.0, math.sin(math.radians(elev)))
	return max(0.85, min(1.0, 1.0 + TEMP_COEFF * (cell - T_STC)))


class BatMonitor:
	def __init__(self):
		self.bus = dbus.SystemBus()
		self.pcc = self.pv = None
		self.bat1 = 0.0
		self.inv_time = 0.0
		self.pcc_avg5 = self.bat1_avg5 = None
		self.aussen = None
		self.aussen_time = 0.0
		self.wp = None
		self.wp_time = 0.0
		self.season = None
		self.season_ts = 0
		# charge block day latches (ESP State: peak_today, ladesperre_latched, badweather_today)
		self.ls_day = None
		self.peak_today = self.ls_latched = self.badweather_today = False
		self.ls_info = {}
		self.r290_hz = 0
		self.r290_time = 0.0
		self.prot = {b: False for b in BANKS}
		self.soc = {b: None for b in BANKS}
		self.lead = None        # bank more than SOC_BALANCE_ON ahead (hysteresis down to SOC_BALANCE_OFF)
		self.power_hist = {b: deque() for b in BANKS}   # (time, DC power W) for the 5 min average
		self.force = {b: False for b in BANKS}
		self.setpoints = {p: 0 for c in BANKS.values() for p in c['phases']}
		self.setpoint_time = 0.0
		self.hub4mode_set = False
		self.soyo_chg_prev = self.soyo_prop_prev = False

	# MQTT thread: only plain assignments
	def on_inverter(self, d):
		self.pcc = d.get('ActivePower_PCC_Total', 0) * 1000.0
		self.bat1 = (d.get('Power_Bat1') or 0) * 1000.0
		self.pv = (d.get('Power_PV1', 0) + d.get('Power_PV2', 0)) * 1000.0
		pcc5, bat5 = d.get('ActivePower_PCC_Total_avg5'), d.get('Power_Bat1_avg5')
		self.pcc_avg5 = (pcc5 if pcc5 is not None else d.get('ActivePower_PCC_Total', 0)) * 1000.0
		self.bat1_avg5 = (bat5 if bat5 is not None else (d.get('Power_Bat1') or 0)) * 1000.0
		self.inv_time = time.monotonic()

	def on_season(self, d):
		self.season = d.get('mode') if d.get('mode') in ('summer', 'winter') else None
		self.season_ts = d.get('ts') or 0
		log.info('season: %s', d)

	def on_wp(self, v):
		if -1000.0 < v < 30000.0:
			self.wp = v
			self.wp_time = time.monotonic()

	def on_aussen(self, v):
		if -40.0 < v < 55.0:
			self.aussen = v
			self.aussen_time = time.monotonic()

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
		# Z2 point = Z1 PCC + heat pump, winter only: the discharge serves only the house
		wp_fresh = self.wp_time and now - self.wp_time < WP_MAX_AGE
		season_ok = self.season and time.time() - self.season_ts < SEASON_MAX_AGE
		winter = self.season == 'winter' if season_ok else month not in SUMMER_MONTHS
		wp_eff = self.wp if (winter and wp_fresh and self.wp and self.wp > 0) else 0.0
		sp, why = {}, {}
		discharge, charge = [], []
		# the meter already includes our own charging: use the measured AC-in, not the setpoints (the AC-in
		# current limit caps the real power, setpoints would wind up to CHARGE_MAX_PHASE)
		vb = self.vebus()
		own_charge = own_discharge = 0.0
		for p in self.setpoints:
			a = self.get(vb, '/Ac/ActiveIn/%s/P' % p) if vb else None
			if a is not None and a > 0:
				own_charge += a
			elif a is not None:
				own_discharge -= a
		# the Sofar holds its PCC at 0 with its own battery Bat1: a Bat1 discharge is no surplus (as dbus-pcc-grid,
		# fox2db: discharge counts fully, charging with BAT1_CHARGE_FACTOR)
		bat1_eff = self.bat1 if self.bat1 < 0 else self.bat1 * BAT1_CHARGE_FACTOR
		# own discharge subtracted (0.18, found by c/tests/test_logic.c): else the night floor, partly going into the
		# Sofar battery, counted as PV surplus and the Multis switched to charging at night
		surplus = (self.pcc or 0.0) + bat1_eff + own_charge - own_discharge
		ladesperre = self.ladesperre(vb, stale)
		# surplus includes the own charging: once charging, it holds down to SOYO_HOLD_TH instead of 200 W
		charge_mode = surplus > PCC_SURPLUS_TH or (self.soyo_chg_prev and surplus > SOYO_HOLD_TH)
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
				elif charge_mode and ladesperre:
					why[p] = '%s:LADESPERRE(peak %sh)' % (bank, self.ls_info.get('peak_h'))
				elif charge_mode:
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
			# what the Multis already give (minus what of it goes into the Sofar battery) + the import still left
			z2 = self.pcc + wp_eff
			night_floor = night and wp_eff < WP_ON_TH   # the night base load would flow into the heat pump
			house = z2 + own_charge           # own (force) charging is no house load: it comes from the grid
			deficit = own_discharge - max(self.bat1, 0.0) - KP * (house - SOYO_TARGET)
			if house < PCC_IMPORT_TH or (self.soyo_prop_prev and deficit > SOYO_HOLD_TH):
				w = max(int(deficit), 0)          # never turn a discharge into charging (Sofar TOU charge)
				if night_floor:
					w = max(w, B_NIGHT)
				w = min(w, W_MAX)
				rule = 'PROPORTIONAL'
				self.soyo_prop_prev = True
			else:
				w = B_NIGHT if night_floor else B_DAY_IDLE
				rule = 'IDLE'
				self.soyo_prop_prev = False
			rule += '|NIGHT' if night else ''
			if wp_eff >= WP_ON_TH:
				rule += '|Z2'
			if not wp_fresh and winter and wp_running and w > WP_CAP:
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
			rest = min(int(own_charge + KP * ((self.pcc or 0.0) + bat1_eff - SOYO_TARGET)), CHARGE_MAX_PHASE * len(charge))
			# SoC balancing: the stack behind gets priority when the other is more than SOC_BALANCE_ON ahead
			order = CHARGE_PRIORITY
			if self.lead:
				order = tuple(b for b in CHARGE_PRIORITY if b != self.lead) + (self.lead,)
			for bank in order:
				ph = [p for p in BANKS[bank]['phases'] if p in charge]
				if not ph:
					continue
				share = min(rest, CHARGER_CAP_PHASE * len(ph))
				for p in ph:
					sp[p] = int(share / len(ph))
					why[p] += '|CHARGE'
				rest -= share
			# what is left after every charger got its real capacity: up to CHARGE_MAX_PHASE on all phases alike
			if rest > 0:
				for p in charge:
					sp[p] += int(min(rest / len(charge), CHARGE_MAX_PHASE - sp[p]))
		self.soyo_chg_prev = bool(charge)
		if not discharge:
			self.soyo_prop_prev = False
		fc = self.full_forecast()
		eta = '  '.join('%s full %s' % (b, datetime.fromtimestamp(f['full_at'], TIMEZONE).strftime('%H:%M') if f['full_at'] else '-')
						for b, f in fc.items())
		log.info('%spcc %s W bat1 %s W pv %s W -> %s | ' + eta, '' if LIVE else 'DRY ',
			None if self.pcc is None else round(self.pcc), round(self.bat1), None if self.pv is None else round(self.pv),
			'  '.join('%s %+d W (%s)' % (p, sp[p], why[p]) for p in sorted(sp)))
		self.setpoints = sp
		self.setpoint_time = now
		self.write_state(sp, why, surplus)
		return True

	def ladesperre(self, vb, stale):
		"""fox2db_logic.h step(): charge block until the PCC peak window, summer only. True = do not charge from PV."""
		now = time.time()
		loc = datetime.now(TIMEZONE)
		month = loc.month
		if loc.date() != self.ls_day:                    # midnight reset
			self.ls_day = loc.date()
			self.peak_today = self.ls_latched = self.badweather_today = False
		dc = dc_now(now, month)
		if dc > 0 and self.aussen_time and time.monotonic() - self.aussen_time < AUSSEN_MAX_AGE:
			dc *= dc_temp_factor(sun_pos(now)[0], self.aussen)
		midnight = loc.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
		best_w, peak_h, win_end = 0.0, -1, -1
		for h in range(5, 21):
			w = dc_now(midnight + h * 3600, month)
			if w > best_w:
				best_w, peak_h = w, h
			if w > PCC_PEAK_TH:
				win_end = h
		noon = solar_noon_utc(now)
		if self.pcc is not None and not stale and self.pcc > PCC_PEAK_TH:
			self.peak_today = True
		# measured PV minus house load: export + Sofar Bat1 + what the Multis take (ESP: + EBox charger)
		multis = 0.0
		for c in BANKS.values():
			for p in c['phases']:
				a = self.get(vb, '/Ac/ActiveIn/%s/P' % p) if vb else None
				multis += a or 0.0
		ratio = ratio_now = -1.0
		if dc > DC_RATIO_MIN and not stale and self.pcc_avg5 is not None:
			ratio = (dc - (self.pcc_avg5 + multis + self.bat1_avg5)) / dc
		if dc > DC_RATIO_MIN and not stale and self.pcc is not None:
			ratio_now = (dc - (self.pcc + multis + self.bat1)) / dc
		block = False
		if LADESPERRE_ENABLE:
			in_window = (month in LADESPERRE_MONTHS and best_w > PCC_PEAK_TH and win_end >= 0 and not self.peak_today
						 and loc.hour <= peak_h and now < noon)
			if in_window and ratio_now >= LADESPERRE_NOW_RATIO:
				self.badweather_today = True
			if not in_window:
				self.ls_latched = False
			elif ratio >= 0.0:
				if not self.ls_latched and ratio <= LADESPERRE_RATIO:
					self.ls_latched = True
				elif self.ls_latched and ratio >= LADESPERRE_RATIO + LADESPERRE_HYST:
					self.ls_latched = False
			block = in_window and self.ls_latched and not self.badweather_today
		self.ls_info = {'active': int(block), 'dc_expected_w': round(dc), 'ratio': round(ratio, 3),
						'ratio_now': round(ratio_now, 3), 'peak_h': peak_h if best_w > PCC_PEAK_TH else -1,
						'win_end_h': win_end, 'noon_h': round((noon - midnight) / 3600.0, 2),
						'peak_today': int(self.peak_today), 'badweather_today': int(self.badweather_today)}
		return block

	def write_state(self, sp, why, surplus):
		"""decisions of this cycle for victron2db.py; written to a temp file and renamed (never half a file)"""
		s1, s2 = list(BANKS)[:2]
		state = {'ts': int(time.time()), 'version': VERSION,
				 'sp': {p: sp[p] for p in sorted(sp)}, 'rule': {p: why[p][:48] for p in sorted(why)},
				 'balance_lead': self.lead, 'surplus_w': None if surplus is None else round(surplus),
				 'prot': {s1: int(self.prot[s1]), s2: int(self.prot[s2])},
				 'force': {s1: int(self.force[s1]), s2: int(self.force[s2])},
				 'forecast': self.full_forecast(), 'ladesperre': self.ls_info}
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

	def sample_power(self):
		"""DC power of each stack from its BMS every SEND_SECONDS, kept for ETA_WINDOW"""
		now = time.monotonic()
		for bank, cfg in BANKS.items():
			h = self.power_hist[bank]
			pw = self.get(cfg['service'], '/Dc/0/Power')
			if pw is not None:
				h.append((now, pw))
			while h and now - h[0][0] > ETA_WINDOW:
				h.popleft()

	def full_forecast(self):
		"""per stack: 5 min average charge power and the time 100 % SoC is reached at that power (None if not charging)"""
		out = {}
		for bank, cfg in BANKS.items():
			h, soc = self.power_hist[bank], self.soc[bank]
			avg = sum(p for _, p in h) / len(h) if h else None
			eta = None
			if avg is not None and soc is not None and avg >= ETA_MIN_W and soc < 100:
				eta = int(time.time() + (100 - soc) / 100 * cfg['capacity_wh'] / avg * 3600)
			out[bank] = {'avg5_w': None if avg is None else round(avg), 'full_at': eta}
		return out

	def send(self):
		self.sample_power()
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
		client.subscribe([(INVERTER_TOPIC, 0), (R290_TOPIC, 0), (AUSSEN_TOPIC, 0), (WP_TOPIC, 0), (SEASON_TOPIC, 0)])

	def on_message(client, userdata, msg):
		if msg.topic in (AUSSEN_TOPIC, WP_TOPIC):
			try:
				(bm.on_aussen if msg.topic == AUSSEN_TOPIC else bm.on_wp)(float(msg.payload))
			except ValueError:
				pass
			return
		try:
			d = json.loads(msg.payload)
		except ValueError:
			log.warning('bad payload on %s', msg.topic)
			return
		if msg.topic == INVERTER_TOPIC:
			bm.on_inverter(d)
		elif msg.topic == R290_TOPIC:
			bm.on_r290(d)
		elif msg.topic == SEASON_TOPIC:
			bm.on_season(d)

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
