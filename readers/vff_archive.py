#!/usr/bin/env python3
"""vff_archive.py - archive a VE.Bus firmware file (.vff) in wagodb.vebus_firmware_file.

User 2026-11-05: keep the firmware the MultiPlus-II run (2623568 = model 2623 MultiPlus-II 48/5000/70-50,
version 568) as a file and in the db, so a firmware change can be undone and the config backups
(vebus_config_backup, made for firmware 2623568) stay usable. One row per file (sha256 unique, a second run
is a no-op); read-back check before the commit.

usage: vff_archive.py FILE.vff [note]          DB login from ~/.victron2db.cnf (as rvms2db.py)
"""
import hashlib
import os
import re
import sys

import pymysql

DB_CNF = os.path.expanduser('~/.victron2db.cnf')
MAGIC = b'\x1cVictron Energy firmware file'   # length byte 28 + text, as in all .vff seen
DDL = """CREATE TABLE IF NOT EXISTS vebus_firmware_file (
	id INT UNSIGNED NOT NULL AUTO_INCREMENT,
	file_name VARCHAR(255) NOT NULL,
	firmware INT UNSIGNED NOT NULL COMMENT 'model * 1000 + version, e.g. 2623568 (as in the .rvms backups)',
	model SMALLINT UNSIGNED NOT NULL COMMENT 'e.g. 2623 = MultiPlus-II 48/5000/70-50 (VEBus firmware numbers.txt)',
	version SMALLINT UNSIGNED NOT NULL,
	size INT UNSIGNED NOT NULL,
	md5 CHAR(32) NOT NULL,
	sha256 CHAR(64) NOT NULL,
	content MEDIUMBLOB NOT NULL COMMENT 'the .vff file as it is',
	note VARCHAR(255) NULL,
	archived_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
	PRIMARY KEY (id),
	UNIQUE KEY (sha256),
	KEY (firmware)
) COMMENT 'VE.Bus firmware files (.vff), written by readers/vff_archive.py'"""


def main():
	if len(sys.argv) < 2:
		sys.exit(__doc__)
	path = sys.argv[1]
	note = sys.argv[2] if len(sys.argv) > 2 else None
	content = open(path, 'rb').read()
	if not content.startswith(MAGIC):
		sys.exit(f'{path}: no "Victron Energy firmware file" header')
	m = re.fullmatch(r'(\d{4})(\d{3})\.vff', os.path.basename(path))
	if not m:
		sys.exit(f'{path}: name is not <model 4 digits><version 3 digits>.vff')
	model, version = int(m.group(1)), int(m.group(2))
	row = {'file_name': os.path.basename(path), 'firmware': model * 1000 + version, 'model': model,
		   'version': version, 'size': len(content), 'md5': hashlib.md5(content).hexdigest(),
		   'sha256': hashlib.sha256(content).hexdigest(), 'content': content, 'note': note}
	conn = pymysql.connect(read_default_file=DB_CNF, connect_timeout=10)
	try:
		with conn.cursor() as cur:
			cur.execute(DDL)
			cur.execute("SELECT id FROM vebus_firmware_file WHERE sha256 = %s", (row['sha256'],))
			old = cur.fetchone()
			if old:
				print(f'{path}: already archived as id {old[0]}')
				return
			cols = list(row)
			cur.execute(f"INSERT INTO vebus_firmware_file ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})",
						[row[c] for c in cols])
			new_id = cur.lastrowid
			cur.execute("SELECT LENGTH(content), MD5(content) FROM vebus_firmware_file WHERE id = %s", (new_id,))
			n, md5 = cur.fetchone()
		if n != len(content) or md5 != row['md5']:
			conn.rollback()
			sys.exit(f'{path}: read-back mismatch ({n} bytes, md5 {md5}), not archived')
		conn.commit()
	finally:
		conn.close()
	print(f"{path}: archived as id {new_id}, firmware {row['firmware']} (model {model}, version {version}), "
		  f"{len(content)} bytes, md5 {row['md5']}")


if __name__ == '__main__':
	main()
