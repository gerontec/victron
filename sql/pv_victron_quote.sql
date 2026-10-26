-- wagodb (Pi .218): daily power quote of the MultiPlus banks, written by readers/powerquote.py (Pi .123, every
-- minute as an upsert of the running day; on a restart powerquote.py continues from the stored row).
-- bank S1 = L1 / Stack1 MUST, S2 = L2+L3 / Stack2 Pytes (EBox), ALL = both. Only minutes with a quote count
-- (|AC| >= 100 W on average and AC / DC the same way). quote = dc/ac charging, ac/dc discharging.
CREATE TABLE IF NOT EXISTS pv_victron_quote (
	day DATE NOT NULL,
	bank ENUM('S1', 'S2', 'ALL') NOT NULL,
	direction ENUM('charge', 'discharge') NOT NULL,
	ac_wh DOUBLE NOT NULL DEFAULT 0,          -- |AC| energy at the unit(s): AC-in - AC-out
	dc_wh DOUBLE NOT NULL DEFAULT 0,          -- |DC| energy at the BMS
	quote DECIMAL(6, 4) NULL,                 -- eta of the day so far
	minutes SMALLINT UNSIGNED NOT NULL DEFAULT 0,
	since DATETIME NULL,                      -- start of the day's integration (midnight or the service start)
	updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
	PRIMARY KEY (day, bank, direction)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4;
