#!/usr/bin/env python3
"""rvms_archive.py - archive a mk2vsc / VEConfigure backup (.rvms) of the three MultiPlus-II in wagodb.

User 2026-11-05: "kann man das heutige backup der config der drei multiplus in der db archivieren?"
dim_config (rvms2db.py) keeps only the newest decoded state per device and setting, the Pi keeps 30 nightly files;
this table keeps the file itself, so any archived state can be restored with VEConfigure / mk2vsc.

One row per file (file_sha256 unique, a second run is a no-op). The files differ every night because each device
block carries its backup time, so settings_sha256 hashes only what is configured (serial, 192 setting words and the
assistant bytes per device, in serial order): the same settings_sha256 = the same configuration.

usage: rvms_archive.py FILE.rvms [note]        DB login from ~/.victron2db.cnf (as rvms2db.py)
"""
import hashlib
import json
import os
import sys

import pymysql

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rvms2db import DB_CNF, parse_rvms  # noqa: E402

DDL = """CREATE TABLE IF NOT EXISTS vebus_config_backup (
	id INT UNSIGNED NOT NULL AUTO_INCREMENT,
	file_name VARCHAR(255) NOT NULL COMMENT 'file name on the Pi (~/vebus-backup)',
	file_sha256 CHAR(64) NOT NULL COMMENT 'sha256 of the file',
	settings_sha256 CHAR(64) NOT NULL COMMENT 'sha256 of serial + setting words + assistant per device: same = same configuration',
	firmware INT UNSIGNED NOT NULL,
	devices TEXT NOT NULL COMMENT 'JSON [{serial, target_id, backup_at}] in file order',
	backup_at DATETIME NOT NULL COMMENT 'earliest device backup time in the file',
	size INT UNSIGNED NOT NULL,
	content MEDIUMBLOB NOT NULL COMMENT 'the .rvms file as it is',
	note VARCHAR(255) NULL,
	archived_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
	PRIMARY KEY (id),
	UNIQUE KEY (file_sha256),
	KEY (backup_at),
	KEY (settings_sha256)
) COMMENT 'MultiPlus-II configuration backups (.rvms) as files, written by readers/rvms_archive.py'"""


def settings_hash(devices):
	h = hashlib.sha256()
	for d in sorted(devices, key=lambda d: d['serial']):
		h.update(d['serial'].encode() + b'\0')
		h.update(b''.join(w.to_bytes(2, 'little') for w in d['words']))
		h.update(len(d['assistant']).to_bytes(4, 'little') + d['assistant'])
	return h.hexdigest()


def main():
	if len(sys.argv) < 2:
		sys.exit(__doc__)
	path = sys.argv[1]
	note = sys.argv[2] if len(sys.argv) > 2 else None
	content = open(path, 'rb').read()
	firmware, _, devices = parse_rvms(path)
	row = {
		'file_name': os.path.basename(path),
		'file_sha256': hashlib.sha256(content).hexdigest(),
		'settings_sha256': settings_hash(devices),
		'firmware': firmware,
		'devices': json.dumps([{'serial': d['serial'], 'target_id': '0x%08x' % d['target_id'],
								'backup_at': d['backup_at'].strftime('%Y-%m-%d %H:%M:%S')} for d in devices]),
		'backup_at': min(d['backup_at'] for d in devices),
		'size': len(content),
		'content': content,
		'note': note,
	}
	conn = pymysql.connect(read_default_file=DB_CNF, connect_timeout=10)
	try:
		with conn.cursor() as cur:
			cur.execute(DDL)
			cur.execute("SELECT id, file_name FROM vebus_config_backup WHERE file_sha256 = %s", (row['file_sha256'],))
			old = cur.fetchone()
			if old:
				print(f"{path}: already archived as id {old[0]} ({old[1]})")
				return
			cur.execute("SELECT id, file_name FROM vebus_config_backup WHERE settings_sha256 = %s ORDER BY id DESC LIMIT 1",
						(row['settings_sha256'],))
			same = cur.fetchone()
			cols = list(row)
			cur.execute(f"INSERT INTO vebus_config_backup ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})",
						[row[c] for c in cols])
			new_id = cur.lastrowid
			cur.execute("SELECT LENGTH(content), SHA2(content, 256) FROM vebus_config_backup WHERE id = %s", (new_id,))
			n, sha = cur.fetchone()
		if n != len(content) or sha != row['file_sha256']:
			conn.rollback()
			sys.exit(f"{path}: read-back mismatch ({n} bytes, sha {sha}), not archived")
		conn.commit()
	finally:
		conn.close()
	print(f"{path}: archived as id {new_id}, {len(content)} bytes, firmware {firmware}, "
		  f"{len(devices)} devices ({', '.join(d['serial'] for d in devices)}), backup {row['backup_at']}; "
		  + (f"configuration identical to id {same[0]} ({same[1]})" if same else "configuration not archived before"))


if __name__ == '__main__':
	main()
