<?php
// bm_params.php - batmonitor parameter editor (stage 1 of the rule editor, user 2026-10-27)
//   read: everyone, no password (parameters, active version, history, source / mode matrix read-only)
//   write: only VPN clients (10.8.0.0/24, user: "nur vpn devices duerfen schreiben"); a save is a new row in
//   wagodb.bm_param_version. The Pi pulls the newest row itself (host/bm_params_pull.py, cron), checks it against the
//   limits of its own bm_logic.py and writes /data/batmonitor/env - nothing is pushed into Venus from here.
// Definitions: always the newest wagodb.bm_param_schema row. The Pi writes it (host/bm_params_pull.py) from the
// DEPLOYED bm_logic.py and the running version, so after a batmonitor update the form and the matrices follow by
// themselves within one pull run (5 min); overrides of parameters the new version no longer has show as obsolete.
date_default_timezone_set('Europe/Berlin');
require_once __DIR__ . '/config.php';
header('Cache-Control: no-store');

function h($s) { return htmlspecialchars((string)$s, ENT_QUOTES); }

function is_vpn_client(): bool {
    // only the direct peer counts (no X-Forwarded-For): VPN clients reach heissa as 10.8.0.x
    $ip = $_SERVER['REMOTE_ADDR'] ?? '';
    return (bool)preg_match('/^10\.8\.0\.\d{1,3}$/', $ip);
}

function num_str($v): string {
    return rtrim(rtrim(sprintf('%.6F', (float)$v), '0'), '.');
}

$can_write = is_vpn_client();
$msg = $err = null;
if ($can_write) {
    session_start();
    if (empty($_SESSION['bm_csrf'])) $_SESSION['bm_csrf'] = bin2hex(random_bytes(16));
}

try {
    $pdo = new PDO("mysql:host=$db_host;dbname=$db_name;charset=utf8mb4", $db_user, $db_pass,
                   [PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION, PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC]);
} catch (Exception $e) {
    http_response_code(500); exit('database: ' . h($e->getMessage()));
}

$schema = $pdo->query("SELECT id, created_at, bm_version, def FROM bm_param_schema ORDER BY id DESC LIMIT 1")->fetch();
$def = $schema ? json_decode($schema['def'], true) : null;
if (!$def) { http_response_code(500); exit('no parameter schema from the Pi yet (wagodb.bm_param_schema)'); }
$params = [];
foreach ($def['params'] as $p) $params[$p['name']] = $p;

// ---- save (VPN only) -----------------------------------------------------------------------------
if ($_SERVER['REQUEST_METHOD'] === 'POST') {
    if (!$can_write) {
        http_response_code(403); exit('write access only from the VPN');
    }
    if (!hash_equals($_SESSION['bm_csrf'], $_POST['csrf'] ?? '')) {
        $err = 'form expired, reload the page';
    } else {
        $new = [];
        foreach ($params as $name => $p) {
            $raw = trim(str_replace(',', '.', $_POST['p'][$name] ?? ''));
            if ($raw === '') continue;                                   // empty = default
            if (!is_numeric($raw)) { $err = "$name: not a number"; break; }
            $v = 0 + $raw;
            if ($v < $p['min'] || $v > $p['max']) { $err = "$name: $raw outside {$p['min']} .. {$p['max']}"; break; }
            if ((float)$v == (float)$p['default']) continue;              // equal to the default: no override
            $new[$name] = $v;
        }
        foreach (array_keys($_POST['p'] ?? []) as $name)
            if (!isset($params[$name])) { $err = 'unknown parameter ' . $name; break; }
        $note = mb_substr(trim($_POST['note'] ?? ''), 0, 255);
        if (!$err) {
            ksort($new);
            $json = json_encode((object)$new, JSON_UNESCAPED_SLASHES);
            $last = $pdo->query("SELECT params FROM bm_param_version ORDER BY id DESC LIMIT 1")->fetch();
            if ($last && $last['params'] === $json) {
                $msg = 'no change, nothing saved';
            } else {
                $st = $pdo->prepare("INSERT INTO bm_param_version (author, note, params, sha256) VALUES (?, ?, ?, ?)");
                $st->execute([$_SERVER['REMOTE_ADDR'], $note, $json, hash('sha256', $json)]);
                $msg = 'saved as version ' . $pdo->lastInsertId() . ', the Pi applies it within 5 min';
            }
        }
    }
}

