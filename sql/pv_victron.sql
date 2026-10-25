-- wagodb.pv_victron: Victron system (3x MultiPlus-II 48/5000, L1/L2/L3) read from Venus OS, one row per minute.
-- Sources: com.victronenergy.vebus (inverter/charger), com.victronenergy.battery (Speicher B: EBox via dbus-ebox-battery; Speicher A: CAN-bus BMS on can0),
-- com.victronenergy.system (ESS, grid, consumption). Units in the column names; NULL = value not available.
CREATE TABLE IF NOT EXISTS `pv_victron` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT,
  `ts` datetime NOT NULL COMMENT 'Minute der Messung (lokale Zeit)',
  `portal_id` varchar(16) NOT NULL COMMENT 'Venus Portal-ID (VRM), z.B. e45f01114b2c',

  -- VE.Bus system (all MultiPlus together)
  `vebus_state` smallint DEFAULT NULL COMMENT '/State: 0 aus,1 low power,2 Fehler,3 bulk,4 absorption,5 float,6 storage,7 equalize,8 passthru,9 inverting,10 power assist,11 power supply,252 external control',
  `vebus_mode` tinyint DEFAULT NULL COMMENT '/Mode: 1 nur Laden, 2 nur Wechselrichter, 3 an, 4 aus',
  `vebus_error` smallint DEFAULT NULL COMMENT '/VebusError, 0 = kein Fehler',
  `active_input` smallint DEFAULT NULL COMMENT '/Ac/ActiveIn/ActiveInput: 0 AC-in 1, 1 AC-in 2, 240 getrennt',
  `ac_in_limit_a` decimal(5,1) DEFAULT NULL COMMENT 'Eingangsstrombegrenzung /Ac/In/1/CurrentLimit',
  `ac_in_f_hz` decimal(5,2) DEFAULT NULL,
  `ac_in_l1_v` decimal(5,1) DEFAULT NULL,
  `ac_in_l2_v` decimal(5,1) DEFAULT NULL,
  `ac_in_l3_v` decimal(5,1) DEFAULT NULL,
  `ac_in_l1_w` int DEFAULT NULL COMMENT 'AC-Eingang je Phase, + = Bezug aus dem Netz in den MultiPlus',
  `ac_in_l2_w` int DEFAULT NULL,
  `ac_in_l3_w` int DEFAULT NULL,
  `ac_out_l1_v` decimal(5,1) DEFAULT NULL,
  `ac_out_l2_v` decimal(5,1) DEFAULT NULL,
  `ac_out_l3_v` decimal(5,1) DEFAULT NULL,
  `ac_out_l1_w` int DEFAULT NULL COMMENT 'AC-Ausgang je Phase (Verbraucher am MultiPlus-Ausgang)',
  `ac_out_l2_w` int DEFAULT NULL,
  `ac_out_l3_w` int DEFAULT NULL,
  `dc_v` decimal(6,2) DEFAULT NULL COMMENT 'DC-Spannung laut MultiPlus',
  `dc_a` decimal(7,2) DEFAULT NULL COMMENT 'DC-Strom laut MultiPlus, + = Laden',
  `charge_state` smallint DEFAULT NULL COMMENT '/VebusChargeState',
  `e_acin_to_inverter_kwh` decimal(12,3) DEFAULT NULL COMMENT 'Zaehler /Energy/AcIn1ToInverter (Laden aus dem Netz)',
  `e_inverter_to_acin_kwh` decimal(12,3) DEFAULT NULL COMMENT 'Zaehler /Energy/InverterToAcIn1 (Rueckspeisung)',
  `e_inverter_to_acout_kwh` decimal(12,3) DEFAULT NULL COMMENT 'Zaehler /Energy/InverterToAcOut',
  `e_acin_to_acout_kwh` decimal(12,3) DEFAULT NULL COMMENT 'Zaehler /Energy/AcIn1ToAcOut (Durchleitung)',

  -- Battery Speicher B (EBox, dbus-ebox-battery, com.victronenergy.battery.ebox)
  `soc` decimal(4,1) DEFAULT NULL COMMENT 'SoC der aktiven Batterie in %',
  `bat_v` decimal(6,2) DEFAULT NULL,
  `bat_a` decimal(7,2) DEFAULT NULL COMMENT '+ = Laden',
  `bat_w` int DEFAULT NULL,
  `cell_max_v` decimal(5,3) DEFAULT NULL,
  `cell_min_v` decimal(5,3) DEFAULT NULL,
  `cvl_v` decimal(5,2) DEFAULT NULL COMMENT '/Info/MaxChargeVoltage (DVCC)',
  `ccl_a` decimal(6,1) DEFAULT NULL COMMENT '/Info/MaxChargeCurrent (DVCC)',
  `dcl_a` decimal(6,1) DEFAULT NULL COMMENT '/Info/MaxDischargeCurrent (DVCC)',
  `charge_request` tinyint DEFAULT NULL COMMENT '/Info/ChargeRequest: 1 = Zwangsladung angefordert',

  -- Battery Speicher A (CAN-bus BMS on can0, Venus can-bus-bms, com.victronenergy.battery.socketcan_can0)
  `a_soc` decimal(4,1) DEFAULT NULL COMMENT 'SoC Speicher A in %',
  `a_soh` decimal(4,1) DEFAULT NULL COMMENT 'SoH Speicher A in %',
  `a_bat_v` decimal(6,2) DEFAULT NULL,
  `a_bat_a` decimal(7,2) DEFAULT NULL COMMENT '+ = Laden',
  `a_bat_w` int DEFAULT NULL,
  `a_temp_c` decimal(4,1) DEFAULT NULL COMMENT 'BMS-Temperatur',
  `a_cell_max_v` decimal(5,3) DEFAULT NULL,
  `a_cell_min_v` decimal(5,3) DEFAULT NULL,
  `a_cvl_v` decimal(5,2) DEFAULT NULL COMMENT 'Ladeschluss laut BMS (0x351)',
  `a_ccl_a` decimal(6,1) DEFAULT NULL COMMENT 'max. Ladestrom laut BMS (0x351)',
  `a_dcl_a` decimal(6,1) DEFAULT NULL COMMENT 'max. Entladestrom laut BMS (0x351)',

  -- ESS / system
  `system_state` smallint DEFAULT NULL COMMENT '/SystemState/State von com.victronenergy.system',
  `ess_setpoint_w` int DEFAULT NULL COMMENT 'ESS Netz-Sollwert /Settings/CGwacs/AcPowerSetPoint',
  `grid_l1_w` int DEFAULT NULL COMMENT 'Netzleistung je Phase laut Venus-Netzzaehler, + = Bezug; NULL ohne Zaehler',
  `grid_l2_w` int DEFAULT NULL,
  `grid_l3_w` int DEFAULT NULL,
  `consumption_l1_w` int DEFAULT NULL COMMENT 'Verbrauch je Phase laut Venus',
  `consumption_l2_w` int DEFAULT NULL,
  `consumption_l3_w` int DEFAULT NULL,

  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_victron` (`portal_id`, `ts`),
  KEY `ix_ts` (`ts`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='Victron-Anlage Lenggries: 3x MultiPlus-II 48/5000 (L1/L2/L3) + EBox, aus Venus OS (vebus/battery/system), 1 Zeile je Minute. Repo gerontec/victron, sql/pv_victron.sql.';

-- 2026-10-08: batmonitor decisions (readers/victron2db.py from /data/batmonitor/state.json)
ALTER TABLE pv_victron
  ADD COLUMN bm_l1_sp_w SMALLINT NULL COMMENT 'batmonitor AcPowerSetpoint L1 (W, + charge)',
  ADD COLUMN bm_l2_sp_w SMALLINT NULL COMMENT 'batmonitor AcPowerSetpoint L2 (W, + charge)',
  ADD COLUMN bm_l3_sp_w SMALLINT NULL COMMENT 'batmonitor AcPowerSetpoint L3 (W, + charge)',
  ADD COLUMN bm_l1_rule VARCHAR(48) NULL COMMENT 'batmonitor rule L1',
  ADD COLUMN bm_l2_rule VARCHAR(48) NULL COMMENT 'batmonitor rule L2',
  ADD COLUMN bm_l3_rule VARCHAR(48) NULL COMMENT 'batmonitor rule L3',
  ADD COLUMN bm_balance_lead VARCHAR(16) NULL COMMENT 'stack more than 3 % SoC ahead (SoC balancing), NULL = balanced',
  ADD COLUMN bm_s1_prot TINYINT NULL COMMENT 'Stack1 MUST discharge protection (SoC < 5 % until 7 %)',
  ADD COLUMN bm_s2_prot TINYINT NULL COMMENT 'Stack2 Pytes discharge protection (SoC < 5 % until 7 %)',
  ADD COLUMN bm_s1_force TINYINT NULL COMMENT 'Stack1 MUST forced grid charge (SoC < 3 % until 5 %)',
  ADD COLUMN bm_s2_force TINYINT NULL COMMENT 'Stack2 Pytes forced grid charge (SoC < 3 % until 5 %)',
  ADD COLUMN bm_surplus_w INT NULL COMMENT 'surplus used for charging: PCC + Sofar Bat1 + own AC-in charge (W)';

-- 2026-10-08: full-charge forecast per stack (batmonitor 0.14)
ALTER TABLE pv_victron
  ADD COLUMN bm_s1_chg_avg5_w INT NULL COMMENT 'Stack1 MUST DC power, 5 min average (W, + charge)',
  ADD COLUMN bm_s2_chg_avg5_w INT NULL COMMENT 'Stack2 Pytes DC power, 5 min average (W, + charge)',
  ADD COLUMN bm_s1_full_at DATETIME NULL COMMENT 'Stack1 MUST forecast 100 % SoC at the 5 min average power',
  ADD COLUMN bm_s2_full_at DATETIME NULL COMMENT 'Stack2 Pytes forecast 100 % SoC at the 5 min average power';

-- 2026-10-09: cycle count per Pytes pack (ebox_mqtt.py `ebox stat <n>` "CYCLE Times", refreshed every 6 h)
ALTER TABLE pv_victron
  ADD COLUMN ebox_p1_cycles SMALLINT UNSIGNED NULL COMMENT 'Stack2 Pytes pack 1 cycle count (BMS CYCLE Times)',
  ADD COLUMN ebox_p2_cycles SMALLINT UNSIGNED NULL COMMENT 'Stack2 Pytes pack 2 cycle count (BMS CYCLE Times)',
  ADD COLUMN ebox_p3_cycles SMALLINT UNSIGNED NULL COMMENT 'Stack2 Pytes pack 3 cycle count (BMS CYCLE Times)';

-- 2026-10-09: AC-out1 apparent power and current per phase (critical loads on AC-out1; VE.Bus S = V x I, coarse at small loads)
ALTER TABLE pv_victron
  ADD COLUMN ac_out_l1_s INT NULL COMMENT 'AC-out1 L1 apparent power (VA)',
  ADD COLUMN ac_out_l2_s INT NULL COMMENT 'AC-out1 L2 apparent power (VA)',
  ADD COLUMN ac_out_l3_s INT NULL COMMENT 'AC-out1 L3 apparent power (VA)',
  ADD COLUMN ac_out_l1_i DECIMAL(6,2) NULL COMMENT 'AC-out1 L1 current (A)',
  ADD COLUMN ac_out_l2_i DECIMAL(6,2) NULL COMMENT 'AC-out1 L2 current (A)',
  ADD COLUMN ac_out_l3_i DECIMAL(6,2) NULL COMMENT 'AC-out1 L3 current (A)';

-- 2026-10-24: batmonitor 0.60-c D1 cell as one byte (state.json d1_id, bm_d1_id in bm_logic.c); INSTANT, no table copy
ALTER TABLE pv_victron
  ADD COLUMN bm_d1_id TINYINT UNSIGNED NULL COMMENT 'batmonitor D1 cell (0.60-c): bit0 night, 1 wp_on, 2 Bat1 > BAT1_SOC_MIN, 3 Bat1 >= release, 4 stack < WP_SOC_MIN, 5 stack >= WP_SOC_MIN+2, 6 bat1_first, 7 wp_low (state before the cycle)' AFTER bm_surplus_w,
  ALGORITHM=INSTANT;

-- 2026-10-25: batmonitor 0.61-c redefines the bm_d1_id bits (WP_SOC_MIN gone, winter heat pump rule). The 0.60-c
-- values (2026-10-24 13:28 .. 2026-10-25 09:24, 1197 rows) were set to NULL (user): a D1 evaluation only makes sense
-- within one matrix version, so every non-NULL bm_d1_id is a 0.61-c cell.
UPDATE pv_victron SET bm_d1_id = NULL WHERE ts < '2026-10-25 09:25:00' AND ts >= '2026-10-24 00:00:00' AND bm_d1_id IS NOT NULL;
ALTER TABLE pv_victron
  MODIFY COLUMN bm_d1_id TINYINT UNSIGNED NULL COMMENT 'batmonitor D1 cell, matrix 0.61-c from 2026-10-25 09:25 (older rows NULL): bit0 night, 1 wp_on, 2 Bat1 > BAT1_SOC_MIN, 3 Bat1 >= release, 4 winter Oct-Mar, 5 Bat1 discharging (< -100 W), 6 bat1_first (state before the cycle)',
  ALGORITHM=INSTANT;

-- 2026-10-25: matrix versions of bm_d1_id (one row per D1 matrix build; user: an evaluation only within one version).
-- Join: pv_victron.ts >= valid_from AND (valid_to IS NULL OR ts < valid_to). New row on every D1 matrix change.
CREATE TABLE IF NOT EXISTS dim_bm_matrix (
  matrix_version VARCHAR(16) NOT NULL PRIMARY KEY COMMENT 'batmonitor build (VERSION in c/batmonitor.c)',
  git_commit     CHAR(7) NOT NULL COMMENT 'gerontec/victron commit of that build',
  valid_from     DATETIME NOT NULL COMMENT 'first batmonitor cycle of the build (local time, as pv_victron.ts)',
  valid_to       DATETIME NULL COMMENT 'restart into the next build, NULL = current',
  d1_cells       SMALLINT UNSIGNED NOT NULL COMMENT 'consistent D1 cells enumerated by make check',
  d1_outcomes    SMALLINT UNSIGNED NOT NULL COMMENT 'distinct D1 outputs',
  bit0 VARCHAR(40) NULL, bit1 VARCHAR(40) NULL, bit2 VARCHAR(40) NULL, bit3 VARCHAR(40) NULL,
  bit4 VARCHAR(40) NULL, bit5 VARCHAR(40) NULL, bit6 VARCHAR(40) NULL, bit7 VARCHAR(40) NULL,
  rows_kept      TINYINT(1) NOT NULL COMMENT '0 = its pv_victron.bm_d1_id values were set to NULL',
  note           VARCHAR(255) NULL
) ENGINE=InnoDB COMMENT='batmonitor D1 matrix versions for pv_victron.bm_d1_id';
INSERT INTO dim_bm_matrix VALUES
 ('0.60-c', '29a3b48', '2026-10-24 13:28:00', '2026-10-25 09:24:42', 192, 13,
  'night', 'wp_on', 'Bat1 > BAT1_SOC_MIN', 'Bat1 >= release', 'stack < WP_SOC_MIN', 'stack >= WP_SOC_MIN+2', 'bat1_first', 'wp_low',
  0, 'WP_SOC_MIN rule (WPLOW / SRC:WPG); values deleted 2026-10-25'),
 ('0.61-c', 'c446b5f', '2026-10-25 09:24:42', NULL, 128, 14,
  'night', 'wp_on', 'Bat1 > BAT1_SOC_MIN', 'Bat1 >= release', 'winter Oct-Mar', 'Bat1 discharging (< -100 W)', 'bat1_first', NULL,
  1, 'winter heat pump rule: WP on -> 0 W while the Sofar discharges, else WPMAX(1000)')
ON DUPLICATE KEY UPDATE git_commit = VALUES(git_commit), valid_from = VALUES(valid_from), valid_to = VALUES(valid_to);

-- 2026-10-25: the bits of each matrix version as rows: bitmask = 2^bit, so bm_d1_id = SUM(bitmask) of the set bits
-- (e.g. 94 = 2+4+8+16+64). Decode: SELECT meaning FROM dim_bm_matrix_bit WHERE matrix_version = .. AND bm_d1_id & bitmask
CREATE TABLE IF NOT EXISTS dim_bm_matrix_bit (
  matrix_version VARCHAR(16) NOT NULL COMMENT 'dim_bm_matrix.matrix_version',
  bit_no         TINYINT UNSIGNED NOT NULL COMMENT '0..7',
  bitmask        TINYINT UNSIGNED NOT NULL COMMENT '2^bit_no, the integer value of this bit in bm_d1_id',
  meaning        VARCHAR(40) NULL COMMENT 'NULL = unused in this version',
  PRIMARY KEY (matrix_version, bit_no),
  CONSTRAINT fk_bm_matrix_bit FOREIGN KEY (matrix_version) REFERENCES dim_bm_matrix (matrix_version)
) ENGINE=InnoDB COMMENT='bits of pv_victron.bm_d1_id per D1 matrix version';
INSERT INTO dim_bm_matrix_bit (matrix_version, bit_no, bitmask, meaning)
SELECT m.matrix_version, b.n, 1 << b.n,
       ELT(b.n + 1, m.bit0, m.bit1, m.bit2, m.bit3, m.bit4, m.bit5, m.bit6, m.bit7)
FROM dim_bm_matrix m
JOIN (SELECT 0 n UNION SELECT 1 UNION SELECT 2 UNION SELECT 3 UNION SELECT 4 UNION SELECT 5 UNION SELECT 6
      UNION SELECT 7) b ON 1 = 1
ON DUPLICATE KEY UPDATE bitmask = VALUES(bitmask), meaning = VALUES(meaning);
