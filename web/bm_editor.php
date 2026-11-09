<?php
// bm_editor.php - the one batmonitor editor (user 2026-11-09, replaces bm_params.php and bm_dmn.php): parameters and
// decision tables in tabs, Bootstrap dark mode, everything through bm_api.php.
//   read: everyone. write: bm_access.php, enforced by bm_api.php; here it only selects the form and the DMN bundle.
//   vendor/: bootstrap and swagger with web/install_web_vendor.sh, dmn-js with web/install_dmn_js.sh
header('Cache-Control: no-store');
const DMNJS = 'vendor/dmn-js-18.0.2';
const BOOTSTRAP = 'vendor/bootstrap-5.3.3';
require_once __DIR__ . '/bm_access.php';
$vpn = bm_can_write();
$bundle = $vpn ? 'dmn-modeler.production.min.js' : 'dmn-viewer.production.min.js';
?><!DOCTYPE html>
<html lang="en" data-bs-theme="dark"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>batmonitor editor</title>
<link rel="stylesheet" href="<?= BOOTSTRAP ?>/bootstrap.min.css">
<link rel="stylesheet" href="<?= DMNJS ?>/assets/dmn-js-shared.css">
<link rel="stylesheet" href="<?= DMNJS ?>/assets/dmn-js-drd.css">
<link rel="stylesheet" href="<?= DMNJS ?>/assets/dmn-js-decision-table.css">
<link rel="stylesheet" href="<?= DMNJS ?>/assets/dmn-js-decision-table-controls.css">
<link rel="stylesheet" href="<?= DMNJS ?>/assets/dmn-js-literal-expression.css">
<link rel="stylesheet" href="<?= DMNJS ?>/assets/dmn-font/css/dmn.css">
<style>
.value-input { width: 7.5em; margin-left: auto; }
.value-input.changed { border-color: var(--bs-warning); box-shadow: 0 0 0 .15rem rgba(255,193,7,.25); }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
#canvas { min-height: 420px; padding: 8px; overflow: auto; border-radius: var(--bs-border-radius); background: #fff; color: #111; }
.rule { font-size: .85em; }
</style></head><body>

<nav class="navbar bg-body-tertiary border-bottom mb-3">
  <div class="container-fluid gap-2">
    <span class="navbar-brand mb-0 h1">batmonitor editor <span id="bm-version" class="badge text-bg-secondary"></span></span>
    <div class="d-flex flex-wrap align-items-center gap-3">
      <?= $vpn ? '<span class="badge text-bg-success">write access</span>' : '<span class="badge text-bg-secondary">read only from this address</span>' ?>
      <a class="link-light" href="bm_flow.php">decision flow</a>
      <a class="link-light" href="bm_swagger.php">API</a>
    </div>
  </div>
</nav>

