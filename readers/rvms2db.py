#!/usr/bin/env python3
"""
rvms2db.py - VE.Bus configuration of the three MultiPlus-II (one per phase) into wagodb.dim_config.

Source: a mk2vsc backup (.rvms, "VEConfig setting section file", mk2vsc 1.33) from /data/vebus-backup;
default is the newest file there, or pass one as argument.

Format and setting names follow the reverse engineering of the mk2vsc Python project (kylehart,
https://kylehart.github.io/mk2vsc/FORMAT.html and FIELDS.html, read 2026-10-21); Victron has no published spec:
  file     u16 29 + "VEConfig setting section file", then sections
  section  u16 name_len | name | u32 next (absolute offset of the next section) | payload | u32 checksum,
           checksum = sum of the little-endian u32 words from the length prefix up to the checksum, mod 2^32
  BareSettingInfo  per setting a 10 byte record scale(i16) offset(i16) default(u16) min(u16) max(u16)
  BareSettingData  one per device, offsets from the name start: +0x17 target id, +0x1b firmware (low 24 bits),
                   +0x3a serial, +0x4f backup time (unix), +0x57 u16 0x0180, +0x59 192 setting words (u16);
                   the upload form of the file has 10 more header bytes, everything from +0x45 shifted by 10
Scaled value: scale > 0 -> (raw + offset) * scale, scale < 0 -> (raw + offset) / -scale,
scale = 0 -> no scaling known from the device schema (value NULL).

ESS assistant (setting ids from 1000 on): behind the 192 setting words each device block carries the assistant
program VEConfig wrote (u16 length, then code). Its settings are constants inside the code, found behind fixed byte
patterns (anchors, ESS_FIELDS). Found 2026-10-24 by diffing pre_/post_pytes49_20261024.rvms (sustain 50 -> 51 V,
dynamic cut-off 52/50/49.2/48 -> 49 V, PV inverters on AC-out no -> yes with 15000 W) and checked on every backup
since 2026-10-07. Cut-off and PV frequencies/power are only in the master (L1); a value whose anchor is missing or
not unique gets no row. The restart offset (1.20 V) is not a plain constant and is not decoded.

Grid code: setting 81 (grid_code). Also read live from Venus (N/<portal>/vebus/<n>/Settings/GridCode, local MQTT
broker) as source = 'dbus', and a mismatch with the backup is reported.

device_id = VE.Bus device number of Venus (Devices/<n>/SerialNumber, 0 = L1 master, 1 = L2, 2 = L3),
mapped by serial number.

Reproducible: each run replaces all rows of its source (DELETE + INSERT in one transaction), so running it
twice on the same file gives the same table.

DB credentials: ~/.victron2db.cnf (same as victron2db.py).
Options:    [file.rvms]   backup to load (default: newest /data/vebus-backup/*.rvms)
            --print       show the rows, do not write
"""

import glob
import json
import os
import struct
import sys
import time
from datetime import datetime

import paho.mqtt.client as mqtt
import pymysql

BACKUP_DIR = '/data/vebus-backup'
DB_CNF = os.path.expanduser('~/.victron2db.cnf')
MQTT_HOST = '127.0.0.1'
COLLECT_SECONDS = 5
MAX_WAIT_SECONDS = 120
TABLE = 'dim_config'
N_SETTINGS = 192
GRID_CODE_SETTING = 81
GRID_CODES = {5: 'Germany'}   # only what Venus has confirmed (GetText '5: Germany', 2026-10-21)
MAGIC = b'\x1d\x00VEConfig setting section file'

