<?php
// bm_dmn.php - live editor of the batmonitor decision tables (stage 2 of the rule editor, user 2026-11-07)
//   the tables are bm_decisions.dmn (standard DMN 1.3), edited here with dmn-js (bpmn.io, the table editor of the
//   Camunda Modeler; vendor/dmn-js-18.0.2, install with web/install_dmn_js.sh).
//   read: everyone (viewer). write: only VPN clients (10.8.0.0/24), like bm_params.php; a save is a new row in
//   wagodb.bm_dmn_version (source 'editor'). The Pi (host/bm_params_pull.py, cron every minute) checks it with its
//   own DMN reader and bm_selftest.py, puts it live and restarts batmonitor, rolls back if batmonitor does not run
//   with it, and writes the result into applied_note; this page polls that. The newest row is what opens here;
//   the Pi adds the running file as a row (source 'pi') after a repo deploy or a refusal.
//   ?xml[=id]   the DMN text of the newest row (or of row id)     ?status=id   JSON state of row id
date_default_timezone_set('Europe/Berlin');
require_once __DIR__ . '/config.php';
header('Cache-Control: no-store');
const DMNJS = 'vendor/dmn-js-18.0.2';
const DMN_NS = 'https://www.omg.org/spec/DMN/20191111/MODEL/';
const DMN_MAX = 2097152;

function h($s) { return htmlspecialchars((string)$s, ENT_QUOTES); }
function is_vpn_client(): bool {
    return (bool)preg_match('/^10\.8\.0\.\d{1,3}$/', $_SERVER['REMOTE_ADDR'] ?? '');   // only the direct peer
}
function json_out($a, int $code = 200) {
    http_response_code($code); header('Content-Type: application/json'); echo json_encode($a); exit;
}

$can_write = is_vpn_client();
if ($can_write) {
    session_start();
    if (empty($_SESSION['bm_dmn_csrf'])) $_SESSION['bm_dmn_csrf'] = bin2hex(random_bytes(16));
}
try {
    $pdo = new PDO("mysql:host=$db_host;dbname=$db_name;charset=utf8mb4", $db_user, $db_pass,
                   [PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION, PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC]);
} catch (Exception $e) {
    http_response_code(500); exit('database: ' . h($e->getMessage()));
}

if (isset($_GET['xml'])) {
    $id = (int)$_GET['xml'];
    $st = $id ? $pdo->prepare("SELECT dmn FROM bm_dmn_version WHERE id = ?") : $pdo->prepare("SELECT dmn FROM bm_dmn_version ORDER BY id DESC LIMIT 1");
    $st->execute($id ? [$id] : []);
    $r = $st->fetch();
    if (!$r) { http_response_code(404); exit('no DMN version'); }
    header('Content-Type: application/xml; charset=utf-8');
    echo $r['dmn']; exit;
}
if (isset($_GET['status'])) {
    $st = $pdo->prepare("SELECT id, source, applied_at, applied_note FROM bm_dmn_version WHERE id = ?");
    $st->execute([(int)$_GET['status']]);
    $newest = $pdo->query("SELECT MAX(id) AS id FROM bm_dmn_version")->fetch();
    json_out(['row' => $st->fetch() ?: null, 'newest' => (int)$newest['id']]);
}

// ---- save (VPN only): JSON {csrf, note, dmn} -----------------------------------------------------------------
if ($_SERVER['REQUEST_METHOD'] === 'POST') {
    if (!$can_write) json_out(['error' => 'write access only from the VPN'], 403);
    $in = json_decode(file_get_contents('php://input'), true);
    if (!is_array($in) || !hash_equals($_SESSION['bm_dmn_csrf'], (string)($in['csrf'] ?? ''))) json_out(['error' => 'page expired, reload'], 400);
    $dmn = (string)($in['dmn'] ?? '');
    if (strlen($dmn) > DMN_MAX) json_out(['error' => 'larger than 2 MB'], 400);
    $doc = new DOMDocument();
    if (!@$doc->loadXML($dmn, LIBXML_NONET) || $doc->documentElement->localName !== 'definitions'
            || $doc->documentElement->namespaceURI !== DMN_NS) {
        json_out(['error' => 'not a DMN 1.3 definitions document'], 400);
    }
    $sha = hash('sha256', $dmn);
    $last = $pdo->query("SELECT sha256 FROM bm_dmn_version ORDER BY id DESC LIMIT 1")->fetch();
    if ($last && $last['sha256'] === $sha) json_out(['error' => 'no change against the newest version'], 400);
    $st = $pdo->prepare("INSERT INTO bm_dmn_version (source, author, note, dmn, sha256) VALUES ('editor', ?, ?, ?, ?)");
    $st->execute([$_SERVER['REMOTE_ADDR'], mb_substr(trim((string)($in['note'] ?? '')), 0, 255), $dmn, $sha]);
    json_out(['ok' => true, 'id' => (int)$pdo->lastInsertId()]);
}

