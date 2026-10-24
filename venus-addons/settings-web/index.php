<?php
// Venus settings editor: every path of com.victronenergy.settings, change one value at a time.
// Served by the Venus nginx/php-fpm at /settings/ (link /var/www/venus/settings -> /data/www-settings).
// No own login: access follows the Venus security profile (profile 2 "unsecured" = open in the LAN).
// Writes go through dbus_settings.py (dbus SetValue + read-back), so localsettings logs them like a GUI change.

const HELPER = '/usr/bin/python3 /data/www-settings/dbus_settings.py';

// change the control of the Multis or what Venus sees as battery: only with the confirm box
const CRITICAL = [
	'/Settings/CGwacs/Hub4Mode',
	'/Settings/CGwacs/MaxChargePower',
	'/Settings/CGwacs/MaxDischargePower',
	'/Settings/CGwacs/AcPowerSetPoint',
	'/Settings/CGwacs/BatteryLife/MinimumSocLimit',
	'/Settings/Services/Bol',
	'/Settings/SystemSetup/*',
	'/Settings/Canbus/*',
	'/Settings/CanBms/*',
];
// maintained by Venus itself: editable, but Venus will overwrite it
const MANAGED = [
	'/Settings/CGwacs/BatteryLife/State',
	'/Settings/CGwacs/BatteryLife/SocLimit',
	'/Settings/CGwacs/BatteryLife/DischargedTime',
	'/Settings/CGwacs/BatteryLife/Flags',
	'/Settings/Gui/Gauges/*',
	'/Settings/Gui/StartWithMenuView',
	'/Settings/System/ActiveNetworkConnection',
	'/Settings/Vecan/*/FreeIdentityNumber',
];
// tooltip texts written by hand (labels and option texts of the GUI come from settings_help.json, see
// qml_settings_help.py); option values of Hub4Mode and BatteryLife/State from the constants in PageSettingsHub4.qml
const NOTES = [
	'/Settings/CGwacs/Hub4Mode' => ['label' => 'ESS mode (Hub4Mode)', 'options' => ['1' => 'optimized, phase compensation',
		'2' => 'optimized, phases split', '3' => 'ESS control disabled = "External control"'],
		'note' => 'batmonitor runs with 3 and writes the setpoints itself. Never switch while batmonitor runs: '
			. 'a change to 1 on 2026-10-11 made the Multis charge 12 kW from the grid until a VE.Bus reset.'],
	'/Settings/CGwacs/BatteryLife/State' => ['label' => 'BatteryLife / SOC guard state (Venus-managed)', 'options' => [
		'0' => 'BatteryLife disabled', '1' => 'restart', '2' => 'default', '3' => 'absorption', '4' => 'float',
		'5' => 'discharged', '6' => 'force charge', '7' => 'sustain', '8' => 'low SOC charge', '9' => 'keep batteries charged',
		'10' => 'SOC guard default', '11' => 'SOC guard discharged', '12' => 'SOC guard low SOC charge']],
	'/Settings/CGwacs/BatteryLife/SocLimit' => ['note' => 'active SOC limit, raised and lowered by BatteryLife itself'],
	'/Settings/CGwacs/MaxChargePower' => ['note' => '-1 = no limit. Written by dbus-pcc-grid (SOC1 gate), changes several times an hour.'],
	'/Settings/Services/Bol' => ['note' => 'DVCC on = 1. The GUI says: reset the VE.Bus system after disabling DVCC.'],
	'/Settings/SystemSetup/BatteryService' => ['note' => 'com.victronenergy.battery/513 = Speicher B (EBox, dbus-ebox-battery); /512 = Speicher A (CAN-bus BMS MUST). Set by venus-settings.sh apply.'],
	'/Settings/SystemSetup/BmsInstance' => ['label' => 'BMS used for DVCC', 'note' => '513 = EBox (dbus-ebox-battery), 512 = CAN-bus BMS (MUST). Set by venus-settings.sh apply.'],
	'/Settings/Canbus/can0/Profile' => ['label' => 'CAN profile of can0', 'note' => 'set by venus-can-profile bms (CAN-bus BMS LV, 500 kbit/s) together with the host bitrate, see can.md'],
	'/Settings/CGwacs/BatteryLife/DischargedTime' => ['note' => 'unix time BatteryLife last saw the battery discharged (Venus-managed)'],
	'/Settings/System/SecurityProfile' => ['label' => 'Security profile', 'options' => ['0' => 'secured', '1' => 'weak', '2' => 'unsecured (no password)'],
		'note' => 'localsettings accepts changes only from venus-platform (GUI: Settings > General)'],
];