# mk2vsc FIELDS.html: setting id -> (name, confidence)
SETTINGS = {
	0: ('flags0', 'HIGH'), 1: ('flags1', 'HIGH'), 2: ('absorption_V', 'CONFIRMED'), 3: ('float_V', 'CONFIRMED'),
	4: ('charge_current_A', 'HIGH'), 5: ('inverter_output_V', 'HIGH'), 6: ('ac1_input_limit_A', 'HIGH'),
	7: ('repeated_absorption_time_min', 'HIGH'), 8: ('repeated_absorption_interval_min', 'HIGH'),
	9: ('max_absorption_time_min', 'HIGH'), 10: ('charge_characteristic', 'HIGH'),
	11: ('dc_low_shutdown_V', 'HIGH'), 12: ('dc_low_restart_offset_V', 'HIGH'), 13: ('number_of_slaves', 'HIGH'),
	14: ('three_phase_setting', 'HIGH'), 15: ('vs_usage', 'HIGH'), 16: ('vs_on_inverter_current_high_A', 'HIGH'),
	17: ('vs_on_ubat_high_V', 'HIGH'), 18: ('vs_on_ubat_low_V', 'HIGH'), 19: ('vs_ton_inverter_current_high', 'HIGH'),
	20: ('vs_ton_ubat_high', 'HIGH'), 21: ('vs_ton_ubat_low', 'HIGH'), 22: ('vs_ton_not_charging', 'HIGH'),
	23: ('vs_ton_fan_on', 'HIGH'), 24: ('vs_ton_temperature_alarm', 'HIGH'), 25: ('vs_ton_low_battery_alarm', 'HIGH'),
	26: ('vs_ton_overload_alarm', 'HIGH'), 27: ('vs_ton_ubat_ripple_alarm', 'HIGH'),
	28: ('vs_off_inverter_current_low_A', 'HIGH'), 29: ('vs_off_ubat_high_V', 'HIGH'),
	30: ('vs_off_ubat_low_V', 'HIGH'), 31: ('vs_toff_inverter_current_low', 'HIGH'),
	32: ('vs_toff_ubat_high', 'HIGH'), 33: ('vs_toff_ubat_low', 'HIGH'), 34: ('vs_toff_charging', 'HIGH'),
	35: ('vs_toff_fan_off', 'HIGH'), 36: ('vs_toff_bulk_finished', 'HIGH'), 37: ('vs_toff_no_on_condition', 'HIGH'),
	38: ('vs_toff_no_ac_input', 'HIGH'), 39: ('vs_toff_temperature_alarm', 'HIGH'),
	40: ('vs_toff_low_battery_alarm', 'HIGH'), 41: ('vs_toff_overload_alarm', 'HIGH'),
	42: ('vs_toff_ubat_ripple_alarm', 'HIGH'), 43: ('vs_minimum_on_time', 'HIGH'),
	44: ('mains_lowest_acceptable_V', 'HIGH'), 45: ('mains_low_hysteresis_V', 'HIGH'),
	46: ('mains_highest_acceptable_V', 'HIGH'), 47: ('mains_high_hysteresis_V', 'HIGH'),
	48: ('assist_current_boost_factor', 'HIGH'), 49: ('ac2_input_limit_A', 'HIGH'),
	50: ('aes_low_current_limit_A', 'HIGH'), 51: ('aes_current_hysteresis_A', 'HIGH'),
	52: ('vs_dont_ignore_load_above_A', 'HIGH'), 53: ('vs_load_above_for_s', 'HIGH'),
	54: ('vs_ignore_ac_below_V', 'CONFIRMED'), 55: ('vs_udc_below_for_s', 'HIGH'),
	56: ('vs_ignore_load_below_A', 'HIGH'), 57: ('vs_load_below_for_min', 'HIGH'),
	58: ('vs_accept_battery_above_V', 'CONFIRMED'), 59: ('vs_udc_above_for_min', 'HIGH'),
	60: ('flags2', 'HIGH'), 61: ('flags3', 'HIGH'), 62: ('output_frequency_Hz', 'HIGH'),
	63: ('ubat_low_prealarm_offset_V', 'HIGH'), 64: ('battery_capacity_Ah', 'HIGH'),
	65: ('soc_at_bulk_end_pct', 'HIGH'), 66: ('fs_ubat_start_V', 'MEDIUM'), 67: ('fs_ubat_start_delay_s', 'MEDIUM'),
	68: ('fs_ubat_stop_V', 'MEDIUM'), 69: ('fs_ubat_stop_delay_s', 'MEDIUM'),
	70: ('vs_dont_ignore_soc_below_pct', 'HIGH'), 71: ('vll_temp_compensation', 'LOW'),
	72: ('charge_efficiency', 'HIGH'), 73: ('inverter_current_limit_during_assist_A', 'HIGH'),
	74: ('abs_to_float_soc_reset_pct', 'HIGH'), 75: ('ibat_for_stop_absorption', 'LOW'),
	76: ('invert_redundancy', 'LOW'), 77: ('expected_presence_flags0', 'LOW'),
	78: ('expected_presence_flags2', 'LOW'), 79: ('relative_phase_info', 'LOW'), 80: ('my_short_id', 'LOW'),
	81: ('grid_code', 'HIGH'), 82: ('permanent_flags0', 'LOW'), 83: ('soc_stop_invert_pct', 'MEDIUM'),
	84: ('soc_start_invert_pct', 'MEDIUM'), 85: ('info_id0', 'LOW'), 86: ('tbat_stop_charge', 'LOW'),
	87: ('temp_compensation_slope', 'LOW'), 88: ('ubat_dont_charge_V', 'HIGH'), 89: ('current_sensor_factor', 'LOW'),
	128: ('grid_settings_valid_checker_a', 'MEDIUM'), 190: ('general_grid_settings_int', 'MEDIUM'),
	191: ('grid_settings_valid_checker_b', 'MEDIUM'),
}
SETTINGS.update({i: (f'not_defined_yet_{127 - i}', 'UNKNOWN') for i in range(90, 128)})
SETTINGS.update({i: (f'grid_settings_int{i - 129}', 'UNKNOWN') for i in range(129, 190)})

