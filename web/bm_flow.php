<?php
// bm_flow.php - batmonitor decision flow as a picture (user 2026-10-27), read only, no password.
// The stages follow bm_step() in venus-addons/batmonitor/c/bm_logic.c (0.70-c): inputs -> D7 house -> D1/S3 source
// -> S0 gates -> S1 mode per bank (D2, first match) -> lead -> S4 amount + S5 split (discharge / charge) -> CCL/DCL
// -> setpoints, beside it D5 (DO4) and the PI shadow. Mode and source rows come from the newest
// wagodb.bm_param_schema (written by the Pi from its deployed batmonitor), so new modes / sources show up by
// themselves; the stage layout itself is drawn for FLOW_VERSION and the page says so when the Pi runs another one.
// The live path is highlighted from the newest pv_victron row (rule texts "<mode> [SRC:x] [split] [limit]").
date_default_timezone_set('Europe/Berlin');
require_once __DIR__ . '/config.php';
header('Cache-Control: no-store');
const FLOW_VERSION = '0.70-c';

function h($s) { return htmlspecialchars((string)$s, ENT_QUOTES); }

try {
    $pdo = new PDO("mysql:host=$db_host;dbname=$db_name;charset=utf8mb4", $db_user, $db_pass,
                   [PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION, PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC]);
    $schema = $pdo->query("SELECT bm_version, def, created_at FROM bm_param_schema ORDER BY id DESC LIMIT 1")->fetch();
    $live = $pdo->query("SELECT ts, bm_l1_rule, bm_l2_rule, bm_l3_rule, bm_l1_sp_w, bm_l2_sp_w, bm_l3_sp_w,
        bm_balance_lead, bm_surplus_w, ac_out_l1_w + ac_out_l2_w + ac_out_l3_w AS acout
        FROM pv_victron ORDER BY ts DESC LIMIT 1")->fetch();
} catch (Exception $e) {
    http_response_code(500); exit('database: ' . h($e->getMessage()));
}
$def = $schema ? json_decode($schema['def'], true) : ['mode_rule' => [], 'src_rule' => []];

// ---- live tokens -------------------------------------------------------------------------------------------
$DIS_AMOUNT = ['HOUSE', 'PROP', 'IDLE'];                      // S4 discharge amount = S1 mode DIS
$rule = [];
$tok = [];                                                    // every token seen on any phase
$mode_of = [];                                                // L1 / L2 / L3 -> S1 mode
foreach (['l1', 'l2', 'l3'] as $p) {
    $r = $live ? trim((string)$live["bm_{$p}_rule"]) : '';
    $rule[$p] = $r;
    $parts = preg_split('/\s+/', $r, -1, PREG_SPLIT_NO_EMPTY);
    if (!$parts) continue;
    $first = preg_replace('/\(.*$/', '', $parts[0]);
    $mode_of[$p] = in_array($first, $DIS_AMOUNT) ? 'DIS' : $first;
    $tok[$first] = true;
    foreach (array_slice($parts, 1) as $t) {
        if (str_starts_with($t, 'SRC:')) $tok['SRC:' . substr($t, 4)] = true;
        else $tok[preg_replace('/\(.*$/', '', $t)] = true;
    }
}
$bank_mode = ['STACK1_MUST' => $mode_of['l1'] ?? null, 'STACK2_PYTES' => $mode_of['l2'] ?? null];
$lead = $live['bm_balance_lead'] ?? null;
$on = fn($t) => isset($tok[$t]);

// ---- SVG helpers --------------------------------------------------------------------------------------------
$svg = [];
function box($x, $y, $w, $h, $active = false, $cls = 'node') {
    global $svg;
    $svg[] = sprintf('<rect x="%d" y="%d" width="%d" height="%d" rx="10" class="%s%s"/>', $x, $y, $w, $h, $cls, $active ? ' on' : '');
}
function txt($x, $y, $s, $cls = 't', $anchor = 'start') {
    global $svg;
    $svg[] = sprintf('<text x="%d" y="%d" class="%s" text-anchor="%s">%s</text>', $x, $y, $cls, $anchor, h($s));
}
function pill($x, $y, $label, $active, $kind = 'zero', $w = null) {
    $w = $w ?? max(52, 9 * mb_strlen($label) + 18);
    global $svg;
    $svg[] = sprintf('<rect x="%d" y="%d" width="%d" height="26" rx="13" class="pill %s%s"/>', $x, $y, $w, $kind, $active ? ' on' : '');
    txt($x + $w / 2, $y + 18, $label, 'pt' . ($active ? ' pton' : ''), 'middle');
    return $x + $w + 8;
}
function arrow($x1, $y1, $x2, $y2) {
    global $svg;
    $svg[] = sprintf('<line x1="%d" y1="%d" x2="%d" y2="%d" class="arr" marker-end="url(#ah)"/>', $x1, $y1, $x2, $y2);
}

$W = 1100;
// inputs
box(20, 10, 1060, 74);
txt(36, 34, 'Inputs, every 5 s', 'th');
txt(36, 56, 'BMS SoC / CCL / DCL / cells (Stack1 MUST on can0, Stack2 Pytes via ebox) · MultiPlus AC-in / AC-out per phase · Sofar PCC (4 s) + Bat1', 't');
txt(36, 74, 'derived: data stale · heat pump on (SDM72D ≥ 300 W) · night (PV below threshold) · winter (Oct–Mar) · own AC-in (charge / discharge)', 'ts');
arrow(280, 84, 280, 108); arrow(820, 84, 820, 108);

// D7 house
$house = $on('HOUSE') || $on('SRC:Z2') || $on('SRC:Z1');
box(20, 108, 520, 112, $house);
txt(36, 132, 'D7 · house on AC-out1', 'th');
txt(36, 154, 'on: AC-out total > 99 W · off: < 30 W for 60 s', 't');
txt(36, 174, 'winter: Z2 ≈ 0 → discharge = AC-out1 load', 't');
txt(36, 194, 'summer: Z1 ≈ 0 → PCC deficit, at least the load', 't');
txt(36, 212, $live ? sprintf('now: AC-out %d W → %s', (int)$live['acout'], $house ? 'HOUSE on' : 'off') : '', 'ts');

// D1 / S3 source
box(560, 108, 520, 112, false);
txt(576, 132, 'D1 / S3 · source: who delivers first', 'th');
$x = 576;
foreach ($def['src_rule'] as $s) $x = pill($x, 146, $s['src'], $on('SRC:' . $s['src']));
foreach (['Z2', 'Z1'] as $s) $x = pill($x, 146, $s, $on('SRC:' . $s), 'house');
txt(576, 192, 'DAY day · B1 Sofar Bat1 first · STK stacks take the Sofar over (night floor)', 'ts');
txt(576, 210, 'WPG winter, heat pump on · Z2 / Z1 house targets (D7)', 'ts');
arrow(550, 220, 550, 244);

// S0 gates
box(20, 244, 1060, 58);
txt(36, 268, 'S0 · gates per bank', 'th');
txt(36, 290, 'SoC protection · force charge · LOW latch (DCL 0 or lowest cell < CELL_FORCE_V) · ceilings: charger, DISCHARGE_MAX_PHASE, BMS CCL / DCL', 't');
arrow(550, 302, 550, 326);

// S1 mode per bank (D2): first match, priority order from the code; schema modes not in it are appended
$KIND = ['ZERO' => 'zero', 'FORCE' => 'force', 'CHARGE' => 'chg', 'DISCHARGE' => 'dis'];
$kind = [];
foreach ($def['mode_rule'] as $m) $kind[$m['mode']] = $KIND[$m['action']] ?? 'zero';
$ORDER = ['BMS_MISSING', 'FORCE', 'STALE', 'LADESPERRE', 'CHARGE', 'FULL', 'HOLD', 'PROT', 'CHARGING', 'DIS'];
foreach (array_keys($kind) as $m) if (!in_array($m, $ORDER)) $ORDER[] = $m;
box(20, 326, 1060, 128, (bool)array_filter($bank_mode));
txt(36, 350, 'S1 · mode per bank (D2) - the first rule that matches wins', 'th');
$x = 36; $pos = [];
foreach ($ORDER as $i => $m) {
    $act = in_array($m, $bank_mode, true);
    $x0 = $x;
    $x = pill($x, 364, $m, $act, $kind[$m] ?? 'zero');
    $pos[$m] = ($x0 + $x - 8) / 2;
    if ($i < count($ORDER) - 1) txt($x - 5, 382, '›', 'ts', 'middle');
}
foreach (['STACK1_MUST' => 'Stack1 MUST · L1', 'STACK2_PYTES' => 'Stack2 Pytes · L2+L3'] as $b => $lab) {
    $m = $bank_mode[$b];
    if ($m && isset($pos[$m])) txt($pos[$m], $b === 'STACK1_MUST' ? 410 : 428, '▲ ' . $lab, 'tb', 'middle');
}
txt(36, 446, 'colour = action of the mode (schema): green charge · orange discharge · blue force charge · grey 0 W · HOLD = force_full stack without surplus', 'ts');
arrow(550, 454, 550, 476);

// lead
box(20, 476, 1060, 44, (bool)$lead);
txt(36, 504, 'Lead (SoC balance): the stack ahead gives first and is charged last · now: ' . ($lead ?: 'none, both stacks alike'), 't');
arrow(280, 520, 280, 546); arrow(820, 520, 820, 546);

// discharge branch
$dis = in_array('DIS', $bank_mode, true);
box(20, 546, 520, 176, $dis);
txt(36, 570, 'Discharge phases (mode DIS)', 'th');
txt(36, 594, 'S4 amount', 'tb');
$x = 120;
$x = pill($x, 578, 'HOUSE', $on('HOUSE'), 'dis');
$x = pill($x, 578, 'PROP', $on('PROP'), 'dis');
pill($x, 578, 'IDLE', $on('IDLE'), 'dis');
txt(36, 622, 'HOUSE = AC-out1 load (D7) · PROP = PCC deficit (D3) · IDLE = idle W of the source row', 'ts');
txt(36, 650, 'S5 split', 'tb');
$x = 120;
foreach (['EQ', 'BAL', 'SPILL', 'WAIT'] as $s) $x = pill($x, 634, $s, $dis && $on($s), 'dis');
txt(36, 678, 'EQ alike · BAL lead stack alone · SPILL lead at its cap, rest to the other · WAIT', 'ts');
pill(36, 688, 'WPMAX', $on('WPMAX'), 'zero', 80);
txt(124, 706, 'winter heat pump cap (only without HOUSE)', 'ts');

// charge branch
$chg = in_array('CHARGE', $bank_mode, true);
box(560, 546, 520, 176, $chg);
txt(576, 570, 'Charge phases (mode CHARGE)', 'th');
txt(576, 594, 'S4 amount: own charge + KP × PCC surplus, at most the phase ceilings', 't');
txt(576, 650, 'S5 split', 'tb');
$x = 660;
foreach (['FF', 'BAL', 'EQ'] as $s) $x = pill($x, 634, $s, $chg && $on($s), 'chg');
txt(576, 678, 'FF force_full stack first · BAL lead stack charged last · EQ alike', 'ts');
txt(576, 706, $live ? 'surplus now: ' . (int)$live['bm_surplus_w'] . ' W' : '', 'ts');
arrow(280, 722, 280, 746); arrow(820, 722, 820, 746);

// limits
box(20, 746, 1060, 44, $on('CCL') || $on('DCL'));
txt(36, 774, 'BMS limits cut the setpoint → tag CCL (charge) / DCL (discharge)' . (($on('CCL') || $on('DCL')) ? ' · active now' : ''), 't');
arrow(550, 790, 550, 812);

// outputs per phase
$PH = ['l1' => 'L1 · Stack1 MUST', 'l2' => 'L2 · Stack2 Pytes', 'l3' => 'L3 · Stack2 Pytes'];
$i = 0;
foreach ($PH as $p => $lab) {
    $x = 20 + $i++ * 360;
    $sp = $live ? (int)$live["bm_{$p}_sp_w"] : 0;
    box($x, 812, 340, 84, true, 'out ' . ($sp < 0 ? 'odis' : ($sp > 0 ? 'ochg' : 'ozero')));
    txt($x + 16, 836, $lab, 'th');
    txt($x + 16, 862, sprintf('setpoint %+d W', $sp), 'big');
    txt($x + 16, 884, $rule[$p] ?: '-', 'ts');
}
txt(20, 922, 'Setpoints → /Hub4/L1..L3/AcPowerSetpoint (+ = charge). Beside the chain: D5 DO4 pulse "curtail WR2" when the PCC export passes 20 kW (max. every 5 min) · PI prototype in shadow mode only.', 'ts');
$H = 940;
?><!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="60">
<title>batmonitor flow</title>
<style>
:root { --bg:#0f172a; --panel:#111c33; --fg:#e2e8f0; --muted:#94a3b8; --line:#334155; --acc:#f59e0b; }
body { margin:0; padding:16px; background:var(--bg); color:var(--fg); font:14px/1.45 system-ui, sans-serif; }
h1 { font-size:20px; margin:0 0 4px; } a { color:var(--acc); }
.muted { color:var(--muted); } .warn { color:#ef4444; }
svg { width:100%; max-width:1100px; height:auto; display:block; margin-top:12px; }
.node { fill:var(--panel); stroke:var(--line); stroke-width:1.2; }
.node.on { stroke:var(--acc); stroke-width:2.4; }
.out { stroke-width:2; } .odis { fill:#2a1d0c; stroke:#f59e0b; } .ochg { fill:#0c2a1a; stroke:#22c55e; } .ozero { fill:var(--panel); stroke:var(--line); }
.t { fill:var(--fg); font-size:13px; } .ts { fill:var(--muted); font-size:11.5px; } .th { fill:var(--fg); font-size:15px; font-weight:700; }
.tb { fill:var(--fg); font-size:13px; font-weight:600; } .big { fill:var(--fg); font-size:20px; font-weight:700; }
.pill { stroke-width:1.2; fill:#0b1220; } .pill.zero { stroke:#64748b; } .pill.dis { stroke:#f59e0b; } .pill.chg { stroke:#22c55e; }
.pill.force { stroke:#60a5fa; } .pill.house { stroke:#a78bfa; }
.pill.on.zero { fill:#64748b; } .pill.on.dis { fill:#f59e0b; } .pill.on.chg { fill:#22c55e; } .pill.on.force { fill:#60a5fa; } .pill.on.house { fill:#a78bfa; }
.pt { fill:var(--fg); font-size:12px; font-weight:600; } .pton { fill:#111; }
.arr { stroke:var(--muted); stroke-width:2; }
</style></head><body>
<h1>batmonitor decision flow</h1>
<div class="muted">drawn for batmonitor <?= FLOW_VERSION ?> · running on the Pi: <?= h($schema['bm_version'] ?? '?') ?>&nbsp;<?php if ($schema && $schema['bm_version'] !== FLOW_VERSION): ?> <span class="warn">- stages may have changed, check this picture against bm_logic.c</span><?php endif ?>
· live <?= h($live['ts'] ?? '-') ?> (highlighted, refresh 60 s) · <a href="bm_params.php">parameters</a></div>
<svg viewBox="0 0 <?= $W ?> <?= $H ?>" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="batmonitor decision flow">
<defs><marker id="ah" markerWidth="10" markerHeight="10" refX="8" refY="5" orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="#94a3b8"/></marker></defs>
<?= implode("\n", $svg) ?>
</svg>
</body></html>