// localsettings accepts these only from venus-platform
const READ_ONLY = ['/Settings/System/SecurityProfile'];

function matches($path, $patterns) {
	foreach ($patterns as $p)
		if (fnmatch($p, $path, FNM_NOESCAPE))
			return true;
	return false;
}

function helper($args) {
	$out = [];
	exec(HELPER . ' ' . implode(' ', array_map('escapeshellarg', $args)) . ' 2>&1', $out);
	$j = json_decode(implode("\n", $out), true);
	return is_array($j) ? $j : ['ok' => false, 'error' => 'helper: ' . implode(' ', $out)];
}

function differs($s) {
	if ($s['type'] !== 's' && $s['default'] !== null)
		return (float)$s['value'] != (float)$s['default'];
	return (string)$s['value'] !== (string)($s['default'] ?? '');
}

function fmt($v) {
	if ($v === null) return '';
	if (is_float($v)) return rtrim(rtrim(sprintf('%.6f', $v), '0'), '.');
	return (string)$v;
}

function h($s) { return htmlspecialchars((string)$s, ENT_QUOTES); }

$help = json_decode(@file_get_contents(__DIR__ . '/settings_help.json') ?: '{}', true) ?: [];
foreach (NOTES as $p => $n) {
	$h = $help[$p] ?? [];
	foreach ($n as $k => $v)
		$h[$k] = $k === 'options' ? $v + ($h['options'] ?? []) : $v;
	$help[$p] = $h;
}

// one tooltip text per path: GUI label and page, meaning of the values, note
function tip($p, $s, $help) {
	$h = $help[$p] ?? [];
	$lines = [];
	if (!empty($h['label']))
		$lines[] = $h['label'] . (!empty($h['unit']) ? ' [' . $h['unit'] . ']' : '') . (!empty($h['page']) ? '  (GUI: ' . $h['page'] . ')' : '');
	foreach ($h['options'] ?? [] as $v => $t)
		$lines[] = (fmt($s['value']) === (string)$v ? '▶ ' : '   ') . $v . ' = ' . $t;
	if (!empty($h['note']))
		$lines[] = $h['note'];
	if (!$lines)
		$lines[] = 'no description in the Venus GUI';
	$lines[] = 'default ' . fmt($s['default']) . (($s['min'] !== null || $s['max'] !== null) ? ', range ' . fmt($s['min']) . ' … ' . fmt($s['max']) : '') . ', type ' . $s['type'];
	return implode("\n", $lines);
}

function value_text($p, $s, $help) {
	$o = $help[$p]['options'] ?? [];
	return $o[fmt($s['value'])] ?? '';
}

$settings = helper(['list']);
$error = isset($settings['ok']) && $settings['ok'] === false ? $settings['error'] : null;
if ($error) $settings = [];
ksort($settings);

if ($_SERVER['REQUEST_METHOD'] === 'POST') {
	$path = $_POST['path'] ?? '';
	$value = $_POST['value'] ?? '';
	// only from this page: another web site in the browser must not be able to post here
	$origin = $_SERVER['HTTP_ORIGIN'] ?? ($_SERVER['HTTP_REFERER'] ?? '');
	$host = parse_url($origin, PHP_URL_HOST) . (parse_url($origin, PHP_URL_PORT) ? ':' . parse_url($origin, PHP_URL_PORT) : '');
	if ($host !== $_SERVER['HTTP_HOST'])
		$r = ['ok' => false, 'error' => 'refused: request not from this page'];
	elseif (!isset($settings[$path]))
		$r = ['ok' => false, 'error' => 'unknown path'];
	elseif (matches($path, READ_ONLY))
		$r = ['ok' => false, 'error' => 'read-only (only venus-platform may change it)'];
	elseif (matches($path, CRITICAL) && empty($_POST['confirm']))
		$r = ['ok' => false, 'error' => 'critical setting: tick "confirm" to write it'];
	else {
		$old = fmt($settings[$path]['value']);
		$r = helper(['set', $path, $value]);
		$r['old'] = $old;
	}
	$msg = ($r['ok'] ? 'ok' : 'ERR') . ' ' . $path . ($r['ok'] ? ': ' . ($r['old'] ?? '') . ' -> ' . fmt($r['value'] ?? '') : ': ' . ($r['error'] ?? '?'));
	$q = http_build_query(['msg' => $msg, 'ok' => $r['ok'] ? 1 : 0, 'q' => $_POST['q'] ?? '', 'nd' => $_POST['nd'] ?? '']);
	header('Location: ' . strtok($_SERVER['REQUEST_URI'], '?') . '?' . $q . '#' . rawurlencode($path), true, 303);
	exit;
}