<main class="container-fluid pb-4">
  <div class="text-secondary small mb-3">live rules <span id="live-ts"></span>:
    <code class="rule">L1</code> <code class="rule" id="rule-L1">–</code> ·
    <code class="rule">L2</code> <code class="rule" id="rule-L2">–</code> ·
    <code class="rule">L3</code> <code class="rule" id="rule-L3">–</code></div>
  <div id="error" class="alert alert-danger d-none"></div>

  <ul class="nav nav-tabs mb-3" role="tablist">
    <li class="nav-item" role="presentation"><button class="nav-link active" data-bs-toggle="tab" data-bs-target="#tab-params" type="button" role="tab">Parameters</button></li>
    <li class="nav-item" role="presentation"><button class="nav-link" data-bs-toggle="tab" data-bs-target="#tab-dmn" type="button" role="tab">Decision tables</button></li>
  </ul>

  <div class="tab-content">
    <!-- ---- parameters ---- -->
    <section class="tab-pane fade show active" id="tab-params" role="tabpanel">
      <div id="param-obsolete" class="alert alert-warning d-none"></div>
      <div class="card mb-3"><div class="table-responsive"><table class="table table-sm table-hover align-middle mb-0">
        <thead><tr><th>parameter</th><th class="num">value</th><th class="num">default</th><th class="num">min</th><th class="num">max</th><th>unit</th><th>meaning</th></tr></thead>
        <tbody id="param-rows"></tbody>
      </table></div></div>
      <?php if ($vpn): ?>
      <div class="card card-body mb-4">
        <div class="row g-2 align-items-center">
          <div class="col-md"><input id="param-note" class="form-control" maxlength="255" placeholder="what was changed and why (stored with the version)"></div>
          <div class="col-md-auto"><button id="param-save" class="btn btn-warning" disabled>Save → live</button></div>
        </div>
        <div class="small mt-2 text-secondary" id="param-msg">Empty field = default. The Pi checks a saved version and restarts batmonitor within a minute.</div>
      </div>
      <?php endif ?>
      <h2 class="h5">History</h2>
      <div class="card mb-4"><div class="table-responsive"><table class="table table-sm align-middle mb-0">
        <thead><tr><th class="num">id</th><th>created</th><th>by</th><th>change to the version before</th><th>note</th><th>on the Pi</th></tr></thead>
        <tbody id="param-history"></tbody>
      </table></div></div>
      <div class="row g-4">
        <div class="col-lg-6"><h2 class="h5">Source matrix (S3, read only)</h2>
          <div class="card"><div class="table-responsive"><table class="table table-sm mb-0">
            <thead><tr><th>source</th><th>b1_signed</th><th>trickle</th><th class="num">idle W</th><th>prop_hold</th></tr></thead><tbody id="src-rows"></tbody>
          </table></div></div></div>
        <div class="col-lg-6"><h2 class="h5">Mode matrix (S1, read only)</h2>
          <div class="card"><div class="table-responsive"><table class="table table-sm mb-0">
            <thead><tr><th>mode</th><th>action</th><th>capped</th><th>rule text</th></tr></thead><tbody id="mode-rows"></tbody>
          </table></div></div></div>
      </div>
    </section>

    <!-- ---- decision tables ---- -->
    <section class="tab-pane fade" id="tab-dmn" role="tabpanel">
      <div class="card card-body mb-3">
        <div class="row g-2 align-items-center">
          <div class="col-md-auto"><select id="dmn-view" class="form-select" aria-label="decision table"></select></div>
          <?php if ($vpn): ?>
          <div class="col-md"><input id="dmn-note" class="form-control" maxlength="255" placeholder="what was changed and why (stored with the version)"></div>
          <div class="col-md-auto"><button id="dmn-save" class="btn btn-warning">Save → live</button></div>
          <?php endif ?>
        </div>
        <div class="small mt-2 text-secondary" id="dmn-msg">bm_decisions.dmn, DMN 1.3, the first matching rule wins. Input entries true / false / -. The Pi rolls back if batmonitor does not run with a saved version.</div>
      </div>
      <div id="canvas" class="mb-4" data-bs-theme="light"></div>
      <h2 class="h5">Versions</h2>
      <div class="card"><div class="table-responsive"><table class="table table-sm align-middle mb-0">
        <thead><tr><th class="num">id</th><th>time</th><th>source</th><th>by</th><th>note</th><th>on the Pi</th><th></th></tr></thead>
        <tbody id="dmn-history"></tbody>
      </table></div></div>
    </section>
  </div>
</main>

<script src="<?= BOOTSTRAP ?>/bootstrap.bundle.min.js"></script>
<script src="<?= DMNJS . '/' . $bundle ?>"></script>
<script>
const API = 'bm_api.php', VPN = <?= json_encode($vpn) ?>;
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const sleep = ms => new Promise(r => setTimeout(r, ms));

