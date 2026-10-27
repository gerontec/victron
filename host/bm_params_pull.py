#!/usr/bin/env python3
"""bm_params_pull.py - the Pi pulls the newest batmonitor parameter version and applies it (cron, root, every 5 min).

The editor (web/bm_params.php, writes only from the VPN) stores versions in wagodb.bm_param_version; nothing is
pushed into Venus. This script, on the Pi host:
  0. schema: exports the parameter definitions and the source / mode matrix of the DEPLOYED bm_logic.py plus the
     running batmonitor version (state.json) and inserts a new wagodb.bm_param_schema row when they changed. The
     editor always draws its form from the newest schema row, so a new batmonitor shows up within one run
  1. reads the newest row; done if its sha256 is the one in APPLIED (already applied or rejected)
  2. checks it: sha256 of the stored text, a JSON object, every name a parameter of the DEPLOYED bm_logic.py
     (/data/batmonitor/bm_logic.py, the Python port of the running C version, same limits), every value a number
     within its min .. max. A failed check: the row gets applied_note "rejected: ...", the env stays. Names the
     deployed version no longer has (a parameter removed by a batmonitor update) are ignored and named in the note
  3. rewrites /data/batmonitor/env: lines that are not BATMONITOR_<parameter> stay (BATMONITOR_LIVE, ...), the
     parameter lines become exactly the version's overrides; backup env.bak-<time>; then svc -t batmonitor in Venus.
     Same effective values as before (e.g. the seed): no restart, applied_note "unchanged"
  4. sets applied_at / applied_note of the row

Config /etc/bm-params-pull.conf (root, 600): host=, user=, password=, database= (wagodb on .218).
Cron: /etc/cron.d/bm-params-pull  */5 * * * * root /usr/local/sbin/bm_params_pull.py >>/var/log/bm_params_pull.log 2>&1
"""
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time

import pymysql

CONF = '/etc/bm-params-pull.conf'
ENV = '/data/batmonitor/env'
LOGIC = '/data/batmonitor/bm_logic.py'
STATE = '/data/batmonitor/state.json'         # "version" = the running batmonitor (C port)
APPLIED = '/data/batmonitor/params.applied'   # "<id> <sha256>" of the last handled version
PREFIX = 'BATMONITOR_'


def log(msg):
    print(time.strftime('%Y-%m-%d %H:%M:%S'), msg, flush=True)


def read_conf():
    c = {}
    with open(CONF) as f:
        for line in f:
            k, sep, v = line.strip().partition('=')
            if sep and not k.startswith('#'):
                c[k.strip()] = v.strip()
    return dict(host=c['host'], user=c['user'], password=c['password'], database=c.get('database', 'wagodb'),
                connect_timeout=5, cursorclass=pymysql.cursors.DictCursor)


def load_logic():
    """the deployed Python port as a module"""
    spec = importlib.util.spec_from_file_location('bm_logic_deployed', LOGIC)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def limits_of(m):
    """{name: (min, max)}"""
    return {p.name: (float(p.min), float(p.max)) for p in m.BM_PARAMS}


ACTION = ['ZERO', 'FORCE', 'CHARGE', 'DISCHARGE']
WHY = ['NONE', 'NAME', 'SOC', 'PEAK_H', 'POWER', 'CHAIN']


def export_defs(m, version):
    """the schema the editor draws: parameters (form) and the source / mode matrix (read only)"""
    return {
        'version': version,
        'params': [{'name': p.name, 'default': p.default, 'min': p.min, 'max': p.max, 'unit': p.unit, 'help': p.help}
                   for p in m.BM_PARAMS],
        'src_rule': [dict(src=m.BM_SRC_NAME[k], b1_signed=r.b1_signed, trickle=r.trickle, idle_w=r.idle_w,
                          prop_hold=r.prop_hold) for k, r in sorted(m.BM_SRC_RULE.items())],
        'mode_rule': [dict(mode=m.BM_MODE_NAME[k], action=ACTION[r.action], capped=r.capped, why=WHY[r.why])
                      for k, r in sorted(m.BM_MODE_RULE.items())],
    }


def running_version():
    try:
        with open(STATE) as f:
            return str(json.load(f).get('version') or '?')
    except (OSError, ValueError):
        return '?'


