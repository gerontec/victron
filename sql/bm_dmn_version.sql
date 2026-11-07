-- bm_dmn_version (2026-11-07, batmonitor 0.78-py): versions of the batmonitor decision tables bm_decisions.dmn.
-- source 'editor' = saved in web/bm_dmn.php (VPN only), the Pi (host/bm_params_pull.py) checks it with its own
-- DMN reader + bm_selftest.py, then puts it live (applied_note 'ok ...') or refuses it ('rejected: ...');
-- source 'pi' = the file that runs on the Pi, published when it differs from the newest row (a repo deploy,
-- or after a rejected / rolled back version). The newest row is what the editor opens.
CREATE TABLE IF NOT EXISTS bm_dmn_version (
  id INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  source ENUM('pi', 'editor') NOT NULL,
  author VARCHAR(64) NULL,
  note VARCHAR(255) NULL,
  bm_version VARCHAR(32) NULL,
  dmn MEDIUMTEXT NOT NULL,
  sha256 CHAR(64) NOT NULL,
  applied_at DATETIME NULL,
  applied_note VARCHAR(1000) NULL,
  KEY k_sha (sha256)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
