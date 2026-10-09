<?php
// victron_api.php - self-describing machine/AI readable API for the Victron three-phase MultiPlus system in Lenggries
//   victron_api.php            JSON (same layout as dyness_api.php / ww_api.php: values.<key>.value/unit/description/source)
//   victron_api.php?format=md  plain-text Markdown summary for language models
// Data: wagodb.pv_victron, one row per minute from victron2db.py on the Venus Pi 4 (192.168.178.119)
// Topology: dev0 = L1 and dev2 = L3 on Stack2 (Pytes, EBox reader), dev1 = L2 (middle unit) on Stack1 (MUST, CAN BMS)
date_default_timezone_set('Europe/Berlin');
require_once __DIR__ . '/config.php';

header('Cache-Control: no-store');
header('Access-Control-Allow-Origin: *');
$self = 'https://heissa.de/web1/victron_api.php';

$errors = [];
$r = null;
$ext = [];
try {
    $pdo = new PDO("mysql:host=$db_host;dbname=$db_name;charset=utf8mb4", $db_user, $db_pass, [PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION]);
    $r = $pdo->query("SELECT * FROM pv_victron ORDER BY ts DESC LIMIT 1")->fetch(PDO::FETCH_ASSOC) ?: null;
    $ext = $pdo->query("SELECT MIN(soc) AS s2_min, MAX(soc) AS s2_max, MIN(a_soc) AS s1_min, MAX(a_soc) AS s1_max
        FROM pv_victron WHERE ts >= NOW() - INTERVAL 24 HOUR")->fetch(PDO::FETCH_ASSOC) ?: [];
} catch (Exception $e) {
    $errors[] = 'database: ' . $e->getMessage();
}
if (!$r) $errors[] = 'no row in pv_victron';

function v($value, string $unit, string $desc, string $src): array {
    return ['value' => $value, 'unit' => $unit, 'description' => $desc, 'source' => $src];
}
function num($x) { return $x === null ? null : $x + 0; }
function kw($x) { return $x === null ? null : round($x / 1000, 2); }

// VE.Bus /State (Victron dbus documentation)
$VEBUS_STATE = [0 => 'Off', 1 => 'Low power', 2 => 'Fault', 3 => 'Bulk', 4 => 'Absorption', 5 => 'Float', 6 => 'Storage',
    7 => 'Equalize', 8 => 'Passthru', 9 => 'Inverting', 10 => 'Power assist', 11 => 'Power supply', 244 => 'Sustain', 252 => 'External control'];
$VEBUS_MODE = [1 => 'Charger only', 2 => 'Inverter only', 3 => 'On', 4 => 'Off'];

$now = date('Y-m-d H:i:s');
$values = [];
if ($r) {
    $st = num($r['vebus_state']);
    $grid = ($r['grid_l1_w'] === null) ? null : $r['grid_l1_w'] + $r['grid_l2_w'] + $r['grid_l3_w'];
    $acin = ($r['ac_in_l1_w'] === null) ? null : $r['ac_in_l1_w'] + $r['ac_in_l2_w'] + $r['ac_in_l3_w'];
    $acout = ($r['ac_out_l1_w'] === null) ? null : $r['ac_out_l1_w'] + $r['ac_out_l2_w'] + $r['ac_out_l3_w'];
    $values = [
        // VE.Bus system
        'vebus_state' => v($st, '', 'VE.Bus state code (3 Bulk, 4 Absorption, 5 Float, 8 Passthru, 9 Inverting, 252 External control)', 'pv_victron.vebus_state'),
        'vebus_state_text' => v($VEBUS_STATE[$st] ?? null, 'text', 'VE.Bus state', 'derived'),
        'vebus_mode' => v(num($r['vebus_mode']), '', 'Switch position (1 Charger only, 2 Inverter only, 3 On, 4 Off)', 'pv_victron.vebus_mode'),
        'vebus_mode_text' => v($VEBUS_MODE[num($r['vebus_mode'])] ?? null, 'text', 'Switch position', 'derived'),
        'vebus_error' => v(num($r['vebus_error']), '', 'VE.Bus error code, 0 = ok', 'pv_victron.vebus_error'),
        'grid_lost' => v($r['active_input'] === null ? null : ((int)$r['active_input'] === 240 ? 1 : 0), '', 'Grid failure reported by Venus: VE.Bus active input 240 = AC-in disconnected (the Multis invert, state 9); 1 = rotary switch to the UPS leg (AC-out1)', 'pv_victron.active_input'),
        'ac_in_f_hz' => v(num($r['ac_in_f_hz']), 'Hz', 'Grid frequency at AC-in', 'pv_victron.ac_in_f_hz'),
        'ac_in_limit_a' => v(num($r['ac_in_limit_a']), 'A', 'AC input current limit', 'pv_victron.ac_in_limit_a'),
        'ac_in_kw' => v(kw($acin), 'kW', 'Power drawn at AC-in by all three MultiPlus, + = from the grid (charging), - = fed back', 'derived'),
        'ac_out_kw' => v(kw($acout), 'kW', 'Power at AC-out of all three MultiPlus (backup loads)', 'derived'),
        'dc_v' => v(num($r['dc_v']), 'V', 'DC voltage measured by the VE.Bus master (dev0)', 'pv_victron.dc_v'),
        'dc_a' => v(num($r['dc_a']), 'A', 'DC current of the VE.Bus system, + = charging', 'pv_victron.dc_a'),
        // the three units: dev0 L1 + dev2 L3 on Stack2 (Pytes), dev1 L2 on Stack1 (MUST)
        'l1_ac_in_v' => v(num($r['ac_in_l1_v']), 'V', 'dev0 / L1 (on Stack2 Pytes): AC-in voltage', 'pv_victron.ac_in_l1_v'),
        'l2_ac_in_v' => v(num($r['ac_in_l2_v']), 'V', 'dev1 / L2 (middle unit, on Stack1 MUST): AC-in voltage', 'pv_victron.ac_in_l2_v'),
        'l3_ac_in_v' => v(num($r['ac_in_l3_v']), 'V', 'dev2 / L3 (on Stack2 Pytes): AC-in voltage', 'pv_victron.ac_in_l3_v'),
        'l1_ac_in_w' => v(num($r['ac_in_l1_w']), 'W', 'dev0 / L1: AC-in power, + = charging', 'pv_victron.ac_in_l1_w'),
        'l2_ac_in_w' => v(num($r['ac_in_l2_w']), 'W', 'dev1 / L2: AC-in power, + = charging', 'pv_victron.ac_in_l2_w'),
        'l3_ac_in_w' => v(num($r['ac_in_l3_w']), 'W', 'dev2 / L3: AC-in power, + = charging', 'pv_victron.ac_in_l3_w'),
        'l1_ac_out_w' => v(num($r['ac_out_l1_w']), 'W', 'dev0 / L1: AC-out power', 'pv_victron.ac_out_l1_w'),
        'l2_ac_out_w' => v(num($r['ac_out_l2_w']), 'W', 'dev1 / L2: AC-out power', 'pv_victron.ac_out_l2_w'),
        'l3_ac_out_w' => v(num($r['ac_out_l3_w']), 'W', 'dev2 / L3: AC-out power', 'pv_victron.ac_out_l3_w'),
        'e_acin_to_inverter_kwh' => v(num($r['e_acin_to_inverter_kwh']), 'kWh', 'Energy counter AC-in -> battery (charged from the grid side)', 'pv_victron.e_acin_to_inverter_kwh'),
        'e_inverter_to_acin_kwh' => v(num($r['e_inverter_to_acin_kwh']), 'kWh', 'Energy counter battery -> AC-in (fed into the house)', 'pv_victron.e_inverter_to_acin_kwh'),
        // Stack2 = Pytes (EBox), active BMS for DVCC
        's2_soc' => v(num($r['soc']), '%', 'Stack2 (Pytes, EBox, 300 Ah) state of charge; active BMS for DVCC', 'pv_victron.soc'),
        's2_soc_min_24h' => v(num($ext['s2_min'] ?? null), '%', 'Stack2 lowest SoC in the last 24 h', 'pv_victron.soc'),
        's2_soc_max_24h' => v(num($ext['s2_max'] ?? null), '%', 'Stack2 highest SoC in the last 24 h', 'pv_victron.soc'),
        's2_bat_v' => v(num($r['bat_v']), 'V', 'Stack2 voltage', 'pv_victron.bat_v'),
        's2_bat_a' => v(num($r['bat_a']), 'A', 'Stack2 current, + = charging', 'pv_victron.bat_a'),
        's2_bat_kw' => v(kw($r['bat_w']), 'kW', 'Stack2 power, + = charging', 'pv_victron.bat_w'),
        's2_cell_max_v' => v(num($r['cell_max_v']), 'V', 'Stack2 highest cell voltage', 'pv_victron.cell_max_v'),
        's2_cell_min_v' => v(num($r['cell_min_v']), 'V', 'Stack2 lowest cell voltage', 'pv_victron.cell_min_v'),
        's2_cvl_v' => v(num($r['cvl_v']), 'V', 'Stack2 charge voltage limit (DVCC)', 'pv_victron.cvl_v'),
        's2_ccl_a' => v(num($r['ccl_a']), 'A', 'Stack2 charge current limit', 'pv_victron.ccl_a'),
        's2_dcl_a' => v(num($r['dcl_a']), 'A', 'Stack2 discharge current limit (0 = discharge blocked)', 'pv_victron.dcl_a'),
        's2_p1_cycles' => v(num($r['ebox_p1_cycles'] ?? null), 'cycles', 'Stack2 Pytes pack 1 cycle count (BMS "CYCLE Times" via ebox stat 1, refreshed every 6 h)', 'pv_victron.ebox_p1_cycles'),
        's2_p2_cycles' => v(num($r['ebox_p2_cycles'] ?? null), 'cycles', 'Stack2 Pytes pack 2 cycle count (BMS "CYCLE Times" via ebox stat 2, refreshed every 6 h)', 'pv_victron.ebox_p2_cycles'),
        's2_p3_cycles' => v(num($r['ebox_p3_cycles'] ?? null), 'cycles', 'Stack2 Pytes pack 3 cycle count (BMS "CYCLE Times" via ebox stat 3, refreshed every 6 h)', 'pv_victron.ebox_p3_cycles'),
        's2_charge_request' => v($r['charge_request'] === null ? null : (bool)$r['charge_request'], 'bool', 'Stack2 ChargeRequest of dbus-ebox-battery (SoC < 5 %, 11-13 h; not used by batmonitor in Hub4Mode 3)', 'pv_victron.charge_request'),
        // Stack1 = MUST (CAN BMS on can0)
        's1_soc' => v(num($r['a_soc']), '%', 'Stack1 (MUST, CAN BMS, 300 Ah) state of charge', 'pv_victron.a_soc'),
        's1_soc_min_24h' => v(num($ext['s1_min'] ?? null), '%', 'Stack1 lowest SoC in the last 24 h', 'pv_victron.a_soc'),
        's1_soc_max_24h' => v(num($ext['s1_max'] ?? null), '%', 'Stack1 highest SoC in the last 24 h', 'pv_victron.a_soc'),
        's1_soh' => v(num($r['a_soh']), '%', 'Stack1 state of health', 'pv_victron.a_soh'),
        's1_bat_v' => v(num($r['a_bat_v']), 'V', 'Stack1 voltage', 'pv_victron.a_bat_v'),
        's1_bat_a' => v(num($r['a_bat_a']), 'A', 'Stack1 current, + = charging', 'pv_victron.a_bat_a'),
        's1_bat_kw' => v(kw($r['a_bat_w']), 'kW', 'Stack1 power, + = charging', 'pv_victron.a_bat_w'),
        's1_temp_c' => v(num($r['a_temp_c']), '°C', 'Stack1 temperature', 'pv_victron.a_temp_c'),
        's1_cell_max_v' => v(num($r['a_cell_max_v']), 'V', 'Stack1 highest cell voltage', 'pv_victron.a_cell_max_v'),
        's1_cell_min_v' => v(num($r['a_cell_min_v']), 'V', 'Stack1 lowest cell voltage', 'pv_victron.a_cell_min_v'),
        's1_cvl_v' => v(num($r['a_cvl_v']), 'V', 'Stack1 charge voltage limit requested by its BMS (not used by DVCC)', 'pv_victron.a_cvl_v'),
        's1_ccl_a' => v(num($r['a_ccl_a']), 'A', 'Stack1 charge current limit requested by its BMS', 'pv_victron.a_ccl_a'),
        's1_dcl_a' => v(num($r['a_dcl_a']), 'A', 'Stack1 discharge current limit requested by its BMS', 'pv_victron.a_dcl_a'),
        // ESS / grid
        'system_state' => v(num($r['system_state']), '', 'Venus system state', 'pv_victron.system_state'),
        'ess_setpoint_w' => v(num($r['ess_setpoint_w']), 'W', 'ESS grid setpoint (+ = import)', 'pv_victron.ess_setpoint_w'),
        'grid_kw' => v(kw($grid), 'kW', 'Grid value used by ESS: -(Sofar PCC + Bat1) with observer (dbus-pcc-grid), + = import', 'derived: pv_victron.grid_l1..l3_w'),
        // batmonitor decisions (venus-addons/batmonitor, Hub4Mode 3)
        'bm_l1_sp_w' => v(num($r['bm_l1_sp_w'] ?? null), 'W', 'batmonitor setpoint L1 (dev0, Stack2), + = charging', 'pv_victron.bm_l1_sp_w'),
        'bm_l2_sp_w' => v(num($r['bm_l2_sp_w'] ?? null), 'W', 'batmonitor setpoint L2 (dev1, Stack1), + = charging', 'pv_victron.bm_l2_sp_w'),
        'bm_l3_sp_w' => v(num($r['bm_l3_sp_w'] ?? null), 'W', 'batmonitor setpoint L3 (dev2, Stack2), + = charging', 'pv_victron.bm_l3_sp_w'),
        'bm_l1_rule' => v($r['bm_l1_rule'] ?? null, 'text', 'batmonitor rule L1', 'pv_victron.bm_l1_rule'),
        'bm_l2_rule' => v($r['bm_l2_rule'] ?? null, 'text', 'batmonitor rule L2', 'pv_victron.bm_l2_rule'),
        'bm_l3_rule' => v($r['bm_l3_rule'] ?? null, 'text', 'batmonitor rule L3', 'pv_victron.bm_l3_rule'),
        'bm_balance_lead' => v($r['bm_balance_lead'] ?? null, 'text', 'Stack more than 3 % SoC ahead (it alone feeds the house, the other is charged first); null = balanced', 'pv_victron.bm_balance_lead'),
        'bm_surplus_kw' => v(kw($r['bm_surplus_w'] ?? null), 'kW', 'Surplus used for charging: Sofar PCC + Bat1 + own AC-in charging', 'pv_victron.bm_surplus_w'),
        's1_discharge_blocked' => v(isset($r['bm_s1_prot']) ? (bool)$r['bm_s1_prot'] : null, 'bool', 'Stack1 discharge protection (SoC < 5 % until 7 %)', 'pv_victron.bm_s1_prot'),
        's2_discharge_blocked' => v(isset($r['bm_s2_prot']) ? (bool)$r['bm_s2_prot'] : null, 'bool', 'Stack2 discharge protection (SoC < 5 % until 7 %)', 'pv_victron.bm_s2_prot'),
        's1_forced_charge' => v(isset($r['bm_s1_force']) ? (bool)$r['bm_s1_force'] : null, 'bool', 'Stack1 forced grid charge (SoC < 3 % until 5 %)', 'pv_victron.bm_s1_force'),
        's2_forced_charge' => v(isset($r['bm_s2_force']) ? (bool)$r['bm_s2_force'] : null, 'bool', 'Stack2 forced grid charge (SoC < 3 % until 5 %)', 'pv_victron.bm_s2_force'),
        'consumption_kw' => v(($r['consumption_l1_w'] === null) ? null : kw($r['consumption_l1_w'] + $r['consumption_l2_w'] + $r['consumption_l3_w']), 'kW', 'Consumption seen by Venus', 'derived'),
    ];
}

$api = [
    'api' => ['name' => 'Victron MultiPlus live API', 'version' => '1.0', 'self' => $self,
              'description' => 'Latest minute of the Victron three-phase system in Lenggries: 3x MultiPlus-II 48/5000 (dev0 L1, dev1 L2 middle, dev2 L3), Venus OS v3.81 on a Raspberry Pi 4. dev1 runs on Stack1 (MUST, 300 Ah, CAN BMS), dev0 and dev2 on Stack2 (Pytes EBox, 300 Ah). ESS regulates on the Sofar PCC plus the Sofar house battery.'],
    'timestamp' => $r['ts'] ?? null,
    'timezone' => 'Europe/Berlin',
    'ok' => !$errors,
    'errors' => $errors,
    'data_age_s' => ['pv_victron' => $r ? strtotime($now) - strtotime($r['ts']) : null],
    'values' => $values,
];

if (($_GET['format'] ?? '') === 'md') {
    header('Content-Type: text/markdown; charset=utf-8');
    $fmt = function ($e) {
        $x = $e['value'];
        if ($x === null) return 'n/a';
        if (is_bool($x)) return $x ? 'yes' : 'no';
        return $x . (in_array($e['unit'], ['', 'text'], true) ? '' : ' ' . $e['unit']);
    };
    echo "# {$api['api']['name']}\n\n{$api['api']['description']}\n\n";
    echo "Timestamp: {$api['timestamp']} ({$api['timezone']}), data age {$api['data_age_s']['pv_victron']} s, status: " . ($api['ok'] ? 'ok' : 'ERROR ' . implode('; ', $errors)) . "\n\n";
    echo "| key | value | meaning |\n|---|---|---|\n";
    foreach ($values as $k => $e) echo "| $k | {$fmt($e)} | " . str_replace('|', '/', $e['description']) . " |\n";
    echo "\nJSON: {$self}\n";
    exit;
}

header('Content-Type: application/json; charset=utf-8');
echo json_encode($api, JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
