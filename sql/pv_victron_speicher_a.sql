-- wagodb.pv_victron: add the Speicher A columns (CAN-bus BMS on can0) to an existing table. Idempotent.
ALTER TABLE `pv_victron`
  ADD COLUMN IF NOT EXISTS `a_soc` decimal(4,1) DEFAULT NULL COMMENT 'SoC Speicher A in %' AFTER `charge_request`,
  ADD COLUMN IF NOT EXISTS `a_soh` decimal(4,1) DEFAULT NULL COMMENT 'SoH Speicher A in %' AFTER `a_soc`,
  ADD COLUMN IF NOT EXISTS `a_bat_v` decimal(6,2) DEFAULT NULL AFTER `a_soh`,
  ADD COLUMN IF NOT EXISTS `a_bat_a` decimal(7,2) DEFAULT NULL COMMENT '+ = Laden' AFTER `a_bat_v`,
  ADD COLUMN IF NOT EXISTS `a_bat_w` int DEFAULT NULL AFTER `a_bat_a`,
  ADD COLUMN IF NOT EXISTS `a_temp_c` decimal(4,1) DEFAULT NULL COMMENT 'BMS-Temperatur' AFTER `a_bat_w`,
  ADD COLUMN IF NOT EXISTS `a_cell_max_v` decimal(5,3) DEFAULT NULL AFTER `a_temp_c`,
  ADD COLUMN IF NOT EXISTS `a_cell_min_v` decimal(5,3) DEFAULT NULL AFTER `a_cell_max_v`,
  ADD COLUMN IF NOT EXISTS `a_cvl_v` decimal(5,2) DEFAULT NULL COMMENT 'Ladeschluss laut BMS (0x351)' AFTER `a_cell_min_v`,
  ADD COLUMN IF NOT EXISTS `a_ccl_a` decimal(6,1) DEFAULT NULL COMMENT 'max. Ladestrom laut BMS (0x351)' AFTER `a_cvl_v`,
  ADD COLUMN IF NOT EXISTS `a_dcl_a` decimal(6,1) DEFAULT NULL COMMENT 'max. Entladestrom laut BMS (0x351)' AFTER `a_ccl_a`;