$q = $_GET['q'] ?? '';
$nd = !empty($_GET['nd']);
$sections = [];
foreach ($settings as $p => $s) {
	$sec = explode('/', $p)[2] ?? '';
	$sections[$sec] = ($sections[$sec] ?? 0) + 1;
}
$nNonDefault = count(array_filter($settings, 'differs'));
?><!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Venus Settings</title>
<style>
:root { --bg:#f7f7f5; --fg:#1d1d1b; --muted:#6b6b66; --line:#dddcd6; --card:#fff; --accent:#1f5fa8;
	--warn:#9a5b00; --warnbg:#fff3df; --ok:#1d7a3a; --okbg:#e5f5ea; --err:#a8241f; --errbg:#fbe6e4; --nd:#eef3fb; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { --bg:#161615; --fg:#ecebe6; --muted:#9c9b94;
	--line:#34342f; --card:#1f1f1d; --accent:#7fb0ef; --warn:#f0b35a; --warnbg:#3a2a10; --ok:#7fd69a; --okbg:#173322;
	--err:#f28b84; --errbg:#3d1a18; --nd:#1d2a3c; } }
:root[data-theme="dark"] { --bg:#161615; --fg:#ecebe6; --muted:#9c9b94; --line:#34342f; --card:#1f1f1d; --accent:#7fb0ef;
	--warn:#f0b35a; --warnbg:#3a2a10; --ok:#7fd69a; --okbg:#173322; --err:#f28b84; --errbg:#3d1a18; --nd:#1d2a3c; }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:14px/1.4 system-ui, sans-serif; }
header { position:sticky; top:0; background:var(--bg); border-bottom:1px solid var(--line); padding:12px 16px; z-index:1; }
h1 { font-size:18px; margin:0 0 8px; }
.bar { display:flex; flex-wrap:wrap; gap:8px 16px; align-items:center; }
.bar input[type=search] { flex:1 1 220px; padding:6px 8px; border:1px solid var(--line); border-radius:6px; background:var(--card); color:var(--fg); }
.muted { color:var(--muted); }
.msg { margin:12px 16px 0; padding:8px 12px; border-radius:6px; }
.msg.ok { background:var(--okbg); color:var(--ok); } .msg.err { background:var(--errbg); color:var(--err); }
main { padding:0 16px 32px; }
h2 { font-size:15px; margin:20px 0 6px; }
table { width:100%; border-collapse:collapse; background:var(--card); border:1px solid var(--line); border-radius:6px; }
td { padding:5px 8px; border-top:1px solid var(--line); vertical-align:middle; }
tr.nd td { background:var(--nd); }
td.path { font-family:ui-monospace, monospace; font-size:12.5px; word-break:break-all; }
td.meta { color:var(--muted); font-size:12px; white-space:nowrap; }
form { display:flex; gap:6px; align-items:center; margin:0; flex-wrap:wrap; }
form input[name=value] { width:12em; max-width:100%; padding:4px 6px; border:1px solid var(--line); border-radius:5px; background:var(--bg); color:var(--fg); font-family:ui-monospace, monospace; }
button { padding:4px 10px; border:1px solid var(--accent); background:transparent; color:var(--accent); border-radius:5px; cursor:pointer; }
button.def { border-color:var(--line); color:var(--muted); }
.tag { font-size:11px; padding:1px 6px; border-radius:9px; margin-left:6px; white-space:nowrap; }
.tag.crit { background:var(--warnbg); color:var(--warn); } .tag.man, .tag.ro { background:var(--line); color:var(--muted); }
label.cf { font-size:12px; color:var(--warn); }
.lbl { font-family:system-ui, sans-serif; font-size:12px; color:var(--muted); }
.vt { font-size:12px; color:var(--muted); }
tr[title] { cursor:help; }
@media (max-width: 720px) { td.meta { display:none; } td { display:block; border-top:none; } tr { border-top:1px solid var(--line); display:block; } }
</style>
</head>
<body>
<header>
	<h1>Venus Settings <span class="muted" style="font-weight:normal;font-size:13px">com.victronenergy.settings · <?= count($settings) ?> paths · <?= $nNonDefault ?> non-default</span></h1>
	<div class="bar">
		<input type="search" id="q" placeholder="filter path or label, e.g. CGwacs, Hub4, DVCC" value="<?= h($q) ?>" autofocus>
		<label><input type="checkbox" id="nd" <?= $nd ? 'checked' : '' ?>> only non-default</label>
		<span class="muted" id="count"></span>
	</div>
</header>
<?php if (isset($_GET['msg'])): ?>
<div class="msg <?= !empty($_GET['ok']) ? 'ok' : 'err' ?>"><?= h($_GET['msg']) ?></div>
<?php endif; ?>
<?php if ($error): ?>
<div class="msg err">cannot read the settings: <?= h($error) ?></div>
<?php endif; ?>
<main>
<?php
$cur = null;
foreach ($settings as $p => $s):
	$sec = explode('/', $p)[2] ?? '';
	if ($sec !== $cur):
		if ($cur !== null) echo "</table>\n";
		$cur = $sec;
		echo '<h2 data-sec="' . h($sec) . '">' . h($sec) . ' <span class="muted">' . $sections[$sec] . "</span></h2>\n<table>\n";
	endif;
	$d = differs($s);
	$crit = matches($p, CRITICAL);
	$ro = matches($p, READ_ONLY);
	$man = matches($p, MANAGED);
	$range = ($s['min'] !== null || $s['max'] !== null) ? fmt($s['min']) . ' … ' . fmt($s['max']) : '';
?>
<?php $lbl = $help[$p]['label'] ?? ''; $vt = value_text($p, $s, $help); ?>
<tr id="<?= h($p) ?>" class="<?= $d ? 'nd' : '' ?>" data-p="<?= h(strtolower($p . ' ' . $lbl)) ?>" data-nd="<?= $d ? 1 : 0 ?>" title="<?= h(tip($p, $s, $help)) ?>">
	<td class="path"><?= h(substr($p, 10)) ?><?php if ($lbl !== ''): ?><div class="lbl"><?= h($lbl) ?></div><?php endif; ?><?php
		if ($crit) echo '<span class="tag crit">critical</span>';
		if ($man) echo '<span class="tag man">venus-managed</span>';
		if ($ro) echo '<span class="tag ro">read-only</span>'; ?></td>
	<td><?php if ($ro): ?><code><?= h(fmt($s['value'])) ?></code><?php else: ?>
		<form method="post">
			<input type="hidden" name="path" value="<?= h($p) ?>">
			<input type="hidden" name="q" class="fq"><input type="hidden" name="nd" class="fnd">
			<input name="value" value="<?= h(fmt($s['value'])) ?>" <?= $s['type'] !== 's' ? 'inputmode="decimal"' : '' ?>>
			<?php if ($vt !== ''): ?><span class="vt"><?= h($vt) ?></span><?php endif; ?>
			<button type="submit">set</button>
			<?php if ($d && $s['default'] !== null): ?><button type="submit" class="def" name="value" value="<?= h(fmt($s['default'])) ?>" title="set to default">default</button><?php endif; ?>
			<?php if ($crit): ?><label class="cf"><input type="checkbox" name="confirm" value="1"> confirm</label><?php endif; ?>
		</form>
	<?php endif; ?></td>
	<td class="meta">default <?= h(fmt($s['default'])) ?><?= $range !== '' ? ' · ' . h($range) : '' ?> · <?= h($s['type']) ?></td>
</tr>
<?php endforeach; if ($cur !== null) echo "</table>\n"; ?>
</main>
<script>
const q = document.getElementById('q'), nd = document.getElementById('nd'), count = document.getElementById('count');
function apply() {
	const t = q.value.trim().toLowerCase(), only = nd.checked;
	let n = 0;
	document.querySelectorAll('table').forEach(tb => {
		let vis = 0;
		tb.querySelectorAll('tr').forEach(tr => {
			const show = (!t || tr.dataset.p.includes(t)) && (!only || tr.dataset.nd === '1');
			tr.style.display = show ? '' : 'none';
			vis += show;
		});
		tb.style.display = vis ? '' : 'none';
		tb.previousElementSibling.style.display = vis ? '' : 'none';
		n += vis;
	});
	count.textContent = n + ' shown';
	document.querySelectorAll('.fq').forEach(i => i.value = q.value);
	document.querySelectorAll('.fnd').forEach(i => i.value = only ? '1' : '');
}
q.addEventListener('input', apply); nd.addEventListener('change', apply); apply();
document.querySelectorAll('form').forEach(f => f.addEventListener('submit', e => {
	const cf = f.querySelector('input[name=confirm]');
	if (cf && !cf.checked) { e.preventDefault(); alert('critical setting: tick "confirm" first'); }
}));
</script>
</body>
</html>
