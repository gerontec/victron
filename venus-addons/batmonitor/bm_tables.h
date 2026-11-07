/*
 * bm_tables.h - the site parameters of batmonitor (0.78-py: only PARAM lines; the decision tables are in
 * bm_decisions.dmn, standard DMN 1.3). bm_logic.py reads this file at import; keep the format:
 *
 *   PARAM(NAME, value)
 *
 * The value this site runs with, for a name of BM_PARAMS in bm_logic.py (default, range, unit and meaning stay
 * there). Order: default in bm_logic.py < PARAM here < env BATMONITOR_<NAME> on the Pi (switches like LIVE / PYTHON
 * and short tests only). An unknown name or a value outside the range stops batmonitor at start. The log line
 * "params" marks + = from here, * = from env. Written by host/bm_params_pull.py from the editor (web/bm_params.php):
 * do not edit by hand on the Pi. Comments: C block comments on their own lines or at the end of a PARAM line.
 */

/* ---- site parameters (until 2026-11-05 in the Pi env /data/batmonitor/env) ---- */
PARAM(CELL_FORCE_RELEASE_V, 3.2)
PARAM(CELL_FORCE_V, 3.05)           /* 2026-10-24: at 49.16 V on L1 the weakest MUST cell was 2.997 V; stop before the BMS cut-off */
PARAM(CHARGE_EFF, 0.9)              /* 2026-10-09: at 0.93 the ceiling bound (MUST BMS saw only 66 A); AC->BMS 0.87-0.91 */
PARAM(CHARGE_RESERVE, 100)          /* 0.72-c: charge 100 W below the surplus; 0.74-c winter export cap */
PARAM(HOUSE_ACOUT, 0)
PARAM(SOC_FORCE, 0)
PARAM(SOC_MIN, 8)
PARAM(SOC_MIN_RELEASE, 10)
PARAM(WP_WINTER_W, 100)
