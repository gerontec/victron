<?php
// bm_api.php - JSON API of the batmonitor editor bm_editor.php (user 2026-11-09). Description: bm_api_openapi.yaml,
// Swagger UI: bm_swagger.php.
//   read: everyone. write: see bm_access.php (VPN; on the Pi also the intranet). A write is a new row, the
//   Pi (host/bm_params_pull.py, cron every minute) checks it against its own limits and puts it live.
//   No session and no cookie here, so no CSRF token: a write must be Content-Type application/json, which a foreign
//   page can only send after a CORS preflight, and this API answers no CORS headers.
date_default_timezone_set('Europe/Berlin');
header('Cache-Control: no-store');
header('X-Content-Type-Options: nosniff');
const DMN_NS = 'https://www.omg.org/spec/DMN/20191111/MODEL/';
const DMN_MAX = 2097152;
const LIST_MAX = 100;

function out($a, int $code = 200): never {
    http_response_code($code);
    header('Content-Type: application/json; charset=utf-8');
    echo json_encode($a, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE);
    exit;
}
function fail(string $msg, int $code = 400): never { out(['error' => $msg], $code); }

set_exception_handler(function (Throwable $e) {
    error_log('bm_api: ' . $e->getMessage());
    fail('internal error', 500);
});
require_once __DIR__ . '/config.php';
require_once __DIR__ . '/bm_access.php';
$pdo = new PDO("mysql:host=$db_host;dbname=$db_name;charset=utf8mb4", $db_user, $db_pass,
               [PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION, PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
                PDO::ATTR_EMULATE_PREPARES => false]);

// pending = the Pi has not answered yet, rejected = refused or rolled back, applied = the Pi runs (or ran) it
function state_of(array $r): string {
    $n = (string)($r['applied_note'] ?? '');
    if (stripos($n, 'rejected') === 0 || stripos($n, 'rolled back') !== false) return 'rejected';
    return $r['applied_at'] === null ? 'pending' : 'applied';
}
function param_row(array $r): array {
    return ['id' => (int)$r['id'], 'created_at' => $r['created_at'], 'author' => $r['author'], 'note' => $r['note'],
            'params' => (object)json_decode($r['params'], true), 'sha256' => $r['sha256'], 'state' => state_of($r),
            'applied_at' => $r['applied_at'], 'applied_note' => $r['applied_note']];
}
const DMN_COLS = 'id, created_at, source, author, note, bm_version, sha256, applied_at, applied_note';
function dmn_row(array $r): array {
    $o = ['id' => (int)$r['id'], 'created_at' => $r['created_at'], 'source' => $r['source'], 'author' => $r['author'],
          'note' => $r['note'], 'bm_version' => $r['bm_version'], 'sha256' => $r['sha256'], 'state' => state_of($r),
          'applied_at' => $r['applied_at'], 'applied_note' => $r['applied_note']];
    if (array_key_exists('dmn', $r)) $o['dmn'] = $r['dmn'];
    return $o;
}
function list_limit(): int {
    $n = filter_var($_GET['limit'] ?? 15, FILTER_VALIDATE_INT);
    if ($n === false || $n < 1 || $n > LIST_MAX) fail('limit must be 1 .. ' . LIST_MAX);
    return $n;
}
function newest_schema(PDO $pdo): array {
    $s = $pdo->query("SELECT id, created_at, bm_version, def FROM bm_param_schema ORDER BY id DESC LIMIT 1")->fetch();
    if (!$s) fail('no parameter schema from the Pi yet', 503);
    $s['def'] = json_decode($s['def'], true);
    return $s;
}
// the checks every write passes; returns the JSON body as an array
function write_body(): array {
    if (!bm_can_write()) fail('no write access from this address', 403);
    if (!preg_match('#^application/json\b#i', $_SERVER['CONTENT_TYPE'] ?? '')) fail('Content-Type must be application/json', 415);
    $in = json_decode(file_get_contents('php://input', false, null, 0, 2 * DMN_MAX + 4096), true);
    if (!is_array($in) || array_is_list($in) && $in !== []) fail('body must be a JSON object');
    return $in;
}
function note_of(array $in): string {
    if (isset($in['note']) && !is_string($in['note'])) fail('note must be a string');
    return mb_substr(trim($in['note'] ?? ''), 0, 255);
}
function is_dry_run(): bool {
    return filter_var($_GET['dry_run'] ?? false, FILTER_VALIDATE_BOOLEAN);
}