def sync_schema(con, m):
    """a new bm_param_schema row when the deployed definitions or the running version changed"""
    text = json.dumps(export_defs(m, running_version()), sort_keys=True, separators=(',', ':'))
    sha = hashlib.sha256(text.encode()).hexdigest()
    with con.cursor() as c:
        c.execute("SELECT sha256 FROM bm_param_schema ORDER BY id DESC LIMIT 1")
        last = c.fetchone()
        if last and last['sha256'] == sha:
            return
        c.execute("INSERT INTO bm_param_schema (bm_version, def, sha256) VALUES (%s, %s, %s)",
                  (running_version(), text, sha))
    con.commit()
    log('schema: new definitions of batmonitor %s published' % running_version())


def check(row, limits):
    """(the overrides {name: value}, ignored names); raises ValueError with the reason"""
    if hashlib.sha256(row['params'].encode()).hexdigest() != row['sha256']:
        raise ValueError('sha256 mismatch')
    p = json.loads(row['params'])
    if not isinstance(p, dict):
        raise ValueError('not a JSON object')
    ignored = sorted(k for k in p if k not in limits)
    p = {k: v for k, v in p.items() if k in limits}
    for k, v in p.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError(k + ': not a number')
        lo, hi = limits[k]
        if not lo <= v <= hi:
            raise ValueError('%s %s outside %s .. %s' % (k, v, lo, hi))
    return p, ignored


def fmt(v):
    return ('%.6f' % v).rstrip('0').rstrip('.') if isinstance(v, float) else str(v)


def mark(con, row_id, note):
    with con.cursor() as c:
        c.execute("UPDATE bm_param_version SET applied_at = IF(%s LIKE 'rejected%%', NULL, NOW()), applied_note = %s "
                  "WHERE id = %s", (note, note, row_id))
    con.commit()


def main():
    con = pymysql.connect(**read_conf())
    m = load_logic()
    sync_schema(con, m)
    with con.cursor() as c:
        c.execute("SELECT id, params, sha256 FROM bm_param_version ORDER BY id DESC LIMIT 1")
        row = c.fetchone()
    if not row:
        return
    tag = '%d %s' % (row['id'], row['sha256'])
    try:
        with open(APPLIED) as f:
            if f.read().strip() == tag:
                return
    except FileNotFoundError:
        pass

    limits = limits_of(m)
    try:
        new, ignored = check(row, limits)
    except (ValueError, json.JSONDecodeError) as e:
        log('version %d rejected: %s' % (row['id'], e))
        mark(con, row['id'], 'rejected: %s' % e)
        with open(APPLIED, 'w') as f:
            f.write(tag + '\n')
        return

    with open(ENV) as f:
        lines = f.read().splitlines()
    keep, old = [], {}
    for line in lines:
        k, sep, v = line.partition('=')
        name = k[len(PREFIX):] if k.startswith(PREFIX) else None
        if sep and name in limits:
            old[name] = v.strip()
        else:
            keep.append(line)
    want = {k: fmt(v) for k, v in sorted(new.items())}
    same = {k: float(v) for k, v in old.items()} == {k: float(v) for k, v in want.items()}
    if same:
        note = 'unchanged'
    else:
        stamp = time.strftime('%Y%m%d-%H%M%S')
        subprocess.run(['cp', '-p', ENV, '%s.bak-%s' % (ENV, stamp)], check=True)
        text = '\n'.join(keep + ['%s%s=%s' % (PREFIX, k, v) for k, v in want.items()]) + '\n'
        with open(ENV + '.tmp', 'w') as f:
            f.write(text)
        os.replace(ENV + '.tmp', ENV)
        leader = subprocess.run(['machinectl', 'show', 'venus', '-p', 'Leader', '--value'],
                                capture_output=True, text=True, check=True).stdout.strip()
        subprocess.run(['nsenter', '-t', leader, '-a', 'svc', '-t', '/service/batmonitor'], check=True)
        note = 'ok'
        log('version %d applied: %s (was %s), batmonitor restarted' % (row['id'], want, old))
    if ignored:
        note += '; ignored (not in batmonitor %s): %s' % (running_version(), ' '.join(ignored))
    mark(con, row['id'], note)
    with open(APPLIED, 'w') as f:
        f.write(tag + '\n')
    if same:
        log('version %d: same values as the env, no restart' % row['id'])


if __name__ == '__main__':
    try:
        main()
    except Exception as e:                       # cron: one line, try again in 5 min
        log('error: %s: %s' % (type(e).__name__, e))
        sys.exit(1)