$hist = $pdo->query("SELECT * FROM bm_param_version ORDER BY id DESC LIMIT 15")->fetchAll();
$cur = $hist ? json_decode($hist[0]['params'], true) : [];
$live = $pdo->query("SELECT ts, bm_l1_rule, bm_l2_rule, bm_l3_rule FROM pv_victron ORDER BY ts DESC LIMIT 1")->fetch();

function diff_text(array $a, array $b): string {
    $out = [];
    foreach (array_unique(array_merge(array_keys($a), array_keys($b))) as $k) {
        $x = $a[$k] ?? null; $y = $b[$k] ?? null;
        if ($x === $y) continue;
        $out[] = $k . ' ' . ($x === null ? 'default' : num_str($x)) . ' → ' . ($y === null ? 'default' : num_str($y));
    }
    return $out ? implode(', ', $out) : '-';
}
?><!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>batmonitor parameters</title>
<style>
:root { --bg:#0f172a; --panel:#111c33; --fg:#e2e8f0; --muted:#94a3b8; --line:#1e293b; --acc:#f59e0b; --ok:#22c55e; --bad:#ef4444; }
* { box-sizing:border-box; }
body { margin:0; padding:16px; background:var(--bg); color:var(--fg); font:14px/1.45 system-ui, sans-serif; }
h1 { font-size:20px; margin:0 0 4px; } h2 { font-size:16px; margin:24px 0 8px; }
.muted { color:var(--muted); } .ok { color:var(--ok); } .bad { color:var(--bad); } .acc { color:var(--acc); }
.box { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:12px; margin:12px 0; overflow-x:auto; }
table { border-collapse:collapse; width:100%; } th, td { text-align:left; padding:5px 8px; border-bottom:1px solid var(--line); vertical-align:top; }
th { color:var(--muted); font-weight:600; } td.n { text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }
input[type=text] { width:7em; background:#0b1220; color:var(--fg); border:1px solid #334155; border-radius:4px; padding:3px 6px; text-align:right; }
input.wide { width:100%; text-align:left; } tr.set td { background:#1a2540; }
button { background:var(--acc); color:#111; border:0; border-radius:6px; padding:7px 16px; font-weight:600; cursor:pointer; }
code { color:var(--acc); }
</style></head><body>
<h1>batmonitor parameters</h1>
<div class="muted">schema <?= (int)$schema['id'] ?> of batmonitor <?= h($schema['bm_version']) ?>, published by the Pi <?= h($schema['created_at']) ?> · the Pi pulls the newest version every 5 min and checks it against its own limits ·
<?= $can_write ? '<span class="ok">write access (VPN ' . h($_SERVER['REMOTE_ADDR']) . ')</span>' : 'read only - writing only from the VPN' ?></div>
<?php if ($live): ?><div class="muted">live rules <?= h($live['ts']) ?>: L1 <code><?= h($live['bm_l1_rule']) ?></code> · L2 <code><?= h($live['bm_l2_rule']) ?></code> · L3 <code><?= h($live['bm_l3_rule']) ?></code></div><?php endif ?>
<?php if ($msg): ?><div class="box ok"><?= h($msg) ?></div><?php endif ?>
<?php if ($err): ?><div class="box bad"><?= h($err) ?></div><?php endif ?>

<?php if ($hist): $a = $hist[0]; ?>
<div class="box">active version <b><?= (int)$a['id'] ?></b> from <?= h($a['created_at']) ?> (<?= h($a['author']) ?>) <?= h($a['note']) ?> ·
<?= $a['applied_at'] ? '<span class="ok">on the Pi since ' . h($a['applied_at']) . ' (' . h($a['applied_note']) . ')</span>'
    : ($a['applied_note'] ? '<span class="bad">' . h($a['applied_note']) . '</span>' : '<span class="acc">not yet applied by the Pi</span>') ?></div>
<?php endif ?>

<?php $obsolete = array_diff_key($cur, $params); if ($obsolete): ?>
<div class="box bad">obsolete in the active version (batmonitor <?= h($schema['bm_version']) ?> has no such parameter, the Pi ignores it):
<?php foreach ($obsolete as $k => $v): ?><code><?= h($k) ?></code> = <?= num_str($v) ?> <?php endforeach ?>· the next save drops it</div>
<?php endif ?>

<form method="post">
<?php if ($can_write): ?><input type="hidden" name="csrf" value="<?= h($_SESSION['bm_csrf']) ?>"><?php endif ?>
<div class="box"><table>
<tr><th>parameter</th><th class="n">default</th><th class="n">min</th><th class="n">max</th><th>unit</th><th class="n">value</th><th>meaning</th></tr>
<?php foreach ($params as $name => $p): $set = array_key_exists($name, $cur); ?>
<tr<?= $set ? ' class="set"' : '' ?>><td><code><?= h($name) ?></code></td>
<td class="n"><?= num_str($p['default']) ?></td><td class="n"><?= num_str($p['min']) ?></td><td class="n"><?= num_str($p['max']) ?></td><td><?= h($p['unit']) ?></td>
<td class="n"><?php if ($can_write): ?><input type="text" name="p[<?= h($name) ?>]" value="<?= $set ? num_str($cur[$name]) : '' ?>" placeholder="<?= num_str($p['default']) ?>"><?php else: ?><?= $set ? '<b class="acc">' . num_str($cur[$name]) . '</b>' : '<span class="muted">default</span>' ?><?php endif ?></td>
<td class="muted"><?= h($p['help']) ?></td></tr>
<?php endforeach ?>
</table></div>
<?php if ($can_write): ?>
<div class="box"><label>note <input class="wide" type="text" name="note" maxlength="255" placeholder="why this change"></label><br><br>
<button type="submit">save as new version</button> <span class="muted">empty field = default; values outside min .. max are refused</span></div>
<?php endif ?>
</form>

<h2>history</h2>
<div class="box"><table><tr><th>id</th><th>created</th><th>by</th><th>change to the version before</th><th>note</th><th>Pi</th></tr>
<?php foreach ($hist as $i => $r):
    $prev = isset($hist[$i + 1]) ? json_decode($hist[$i + 1]['params'], true) : []; ?>
<tr><td class="n"><?= (int)$r['id'] ?></td><td><?= h($r['created_at']) ?></td><td><?= h($r['author']) ?></td>
<td><?= h(diff_text($prev, json_decode($r['params'], true))) ?></td><td class="muted"><?= h($r['note']) ?></td>
<td><?= $r['applied_at'] ? h($r['applied_at']) : '<span class="muted">' . h($r['applied_note'] ?? '-') . '</span>' ?></td></tr>
<?php endforeach ?></table></div>

<h2>source matrix (S3, read only)</h2>
<div class="box"><table><tr><th>source</th><th>b1_signed</th><th>trickle</th><th class="n">idle W</th><th>prop_hold</th></tr>
<?php foreach ($def['src_rule'] as $r): ?><tr><td><code><?= h($r['src']) ?></code></td><td><?= (int)$r['b1_signed'] ?></td><td><?= (int)$r['trickle'] ?></td><td class="n"><?= (int)$r['idle_w'] ?></td><td><?= (int)$r['prop_hold'] ?></td></tr><?php endforeach ?>
</table></div>
<h2>mode matrix (S1, read only)</h2>
<div class="box"><table><tr><th>mode</th><th>action</th><th>capped</th><th>rule text</th></tr>
<?php foreach ($def['mode_rule'] as $r): ?><tr><td><code><?= h($r['mode']) ?></code></td><td><?= h($r['action']) ?></td><td><?= (int)$r['capped'] ?></td><td><?= h($r['why']) ?></td></tr><?php endforeach ?>
</table></div>
</body></html>