async function api(path, options) {
  const r = await fetch(API + path, Object.assign({ cache: 'no-store' }, options));
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || ('HTTP ' + r.status));
  return { status: r.status, data };
}
const post = (path, body) => api(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
function showError(e) { const b = $('error'); b.textContent = String(e.message || e); b.classList.remove('d-none'); }
function msg(el, text, kind) { el.textContent = text; el.className = 'small mt-2 text-' + (kind || 'secondary'); }
function stateCell(v) {
  const cls = { applied: 'success', pending: 'warning', rejected: 'danger' }[v.state];
  const label = v.state === 'pending' ? 'waiting for the Pi' : (v.applied_note || v.state);
  return `<span class="badge text-bg-${cls} text-wrap text-start">${esc(label)}</span>` + (v.applied_at ? ` <span class="small text-secondary">${esc(v.applied_at)}</span>` : '');
}
// waits for the answer of the Pi to version id, cron runs every minute
async function waitForPi(path, id, el, done) {
  for (let i = 1; i <= 60; i++) {
    await sleep(5000);
    const { data } = await api(path + '/versions/' + id);
    if (data.state === 'pending') { msg(el, `Version ${id} saved, waiting for the Pi … ${i * 5} s`, 'warning'); continue; }
    if (data.state === 'rejected') msg(el, `Version ${id}: ${data.applied_note} - the Pi keeps the old values`, 'danger');
    else msg(el, `Version ${id}: ${data.applied_note}`, 'success');
    return done();
  }
  msg(el, `Version ${id}: no answer from the Pi within 5 min (cron / VPN?)`, 'danger');
}

// ---- status line ----------------------------------------------------------------------------------------------------
async function loadStatus() {
  const { data } = await api('/status');
  $('bm-version').textContent = data.bm_version;
  if (data.live) {
    $('live-ts').textContent = data.live.ts;
    for (const p of ['L1', 'L2', 'L3']) $('rule-' + p).textContent = data.live.rules[p];
  }
}

// ---- parameters -----------------------------------------------------------------------------------------------------
let paramValues = [];
function diffText(prev, cur) {
  const out = [];
  for (const k of new Set([...Object.keys(prev), ...Object.keys(cur)])) {
    if (prev[k] !== cur[k]) out.push(`${k} ${prev[k] ?? 'default'} → ${cur[k] ?? 'default'}`);
  }
  return out.join(', ') || '-';
}
function paramChanges() {
  const set = {}, reset = [];
  let bad = null;
  for (const inp of document.querySelectorAll('.value-input')) {
    const v = paramValues.find(x => x.name === inp.dataset.name);
    const raw = inp.value.trim().replace(',', '.');
    const was = v.overridden ? String(v.value) : '';
    inp.classList.toggle('changed', raw !== was);
    if (raw === was) continue;
    if (raw === '') { reset.push(v.name); continue; }
    const n = Number(raw);
    if (!Number.isFinite(n) || n < v.min || n > v.max) bad = bad || `${v.name}: ${raw} outside ${v.min} .. ${v.max}`;
    set[v.name] = n;
  }
  return { set, reset, bad };
}
function refreshSave() {
  if (!VPN) return;
  const { set, reset, bad } = paramChanges(), n = Object.keys(set).length + reset.length;
  $('param-save').disabled = !n || !!bad;
  $('param-save').textContent = n ? `Save ${n} change${n > 1 ? 's' : ''} → live` : 'Save → live';
  if (bad) msg($('param-msg'), bad, 'danger');
}
async function loadParams() {
  const [{ data: p }, { data: h }, { data: s }] = await Promise.all([api('/params'), api('/params/versions'), api('/params/schema')]);
  paramValues = p.values;
  $('param-rows').innerHTML = p.values.map(v => {
    const val = VPN ? `<input class="form-control form-control-sm text-end value-input" inputmode="decimal" data-name="${esc(v.name)}" aria-label="${esc(v.name)}" value="${v.overridden ? esc(v.value) : ''}" placeholder="${esc(v.default)}">`
                    : (v.overridden ? `<strong class="text-warning">${esc(v.value)}</strong>` : '<span class="text-secondary">default</span>');
    return `<tr${v.overridden ? ' class="table-active"' : ''}><td><code>${esc(v.name)}</code></td><td class="num">${val}</td><td class="num">${esc(v.default)}</td><td class="num">${esc(v.min)}</td><td class="num">${esc(v.max)}</td><td>${esc(v.unit)}</td><td class="small text-secondary">${esc(v.help)}</td></tr>`;
  }).join('');
  const known = new Set(p.values.map(v => v.name)), old = Object.keys(p.version.params).filter(k => !known.has(k));
  $('param-obsolete').classList.toggle('d-none', !old.length);
  $('param-obsolete').textContent = old.length ? `Obsolete in the newest version, ignored by batmonitor ${p.bm_version}: ${old.join(', ')}. The next save drops them.` : '';
  $('param-history').innerHTML = h.versions.map((v, i) =>
    `<tr><td class="num">${v.id}</td><td class="text-nowrap">${esc(v.created_at)}</td><td>${esc(v.author)}</td><td class="small">${esc(diffText(h.versions[i + 1] ? h.versions[i + 1].params : {}, v.params))}</td><td class="small text-secondary">${esc(v.note)}</td><td>${stateCell(v)}</td></tr>`).join('');
  $('src-rows').innerHTML = s.def.src_rule.map(r => `<tr><td><code>${esc(r.src)}</code></td><td>${+r.b1_signed}</td><td>${+r.trickle}</td><td class="num">${+r.idle_w}</td><td>${+r.prop_hold}</td></tr>`).join('');
  $('mode-rows').innerHTML = s.def.mode_rule.map(r => `<tr><td><code>${esc(r.mode)}</code></td><td>${esc(r.action)}</td><td>${+r.capped}</td><td>${esc(r.why)}</td></tr>`).join('');
  refreshSave();
}
if (VPN) {
  $('param-rows').addEventListener('input', refreshSave);
  $('param-save').addEventListener('click', async () => {
    const { set, reset, bad } = paramChanges();
    if (bad) return;
    $('param-save').disabled = true;
    try {
      const { status, data } = await post('/params', { set, reset, note: $('param-note').value });
      if (status !== 201) { msg($('param-msg'), 'No change, nothing saved', 'secondary'); return loadParams(); }
      $('param-note').value = '';
      msg($('param-msg'), `Version ${data.id} saved, waiting for the Pi …`, 'warning');
      await loadParams();
      await waitForPi('/params', data.id, $('param-msg'), () => Promise.all([loadParams(), loadStatus()]));
    } catch (e) { msg($('param-msg'), 'Save failed: ' + e.message, 'danger'); refreshSave(); }
  });
}

// ---- decision tables ------------------------------------------------------------------------------------------------
let dmn = null, loadedXml = '';
function fillViews(keep) {
  const views = dmn.getViews().filter(v => v.type === 'decisionTable'), sel = $('dmn-view');
  sel.innerHTML = '';
  for (const v of views) sel.add(new Option(v.element.name || v.element.id, v.element.id));
  const want = views.find(v => v.element.id === keep) || views[0];
  if (want) { sel.value = want.element.id; dmn.open(want); }
}
async function loadDmn(keep) {
  const [{ data: d }, { data: h }] = await Promise.all([api('/dmn'), api('/dmn/versions')]);
  loadedXml = d.dmn;
  await dmn.importXML(loadedXml);
  fillViews(keep);
  $('dmn-history').innerHTML = h.versions.map(v =>
    `<tr><td class="num">${v.id}</td><td class="text-nowrap">${esc(v.created_at)}</td><td>${esc(v.source)}</td><td>${esc(v.author)}</td><td class="small text-secondary">${esc(v.note)}</td><td>${stateCell(v)}</td><td><a href="${API}/dmn/versions/${v.id}/xml">XML</a></td></tr>`).join('');
}
function initDmn() {
  if (dmn) return;
  dmn = new DmnJS({ container: '#canvas' });
  loadDmn().catch(e => msg($('dmn-msg'), 'Cannot open the DMN: ' + e.message, 'danger'));
}
$('dmn-view').addEventListener('change', () => {
  const v = dmn.getViews().find(v => v.element.id === $('dmn-view').value);
  if (v) dmn.open(v);
});
if (VPN) {
  $('dmn-save').addEventListener('click', async () => {
    const { xml } = await dmn.saveXML({ format: true }), btn = $('dmn-save');
    if (xml === loadedXml) { msg($('dmn-msg'), 'No change', 'secondary'); return; }
    btn.disabled = true;
    try {
      const { status, data } = await post('/dmn', { dmn: xml, note: $('dmn-note').value });
      if (status !== 201) { msg($('dmn-msg'), 'No change, nothing saved', 'secondary'); return; }
      loadedXml = xml;
      $('dmn-note').value = '';
      msg($('dmn-msg'), `Version ${data.id} saved, waiting for the Pi …`, 'warning');
      await waitForPi('/dmn', data.id, $('dmn-msg'), () => Promise.all([loadDmn($('dmn-view').value), loadStatus()]));
    } catch (e) { msg($('dmn-msg'), 'Save failed: ' + e.message, 'danger'); }
    btn.disabled = false;
  });
}

// ---- tabs: #dmn opens the decision tables; dmn-js is created when its tab is visible, it cannot measure a hidden one
document.querySelectorAll('[data-bs-toggle="tab"]').forEach(t => t.addEventListener('shown.bs.tab', ev => {
  const dmnTab = ev.target.dataset.bsTarget === '#tab-dmn';
  history.replaceState(null, '', dmnTab ? '#dmn' : '#params');
  if (dmnTab) initDmn();
}));
if (location.hash === '#dmn') bootstrap.Tab.getOrCreateInstance(document.querySelector('[data-bs-target="#tab-dmn"]')).show();
loadStatus().catch(showError);
loadParams().catch(showError);
setInterval(() => loadStatus().catch(() => {}), 60000);
</script>
</body></html>