# ESS assistant constants: anchor bytes -> values directly behind it, (setting id, name, bytes, divisor, confidence)
ESS_FIELDS = (
	(bytes.fromhex('51012bb244'), ((1000, 'ess_sustain_V', 2, 100, 'CONFIRMED'),)),
	(bytes.fromhex('54d40264'), ((1001, 'ess_cutoff_0005C_V', 2, 100, 'CONFIRMED'),
								  (1002, 'ess_cutoff_025C_V', 2, 100, 'CONFIRMED'),
								  (1003, 'ess_cutoff_07C_V', 2, 100, 'CONFIRMED'),
								  (1004, 'ess_cutoff_2C_V', 2, 100, 'CONFIRMED'))),
	(bytes.fromhex('2ea624'), ((1010, 'ess_pv_freq_start_reduce_Hz', 2, 100, 'HIGH'),
							   (1011, 'ess_pv_freq_min_power_Hz', 2, 100, 'HIGH'),
							   (1012, 'ess_pv_freq_disconnect_Hz', 2, 100, 'HIGH'),
							   (1013, 'ess_pv_inverter_W', 3, 1, 'CONFIRMED'),
							   (1014, 'ess_pv_panel_W', 3, 1, 'CONFIRMED'))),
)
ASSISTANT_LEN_ID = 999

DDL = f"""CREATE TABLE IF NOT EXISTS {TABLE} (
	device_id TINYINT UNSIGNED NOT NULL COMMENT 'VE.Bus device of Venus: 0 = L1 master, 1 = L2, 2 = L3',
	source VARCHAR(8) NOT NULL COMMENT 'rvms = mk2vsc backup file, dbus = live Venus value',
	setting_id SMALLINT UNSIGNED NOT NULL,
	setting_name VARCHAR(64) NULL COMMENT 'name from the mk2vsc project (FIELDS.html); ids >= 999 = ESS assistant',
	confidence VARCHAR(10) NULL COMMENT 'mk2vsc: CONFIRMED / HIGH / MEDIUM / LOW / UNKNOWN',
	raw_value SMALLINT UNSIGNED NULL,
	value DOUBLE NULL COMMENT 'scaled raw value, NULL if the device schema has no scaling',
	value_text VARCHAR(64) NULL,
	scale SMALLINT NULL,
	offset_raw SMALLINT NULL,
	default_raw SMALLINT UNSIGNED NULL,
	min_raw SMALLINT UNSIGNED NULL,
	max_raw SMALLINT UNSIGNED NULL,
	serial VARCHAR(16) NOT NULL,
	target_id INT UNSIGNED NULL,
	firmware INT UNSIGNED NULL,
	source_file VARCHAR(255) NULL,
	backup_at DATETIME NULL,
	loaded_at DATETIME NOT NULL,
	PRIMARY KEY (device_id, source, setting_id)
) COMMENT 'MultiPlus-II configuration per device, filled by python/rvms2db.py'"""

COLS = ('device_id', 'source', 'setting_id', 'setting_name', 'confidence', 'raw_value', 'value', 'value_text',
		'scale', 'offset_raw', 'default_raw', 'min_raw', 'max_raw', 'serial', 'target_id', 'firmware',
		'source_file', 'backup_at', 'loaded_at')


def scaled(raw, scale, offset):
	if scale > 0:
		return (raw + offset) * scale
	if scale < 0:
		return (raw + offset) / -scale
	return None