// ---- the parameters -----------------------------------------------------------------------------------------------
function get_params(PDO $pdo): never {
    $schema = newest_schema($pdo);
    $r = $pdo->query("SELECT * FROM bm_param_version ORDER BY id DESC LIMIT 1")->fetch();
    if (!$r) fail('no parameter version yet', 404);
    $cur = json_decode($r['params'], true);
    $values = [];
    foreach ($schema['def']['params'] as $p) {
        $set = array_key_exists($p['name'], $cur);
        $values[] = ['name' => $p['name'], 'value' => $set ? $cur[$p['name']] : $p['default'], 'overridden' => $set,
                     'default' => $p['default'], 'min' => $p['min'], 'max' => $p['max'], 'unit' => $p['unit'], 'help' => $p['help']];
    }
    $run = $pdo->query("SELECT id FROM bm_param_version WHERE applied_at IS NOT NULL AND (applied_note IS NULL OR applied_note NOT LIKE 'rejected%')
                        ORDER BY id DESC LIMIT 1")->fetch();
    out(['bm_version' => $schema['bm_version'], 'version' => param_row($r), 'running_version_id' => $run ? (int)$run['id'] : null,
         'values' => $values]);
}

// merges into the newest version: `set` values, `reset` names back to the default, everything else stays
function post_params(PDO $pdo): never {
    $in = write_body();
    $set = $in['set'] ?? [];
    $reset = $in['reset'] ?? [];
    if (!is_array($set) || ($set !== [] && array_is_list($set))) fail('set must be an object {NAME: number}');
    if (!is_array($reset) || !array_is_list($reset)) fail('reset must be an array of names');
    if (!$set && !$reset) fail('nothing to do: give set or reset');
    $defs = [];
    foreach (newest_schema($pdo)['def']['params'] as $p) $defs[$p['name']] = $p;
    foreach ($reset as $name) {
        if (!is_string($name) || !isset($defs[$name])) fail('unknown parameter in reset: ' . (is_string($name) ? $name : gettype($name)));
        if (array_key_exists($name, $set)) fail("$name: in set and in reset");
    }
    foreach ($set as $name => $v) {
        if (!isset($defs[$name])) fail("unknown parameter: $name");
        if (!is_int($v) && !is_float($v)) fail("$name: not a number");
        if ($v < $defs[$name]['min'] || $v > $defs[$name]['max']) fail("$name: $v outside {$defs[$name]['min']} .. {$defs[$name]['max']}");
    }
    $note = note_of($in);
    $pdo->beginTransaction();
    $last = $pdo->query("SELECT id, params FROM bm_param_version ORDER BY id DESC LIMIT 1 FOR UPDATE")->fetch();
    $cur = $last ? json_decode($last['params'], true) : [];
    $new = array_intersect_key($cur, $defs);                       // a parameter the running version no longer has goes
    $dropped = array_keys(array_diff_key($cur, $defs));
    foreach ($reset as $name) unset($new[$name]);
    foreach ($set as $name => $v) {
        if ((float)$v == (float)$defs[$name]['default']) unset($new[$name]);   // equal to the default: no override
        else $new[$name] = $v;
    }
    ksort($new);
    $json = json_encode((object)$new, JSON_UNESCAPED_SLASHES);
    $changed = !$last || $last['params'] !== $json;
    $res = ['changed' => $changed, 'dry_run' => is_dry_run(), 'id' => $last ? (int)$last['id'] : null,
            'params' => (object)$new, 'dropped_obsolete' => $dropped];
    if (!$changed || is_dry_run()) { $pdo->rollBack(); out($res); }
    $st = $pdo->prepare("INSERT INTO bm_param_version (author, note, params, sha256) VALUES (?, ?, ?, ?)");
    $st->execute(['api ' . $_SERVER['REMOTE_ADDR'], $note, $json, hash('sha256', $json)]);
    $res['id'] = (int)$pdo->lastInsertId();
    $pdo->commit();
    out($res, 201);
}

// ---- the decision tables --------------------------------------------------------------------------------------------
function post_dmn(PDO $pdo): never {
    $in = write_body();
    if (!is_string($in['dmn'] ?? null) || $in['dmn'] === '') fail('dmn must be the DMN XML text');
    $dmn = $in['dmn'];
    if (strlen($dmn) > DMN_MAX) fail('larger than 2 MB');
    $doc = new DOMDocument();
    if (!@$doc->loadXML($dmn, LIBXML_NONET) || $doc->doctype !== null || $doc->documentElement->localName !== 'definitions'
            || $doc->documentElement->namespaceURI !== DMN_NS) {
        fail('not a DMN 1.3 definitions document');
    }
    $note = note_of($in);
    $sha = hash('sha256', $dmn);
    $last = $pdo->query("SELECT id, sha256 FROM bm_dmn_version ORDER BY id DESC LIMIT 1")->fetch();
    $changed = !$last || $last['sha256'] !== $sha;
    $res = ['changed' => $changed, 'dry_run' => is_dry_run(), 'id' => $last ? (int)$last['id'] : null, 'sha256' => $sha];
    if (!$changed || is_dry_run()) out($res);
    $st = $pdo->prepare("INSERT INTO bm_dmn_version (source, author, note, dmn, sha256) VALUES ('editor', ?, ?, ?, ?)");
    $st->execute(['api ' . $_SERVER['REMOTE_ADDR'], $note, $dmn, $sha]);
    $res['id'] = (int)$pdo->lastInsertId();
    out($res, 201);
}

function get_status(PDO $pdo): never {
    $live = $pdo->query("SELECT ts, bm_l1_rule, bm_l2_rule, bm_l3_rule FROM pv_victron ORDER BY ts DESC LIMIT 1")->fetch();
    $p = $pdo->query("SELECT * FROM bm_param_version ORDER BY id DESC LIMIT 1")->fetch();
    $d = $pdo->query("SELECT " . DMN_COLS . " FROM bm_dmn_version ORDER BY id DESC LIMIT 1")->fetch();
    out(['bm_version' => newest_schema($pdo)['bm_version'], 'can_write' => bm_can_write(),
         'live' => $live ? ['ts' => $live['ts'], 'rules' => ['L1' => $live['bm_l1_rule'], 'L2' => $live['bm_l2_rule'], 'L3' => $live['bm_l3_rule']]] : null,
         'params_version' => $p ? param_row($p) : null, 'dmn_version' => $d ? dmn_row($d) : null]);
}

// ---- routes --------------------------------------------------------------------------------------------------------
$path = trim($_SERVER['PATH_INFO'] ?? '', '/');
$method = $_SERVER['REQUEST_METHOD'] === 'HEAD' ? 'GET' : $_SERVER['REQUEST_METHOD'];
$id = 0;
$route = preg_replace_callback('#(?<=/versions/)\d+#', function ($m) use (&$id) { $id = (int)$m[0]; return '{id}'; }, $path, 1);
$known = ['status' => 'GET', 'params' => 'GET,POST', 'params/schema' => 'GET', 'params/versions' => 'GET', 'params/versions/{id}' => 'GET',
          'dmn' => 'GET,POST', 'dmn/versions' => 'GET', 'dmn/versions/{id}' => 'GET', 'dmn/versions/{id}/xml' => 'GET'];
if (!isset($known[$route])) fail('no such resource, see bm_swagger.php', 404);
if (!in_array($method, explode(',', $known[$route]), true)) {
    header('Allow: ' . $known[$route]);
    fail('method not allowed', 405);
}

function dmn_by_id(PDO $pdo, int $id): array {
    $st = $pdo->prepare("SELECT " . DMN_COLS . ", dmn FROM bm_dmn_version WHERE id = ?");
    $st->execute([$id]);
    $r = $st->fetch();
    if (!$r) fail('no such version', 404);
    return $r;
}

switch ("$method $route") {
    case 'GET status': get_status($pdo);
    case 'GET params': get_params($pdo);
    case 'POST params': post_params($pdo);
    case 'POST dmn': post_dmn($pdo);
    case 'GET params/schema':
        $s = newest_schema($pdo);
        out(['id' => (int)$s['id'], 'created_at' => $s['created_at'], 'bm_version' => $s['bm_version'], 'def' => $s['def']]);
    case 'GET params/versions':
        $st = $pdo->prepare("SELECT * FROM bm_param_version ORDER BY id DESC LIMIT ?");
        $st->bindValue(1, list_limit(), PDO::PARAM_INT);
        $st->execute();
        out(['versions' => array_map('param_row', $st->fetchAll())]);
    case 'GET params/versions/{id}':
        $st = $pdo->prepare("SELECT * FROM bm_param_version WHERE id = ?");
        $st->execute([$id]);
        $r = $st->fetch();
        if (!$r) fail('no such version', 404);
        out(param_row($r));
    case 'GET dmn':
        $r = $pdo->query("SELECT " . DMN_COLS . ", dmn FROM bm_dmn_version ORDER BY id DESC LIMIT 1")->fetch();
        if (!$r) fail('no DMN version yet', 404);
        out(dmn_row($r));
    case 'GET dmn/versions':
        $st = $pdo->prepare("SELECT " . DMN_COLS . " FROM bm_dmn_version ORDER BY id DESC LIMIT ?");
        $st->bindValue(1, list_limit(), PDO::PARAM_INT);
        $st->execute();
        out(['versions' => array_map('dmn_row', $st->fetchAll())]);
    case 'GET dmn/versions/{id}':
        out(dmn_row(dmn_by_id($pdo, $id)));
    case 'GET dmn/versions/{id}/xml':
        $r = dmn_by_id($pdo, $id);
        header('Content-Type: application/xml; charset=utf-8');
        echo $r['dmn'];
        exit;
}
