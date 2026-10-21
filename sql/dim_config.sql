-- wagodb.dim_config: VE.Bus configuration of the three MultiPlus-II, one row per device and setting.
-- Filled (and created if missing) by readers/rvms2db.py from a mk2vsc backup (.rvms, source = 'rvms');
-- the grid code is also stored live from Venus (source = 'dbus', setting 81) as a cross-check.
-- Setting names and confidence: mk2vsc project (https://kylehart.github.io/mk2vsc/FIELDS.html).
CREATE TABLE IF NOT EXISTS dim_config (
	device_id TINYINT UNSIGNED NOT NULL COMMENT 'VE.Bus device of Venus: 0 = L1 master, 1 = L2, 2 = L3',
	source VARCHAR(8) NOT NULL COMMENT 'rvms = mk2vsc backup file, dbus = live Venus value',
	setting_id SMALLINT UNSIGNED NOT NULL,
	setting_name VARCHAR(64) NULL COMMENT 'name from the mk2vsc project (FIELDS.html)',
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
) COMMENT 'MultiPlus-II configuration per device, filled by python/rvms2db.py';

-- settings that differ between the units (a three-phase system needs identical settings):
-- SELECT setting_id, setting_name, GROUP_CONCAT(CONCAT(device_id, '=', raw_value) ORDER BY device_id)
--   FROM dim_config WHERE source = 'rvms' GROUP BY setting_id, setting_name HAVING COUNT(DISTINCT raw_value) > 1;