def sections(path, data):
	"""Yield (name, name position, end) of each section, after checking its checksum."""
	pos = len(MAGIC)
	while pos < len(data):
		nlen, = struct.unpack_from('<H', data, pos)
		name = data[pos + 2:pos + 2 + nlen].decode('ascii')
		end, = struct.unpack_from('<I', data, pos + 2 + nlen)
		if not pos < end <= len(data):
			sys.exit(f'{path}: section {name} at {pos} points to {end}')
		body = data[pos:end - 4]
		body += b'\0' * (-len(body) % 4)
		total = sum(struct.unpack(f'<{len(body) // 4}I', body)) & 0xFFFFFFFF
		stored, = struct.unpack_from('<I', data, end - 4)
		if total != stored:
			sys.exit(f'{path}: checksum of section {name} at {pos} is 0x{total:08x}, file says 0x{stored:08x}')
		yield name, pos + 2, end
		pos = end


def parse_rvms(path):
	"""Return (firmware, {setting id: info record}, [device dicts])."""
	data = open(path, 'rb').read()
	if not data.startswith(MAGIC):
		sys.exit(f'{path}: not a VEConfig setting section file')
	firmware, infos, devices = None, {}, []
	for name, b, end in sections(path, data):
		if name == 'BareSettingInfo':
			firmware = struct.unpack_from('<I', data, b + 23)[0] & 0xFFFFFF
			for sid in range(N_SETTINGS):
				infos[sid] = struct.unpack_from('<hhHHH', data, b + 30 + 10 * sid)
		elif name == 'BareSettingData':
			shift = next((s for s in (0, 10) if struct.unpack_from('<H', data, b + 0x57 + s)[0] == 0x0180), None)
			if shift is None:
				sys.exit(f'{path}: device block at {b} has no 0x0180 marker (unknown form)')
			target, fw = struct.unpack_from('<II', data, b + 0x17)
			serial = data[b + 0x3a:b + 0x45].split(b'\0')[0].decode('ascii')
			stamp, = struct.unpack_from('<I', data, b + 0x4f + shift)
			words = struct.unpack_from(f'<{N_SETTINGS}H', data, b + 0x59 + shift)
			assistant = data[b + 0x59 + shift + 2 * N_SETTINGS:end - 4]
			devices.append({'serial': serial, 'target_id': target, 'firmware': fw & 0xFFFFFF, 'assistant': assistant,
							'backup_at': datetime.fromtimestamp(stamp), 'words': words})
	if firmware is None or not devices:
		sys.exit(f'{path}: settings info or device data missing')
	for dev in devices:
		if dev['firmware'] != firmware:
			sys.exit(f"{path}: device {dev['serial']} has firmware {dev['firmware']}, settings info is for {firmware}")
	return firmware, infos, devices


def ess_values(assistant):
	"""Return [(setting id, name, raw, value, confidence)] of the ESS constants found in an assistant program."""
	if len(assistant) < 2:
		return []
	out = [(ASSISTANT_LEN_ID, 'assistant_program_len', struct.unpack_from('<H', assistant)[0], None, 'MEDIUM')]
	for anchor, fields in ESS_FIELDS:
		pos = assistant.find(anchor)
		if pos < 0 or assistant.find(anchor, pos + 1) >= 0:
			continue
		pos += len(anchor)
		for sid, name, width, divisor, confidence in fields:
			raw = int.from_bytes(assistant[pos:pos + width], 'little')
			out.append((sid, name, raw, raw / divisor, confidence))
			pos += width
	return out


def venus_vebus(expected):
	"""Return ({serial: device number}, grid code) of the first vebus service from the local Venus MQTT broker.
	Waits up to MAX_WAIT_SECONDS for `expected` serials: right after a mk2vsc access mk2-dbus lists the units
	one by one again."""
	serials, grid = {}, {}

	def on_connect(client, userdata, flags, reason_code, properties):
		client.subscribe('N/+/vebus/+/Devices/+/SerialNumber')
		client.subscribe('N/+/vebus/+/Settings/GridCode')
		client.publish(f'R/{portal_id()}/keepalive', '')

	def on_message(client, userdata, msg):
		parts = msg.topic.split('/')
		try:
			v = json.loads(msg.payload).get('value')
		except (ValueError, AttributeError):
			return
		if parts[-1] == 'SerialNumber' and v:
			serials[v] = int(parts[-2])
		elif parts[-1] == 'GridCode':
			grid['value'] = v

	client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f'rvms2db-{os.getpid()}')
	client.on_connect = on_connect
	client.on_message = on_message
	client.connect(MQTT_HOST, 1883, 30)
	client.loop_start()
	time.sleep(COLLECT_SECONDS)
	deadline = time.monotonic() + MAX_WAIT_SECONDS
	while len(serials) < expected and time.monotonic() < deadline:
		client.publish(f'R/{portal_id()}/keepalive', '')
		time.sleep(5)
	client.loop_stop()
	client.disconnect()
	return serials, grid.get('value')


