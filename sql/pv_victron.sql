-- wagodb.pv_victron: Victron system (3x MultiPlus-II 48/5000, L1/L2/L3) read from Venus OS, one row per minute.
-- Sources: com.victronenergy.vebus (inverter/charger), com.victronenergy.battery (EBox via dbus-ebox-battery),
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

  -- Battery (EBox, dbus-ebox-battery)
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
