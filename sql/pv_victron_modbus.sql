-- wagodb (Pi .218): the Venus OS Modbus-TCP register map (dbus-modbustcp attributes.csv), one row per Venus
-- version, service and register; loaded by readers/modbus_registers2db.py. RESERVED rows are the gaps of the map.
CREATE TABLE IF NOT EXISTS pv_victron_modbus (
	venus_version VARCHAR(16) NOT NULL,       -- e.g. v3.81 (/opt/victronenergy/version)
	service VARCHAR(64) NOT NULL,             -- com.victronenergy.vebus, ...
	reg_address SMALLINT UNSIGNED NOT NULL,   -- first register
	dbus_path VARCHAR(128) NOT NULL,          -- /Ac/ActiveIn/L1/P, or RESERVED
	dbus_type CHAR(1) NULL,                   -- d double, i int, s string, y byte, ...
	description TEXT NULL,                    -- unit or value map (0=...;1=...)
	data_type VARCHAR(32) NOT NULL,           -- uint16, int32, string[n], reserved[n], ...
	words SMALLINT UNSIGNED NOT NULL,         -- registers it spans
	scale DOUBLE NULL,                        -- register value = D-Bus value x scale
	access ENUM('R', 'W') NOT NULL,
	loaded_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
	PRIMARY KEY (venus_version, service, reg_address),
	KEY (service, dbus_path)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4;

-- changes between two versions: SELECT ... FROM pv_victron_modbus a LEFT JOIN pv_victron_modbus b
--   ON b.venus_version = 'v3.80' AND b.service = a.service AND b.reg_address = a.reg_address
--   WHERE a.venus_version = 'v3.81' AND (b.reg_address IS NULL OR b.dbus_path <> a.dbus_path OR b.access <> a.access);