$hist = $pdo->query("SELECT id, created_at, source, author, note, bm_version, sha256, applied_at, applied_note
                     FROM bm_dmn_version ORDER BY id DESC LIMIT 15")->fetchAll();
$live = $pdo->query("SELECT ts, bm_l1_rule, bm_l2_rule, bm_l3_rule FROM pv_victron ORDER BY ts DESC LIMIT 1")->fetch();
$bundle = $can_write ? 'dmn-modeler.production.min.js' : 'dmn-viewer.production.min.js';
?><!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>batmonitor decision tables (DMN)</title>
<link rel="stylesheet" href="<?= DMNJS ?>/assets/dmn-js-shared.css">
<link rel="stylesheet" href="<?= DMNJS ?>/assets/dmn-js-drd.css">
<link rel="stylesheet" href="<?= DMNJS ?>/assets/dmn-js-decision-table.css">
<link rel="stylesheet" href="<?= DMNJS ?>/assets/dmn-js-decision-table-controls.css">
<link rel="stylesheet" href="<?= DMNJS ?>/assets/dmn-js-literal-expression.css">
<link rel="stylesheet" href="<?= DMNJS ?>/assets/dmn-font/css/dmn.css">
<style>
:root { --bg:#0f172a; --panel:#111c33; --fg:#e2e8f0; --muted:#94a3b8; --line:#1e293b; --acc:#f59e0b; --ok:#22c55e; --bad:#ef4444; }
body { margin:0; padding:16px; background:var(--bg); color:var(--fg); font:14px/1.45 system-ui, sans-serif; }
h1 { font-size:20px; margin:0 0 4px; } h2 { font-size:16px; margin:20px 0 8px; }
a { color:var(--acc); } .muted { color:var(--muted); } .ok { color:var(--ok); } .bad { color:var(--bad); } .acc { color:var(--acc); }
.box { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:12px; margin:12px 0; overflow-x:auto; }
.bar { display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
select, input[type=text] { background:#0b1220; color:var(--fg); border:1px solid #334155; border-radius:4px; padding:5px 8px; }
input.note { flex:1; min-width:16em; }
button { background:var(--acc); color:#111; border:0; border-radius:6px; padding:7px 16px; font-weight:600; cursor:pointer; }
button:disabled { opacity:.5; cursor:default; }
#canvas { background:#fff; color:#111; border-radius:8px; min-height:420px; padding:8px; overflow:auto; }
table.h { border-collapse:collapse; width:100%; } table.h th, table.h td { text-align:left; padding:5px 8px; border-bottom:1px solid var(--line); vertical-align:top; }
table.h th { color:var(--muted); font-weight:600; }
code { color:var(--acc); }
</style></head><body>
<h1>batmonitor decision tables</h1>
<div class="muted">bm_decisions.dmn, standard DMN 1.3, first matching rule wins · input entries <code>true</code> / <code>false</code> / <code>-</code>,
the column header expression is its meaning (e.g. <code>soc &lt; SOC_MIN</code>) · the Pi checks every saved version (reader + self-test), puts it live within a minute and rolls back if batmonitor does not run with it ·
<?= $can_write ? '<span class="ok">write access (VPN ' . h($_SERVER['REMOTE_ADDR']) . ')</span>' : 'read only - writing only from the VPN' ?>
· <a href="bm_params.php">parameters</a> · <a href="bm_flow.php">decision flow</a> · <a href="?xml">XML</a></div>
<?php if ($live): ?><div class="muted">live rules <?= h($live['ts']) ?>: L1 <code><?= h($live['bm_l1_rule']) ?></code> · L2 <code><?= h($live['bm_l2_rule']) ?></code> · L3 <code><?= h($live['bm_l3_rule']) ?></code></div><?php endif ?>

<div class="box bar">
  <label>table <select id="view"></select></label>
  <?php if ($can_write): ?>
  <input type="text" id="note" class="note" placeholder="what was changed and why (stored with the version)">
  <button id="save">save → live</button>
  <?php endif ?>
  <span id="msg" class="muted"></span>
</div>
<div id="canvas"></div>

<h2>versions</h2>
<div class="box"><table class="h">
<tr><th>id</th><th>time</th><th>source</th><th>by</th><th>note</th><th>on the Pi</th><th></th></tr>
<?php foreach ($hist as $r): ?>
<tr><td><?= (int)$r['id'] ?></td><td><?= h($r['created_at']) ?></td><td><?= h($r['source']) ?></td><td><?= h($r['author']) ?></td>
<td><?= h($r['note']) ?></td>
<td class="<?= $r['applied_at'] ? 'ok' : ($r['applied_note'] ? 'bad' : 'acc') ?>"><?= $r['applied_note'] !== null ? h($r['applied_note']) : 'waiting for the Pi' ?></td>
<td><a href="?xml=<?= (int)$r['id'] ?>">XML</a></td></tr>
<?php endforeach ?>
</table></div>

<script src="<?= DMNJS . '/' . $bundle ?>"></script>
<script>
const CSRF = <?= json_encode($can_write ? $_SESSION['bm_dmn_csrf'] : '') ?>;
const msg = (t, cls) => { const m = document.getElementById('msg'); m.textContent = t; m.className = cls || 'muted'; };
const dmn = new DmnJS({ container: '#canvas', common: { keyboard: { bindTo: window } } });
const sel = document.getElementById('view');
let loaded = '';

function fillViews(keep) {
  const views = dmn.getViews().filter(v => v.type === 'decisionTable');
  sel.innerHTML = '';
  for (const v of views) {
    const o = document.createElement('option');
    o.value = v.element.id; o.textContent = v.element.name || v.element.id;
    sel.appendChild(o);
  }
  const want = views.find(v => v.element.id === keep) || views[0];
  if (want) { sel.value = want.element.id; dmn.open(want); }
}
sel.addEventListener('change', () => {
  const v = dmn.getViews().find(v => v.element.id === sel.value);
  if (v) dmn.open(v);
});

async function load(keep) {
  const r = await fetch('?xml', { cache: 'no-store' });
  loaded = await r.text();
  await dmn.importXML(loaded);
  fillViews(keep);
}

async function poll(id) {
  for (let i = 0; i < 60; i++) {
    await new Promise(res => setTimeout(res, 5000));
    const s = await (await fetch('?status=' + id, { cache: 'no-store' })).json();
    const note = s.row && s.row.applied_note;
    if (note === null || note === undefined) { msg('version ' + id + ' saved, waiting for the Pi … ' + (i + 1) * 5 + ' s', 'acc'); continue; }
    msg('version ' + id + ': ' + note, note.startsWith('rejected') ? 'bad' : 'ok');
    if (note.startsWith('rejected')) msg('version ' + id + ': ' + note + ' - the Pi keeps the old tables; reload to see them', 'bad');
    setTimeout(() => location.reload(), note.startsWith('rejected') ? 15000 : 4000);
    return;
  }
  msg('version ' + id + ': no answer from the Pi within 5 min (cron / VPN?)', 'bad');
}

const btn = document.getElementById('save');
if (btn) btn.addEventListener('click', async () => {
  const { xml } = await dmn.saveXML({ format: true });
  if (xml === loaded) { msg('no change', 'muted'); return; }
  btn.disabled = true;
  try {
    const r = await fetch(location.pathname, { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ csrf: CSRF, note: document.getElementById('note').value, dmn: xml }) });
    const j = await r.json();
    if (!j.ok) { msg(j.error || 'save failed', 'bad'); btn.disabled = false; return; }
    loaded = xml;
    poll(j.id);
  } catch (e) { msg('save failed: ' + e, 'bad'); btn.disabled = false; }
});

load().catch(e => msg('cannot open the DMN: ' + e, 'bad'));
</script>
</body></html>