def portal_id():
	with open('/sys/class/net/eth0/address') as f:
		return f.read().strip().replace(':', '')


def build_rows(path, firmware, infos, devices, serial_map, grid_code):
	now = datetime.now().replace(microsecond=0)
	rows = []
	for dev in devices:
		if dev['serial'] not in serial_map:
			sys.exit(f"serial {dev['serial']} from {path} is not on the VE.Bus (Venus knows {sorted(serial_map)})")
		device_id = serial_map[dev['serial']]
		common = {'device_id': device_id, 'serial': dev['serial'], 'target_id': dev['target_id'],
				  'firmware': firmware, 'loaded_at': now}
		for sid in range(N_SETTINGS):
			scale, offset, default, vmin, vmax = infos[sid]
			raw = dev['words'][sid]
			name, confidence = SETTINGS[sid]
			rows.append(dict(common, source='rvms', setting_id=sid, setting_name=name, confidence=confidence,
							 raw_value=raw, value=scaled(raw, scale, offset),
							 value_text=GRID_CODES.get(raw) if sid == GRID_CODE_SETTING else None,
							 scale=scale, offset_raw=offset, default_raw=default, min_raw=vmin, max_raw=vmax,
							 source_file=os.path.basename(path), backup_at=dev['backup_at']))
		for sid, name, raw, value, confidence in ess_values(dev['assistant']):
			rows.append(dict(common, source='rvms', setting_id=sid, setting_name=name, confidence=confidence,
							 raw_value=raw if raw <= 0xFFFF else None, value=value if value is not None else raw,
							 value_text=None, scale=None, offset_raw=None, default_raw=None, min_raw=None, max_raw=None,
							 source_file=os.path.basename(path), backup_at=dev['backup_at']))
		if grid_code is not None:
			if grid_code != dev['words'][GRID_CODE_SETTING]:
				print(f"WARNING: grid code of {dev['serial']} is {dev['words'][GRID_CODE_SETTING]} in the backup, "
					  f"{grid_code} live in Venus", file=sys.stderr)
			rows.append(dict(common, source='dbus', setting_id=GRID_CODE_SETTING, setting_name='grid_code',
							 confidence='CONFIRMED', raw_value=grid_code, value=grid_code,
							 value_text=GRID_CODES.get(grid_code), scale=None, offset_raw=None, default_raw=None,
							 min_raw=None, max_raw=None, source_file=None, backup_at=None))
	return rows


def write(rows):
	conn = pymysql.connect(read_default_file=DB_CNF, connect_timeout=10)
	try:
		with conn.cursor() as cur:
			cur.execute(DDL)
			for source in sorted({r['source'] for r in rows}):
				cur.execute(f'DELETE FROM {TABLE} WHERE source = %s', (source,))
			cur.executemany(f"INSERT INTO {TABLE} ({', '.join(COLS)}) VALUES ({', '.join(['%s'] * len(COLS))})",
							[[r[c] for c in COLS] for r in rows])
		conn.commit()
	finally:
		conn.close()


def main():
	args = [a for a in sys.argv[1:] if not a.startswith('--')]
	if args:
		path = args[0]
	else:
		files = glob.glob(os.path.join(BACKUP_DIR, '*.rvms'))
		if not files:
			sys.exit(f'no .rvms in {BACKUP_DIR}')
		path = max(files, key=os.path.getmtime)
	firmware, infos, devices = parse_rvms(path)
	serial_map, grid_code = venus_vebus(len(devices))
	if not serial_map:
		sys.exit('no VE.Bus device serials from Venus MQTT, cannot map device_id')
	rows = build_rows(path, firmware, infos, devices, serial_map, grid_code)
	print(f'{path}: firmware {firmware}, devices '
		  + ', '.join(f"{d['serial']}->{serial_map[d['serial']]} ({d['backup_at']})" for d in devices)
		  + f', grid code backup {sorted({d["words"][GRID_CODE_SETTING] for d in devices})} / Venus {grid_code}')
	if '--print' in sys.argv:
		for r in rows:
			print(f"{r['device_id']} {r['source']:4s} {r['setting_id']:4d} {r['setting_name']:38s} {r['confidence']:9s} "
				  f"raw={r['raw_value']} value={r['value']} {r['value_text'] or ''}")
		return
	write(rows)
	print(f'{len(rows)} rows written to {TABLE}')


if __name__ == '__main__':
	main()
