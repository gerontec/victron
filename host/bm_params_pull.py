#!/usr/bin/env python3
"""bm_params_pull.py - the Pi pulls the newest batmonitor parameter version and applies it (cron, root, every 5 min).

The editor (web/bm_params.php, writes only from the VPN) stores versions in wagodb.bm_param_version; nothing is
pushed into Venus. This script, on the Pi host:
  0. schema: exports the parameter definitions and the source / mode matrix of the DEPLOYED bm_logic.py plus the
     running batmonitor version (state.json) and inserts a new wagodb.bm_param_schema row when they changed. The
     editor always draws its form from the newest schema row, so a new batmonitor shows up within one run
  1. reads the newest row. A rejected row (sha256 in APPLIED with a "rejected" note) is not tried again; an accepted
     one is compared with what the Pi has on every run, so a batmonitor deploy that brought its own bm_tables.h is
     corrected within 5 min (2026-11-05)
  2. checks it: sha256 of the stored text, a JSON object, every name a parameter of the DEPLOYED bm_logic.py
     (/data/batmonitor/bm_logic.py, the Python port of the running C version, same limits), every value a number
     within its min .. max. A failed check: the row gets applied_note "rejected: ...", the env stays. Names the
     deployed version no longer has (a parameter removed by a batmonitor update) are ignored and named in the note
  3. target (2026-11-05, user: "jetzt laeuft alles ueber das .h file"): with a deployed bm_logic.py that reads PARAM
     lines (SITE_PARAMS, batmonitor >= 0.76-py) the PARAM block of /data/batmonitor/bm_tables.h becomes exactly the
     version's overrides (the new file is parsed with the deployed loader before it replaces the old one) and the
     BATMONITOR_<parameter> lines leave the env, so the env keeps only switches (LIVE, PYTHON, ...). An older
     bm_logic.py: the env as before. Backup <file>.bak-<time>, then svc -t batmonitor in Venus.
     Same effective values as before: no restart, applied_note "unchanged"
  4. sets applied_at / applied_note of the row
  D. decision tables (0.78-py, live DMN editor web/bm_dmn.php, wagodb.bm_dmn_version): the newest row from the
     editor is checked with the DEPLOYED DMN reader (load_decisions) and bm_selftest.py (random cycles through
     bm_step with the new tables), then /data/batmonitor/bm_decisions.dmn is replaced (backup .bak-<time>, the last
     DMN_KEEP kept) and batmonitor restarted. A fresh state.json within RUN_CHECK_S proves it runs, else the old file
     goes back (rolled back). Any refusal: applied_note "rejected: ...". When the file on the Pi differs from the
     newest row (a repo deploy, or after a refusal), it is published as a new row with source 'pi', so the editor
     always opens what runs.

Config /etc/bm-params-pull.conf (root, 600): host=, user=, password=, database= (wagodb on .218).
Cron: /etc/cron.d/bm-params-pull  * * * * * root flock -n /run/bm_params_pull.lock /usr/local/sbin/bm_params_pull.py
      >>/var/log/bm_params_pull.log 2>&1   (every minute since 2026-11-07, live DMN editor)
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
TABLES = '/data/batmonitor/bm_tables.h'      # PARAM lines (batmonitor >= 0.76-py)
STATE = '/data/batmonitor/state.json'         # "version" = the running batmonitor (C port)
APPLIED = '/data/batmonitor/params.applied'   # "<id> <sha256>" of the last handled version
DMN = '/data/batmonitor/bm_decisions.dmn'     # decision tables (batmonitor >= 0.78-py)
SELFTEST = '/data/batmonitor/bm_selftest.py'
DMN_KEEP = 10                                 # backups of bm_decisions.dmn kept
DMN_MAX = 2 * 1024 * 1024
RUN_CHECK_S = 45                              # after a DMN change: state.json must be written again within this
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


def has_site_params(m):
    """the deployed logic reads PARAM lines from bm_tables.h"""
    return hasattr(m, 'SITE_PARAMS') and hasattr(m, 'load_tables') and os.path.exists(TABLES)


def env_split(limits):
    """env lines -> (lines to keep, {name: value text} of the parameter lines)"""
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
    return keep, old


def tables_with_params(want):
    """bm_tables.h text with its PARAM lines replaced by `want` ({name: text}), at the place of the first PARAM line
    (or after the header comment); a trailing comment of a kept name stays"""
    with open(TABLES) as f:
        lines = f.read().splitlines()
    notes, out, at = {}, [], None
    for line in lines:
        st = line.strip()
        if st.startswith('PARAM('):
            name = st[6:].split(',', 1)[0].strip()
            i = line.find('/*')
            notes[name] = line[i:] if i >= 0 else ''
            if at is None:
                at = len(out)
            continue
        out.append(line)
    if at is None:                                   # no PARAM block yet: after the first comment block
        at = next((i + 1 for i, l in enumerate(out) if l.strip().endswith('*/')), 0)
        out[at:at] = ['', '/* ---- site parameters: written by host/bm_params_pull.py from the editor ---- */']
        at += 2
    block = []
    for k, v in sorted(want.items()):
        line = 'PARAM(%s, %s)' % (k, v)
        if notes.get(k):
            line = line.ljust(36) + notes[k]
        block.append(line)
    out[at:at] = block
    return '\n'.join(out) + '\n'


def apply_tables(m, want, keep_env, limits):
    """writes the PARAM block and strips the parameter lines from the env; True = something changed"""
    m.load_tables(TABLES)
    old_h = {k: float(v) for k, v in m.SITE_PARAMS.items()}
    _, old_env = env_split(limits)
    if old_h == {k: float(v) for k, v in want.items()} and not old_env:
        return False, old_h
    stamp = time.strftime('%Y%m%d-%H%M%S')
    text = tables_with_params(want)
    with open(TABLES + '.tmp', 'w') as f:
        f.write(text)
    m.load_tables(TABLES + '.tmp')                   # raises on a bad line or value: nothing replaced
    if {k: float(v) for k, v in m.SITE_PARAMS.items()} != {k: float(v) for k, v in want.items()}:
        raise ValueError('bm_tables.h.tmp does not hold the wanted PARAM values')
    subprocess.run(['cp', '-p', TABLES, '%s.bak-%s' % (TABLES, stamp)], check=True)
    os.replace(TABLES + '.tmp', TABLES)
    if old_env:
        subprocess.run(['cp', '-p', ENV, '%s.bak-%s' % (ENV, stamp)], check=True)
        with open(ENV + '.tmp', 'w') as f:
            f.write('\n'.join(keep_env) + '\n')
        os.replace(ENV + '.tmp', ENV)
    return True, dict(old_h, **{'env:' + k: v for k, v in old_env.items()})


def apply_env(want, keep, old):
    """the env as before 0.76-py; True = something changed"""
    if {k: float(v) for k, v in old.items()} == {k: float(v) for k, v in want.items()}:
        return False
    stamp = time.strftime('%Y%m%d-%H%M%S')
    subprocess.run(['cp', '-p', ENV, '%s.bak-%s' % (ENV, stamp)], check=True)
    text = '\n'.join(keep + ['%s%s=%s' % (PREFIX, k, v) for k, v in sorted(want.items())]) + '\n'
    with open(ENV + '.tmp', 'w') as f:
        f.write(text)
    os.replace(ENV + '.tmp', ENV)
    return True


def restart_batmonitor():
    leader = subprocess.run(['machinectl', 'show', 'venus', '-p', 'Leader', '--value'],
                            capture_output=True, text=True, check=True).stdout.strip()
    subprocess.run(['nsenter', '-t', leader, '-a', 'svc', '-t', '/service/batmonitor'], check=True)


def mark(con, row_id, note):
    with con.cursor() as c:
        c.execute("UPDATE bm_param_version SET applied_at = IF(%s LIKE 'rejected%%', NULL, NOW()), applied_note = %s "
                  "WHERE id = %s", (note, note, row_id))
    con.commit()


def load_selftest():
    spec = importlib.util.spec_from_file_location('bm_selftest_deployed', SELFTEST)
    t = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(t)
    return t


def check_dmn(text):
    """the candidate through the deployed reader and bm_selftest; raises with the reason. A fresh module copy, so
    the tables of the running check never mix with the deployed ones"""
    if len(text.encode()) > DMN_MAX:
        raise ValueError('larger than %d bytes' % DMN_MAX)
    tmp = DMN + '.check'
    with open(tmp, 'w') as f:
        f.write(text)
    try:
        m = load_logic()
        m.TABLES = m.load_decisions(tmp)             # format, hit policy, entries, expressions, output words
        m._check_tables()                            # every decision callable with the columns the code supplies
        modes = load_selftest().run(m, 600)          # random cycles through bm_step with these tables
    finally:
        os.remove(tmp)
    return modes


def state_ts():
    try:
        with open(STATE) as f:
            return float(json.load(f).get('ts') or 0)
    except (OSError, ValueError):
        return 0.0


def mark_dmn(con, row_id, note):
    with con.cursor() as c:
        c.execute("UPDATE bm_dmn_version SET applied_at = IF(%s LIKE 'rejected%%', NULL, NOW()), applied_note = %s "
                  "WHERE id = %s", (note, note[:1000], row_id))
    con.commit()


def prune_dmn_backups():
    d = os.path.dirname(DMN)
    baks = sorted(f for f in os.listdir(d) if f.startswith(os.path.basename(DMN) + '.bak-'))
    for f in baks[:-DMN_KEEP]:
        os.remove(os.path.join(d, f))


def sync_dmn(con, m):
    """D. the decision tables: apply the newest editor version, or publish the running file (see the header)"""
    if not hasattr(m, 'load_decisions') or not os.path.exists(DMN):
        return                                       # batmonitor < 0.78-py: tables in bm_tables.h, nothing to do
    with open(DMN) as f:
        running = f.read()
    run_sha = hashlib.sha256(running.encode()).hexdigest()
    with con.cursor() as c:
        c.execute("SELECT id, source, dmn, sha256, applied_note FROM bm_dmn_version ORDER BY id DESC LIMIT 1")
        row = c.fetchone()
    if row and row['source'] == 'editor' and row['applied_note'] is None:
        if hashlib.sha256(row['dmn'].encode()).hexdigest() != row['sha256']:
            mark_dmn(con, row['id'], 'rejected: sha256 mismatch')
        elif row['sha256'] == run_sha:
            mark_dmn(con, row['id'], 'unchanged (same file as on the Pi)')
            return
        else:
            try:
                modes = check_dmn(row['dmn'])
            except Exception as e:
                log('DMN version %d rejected: %s: %s' % (row['id'], type(e).__name__, e))
                mark_dmn(con, row['id'], 'rejected: %s: %s' % (type(e).__name__, e))
            else:
                stamp = time.strftime('%Y%m%d-%H%M%S')
                bak = '%s.bak-%s' % (DMN, stamp)
                subprocess.run(['cp', '-p', DMN, bak], check=True)
                with open(DMN + '.tmp', 'w') as f:
                    f.write(row['dmn'])
                os.replace(DMN + '.tmp', DMN)
                t0 = time.time()
                restart_batmonitor()
                while time.time() - t0 < RUN_CHECK_S and state_ts() <= t0 + 1:
                    time.sleep(2)
                if state_ts() > t0 + 1:
                    prune_dmn_backups()
                    note = 'ok: live since %s, self-test modes %s' % (time.strftime('%H:%M:%S'), ' '.join(sorted(modes)))
                    log('DMN version %d applied, batmonitor restarted and running' % row['id'])
                    mark_dmn(con, row['id'], note)
                    return
                os.replace(bak, DMN)                 # did not run: back to the old tables
                restart_batmonitor()
                log('DMN version %d: batmonitor wrote no state.json within %d s, rolled back' % (row['id'], RUN_CHECK_S))
                mark_dmn(con, row['id'], 'rejected: batmonitor did not run with it within %d s, rolled back' % RUN_CHECK_S)
        with open(DMN) as f:                         # after a refusal: the running file becomes the newest row
            running = f.read()
        run_sha = hashlib.sha256(running.encode()).hexdigest()
        row = None
    if row is None or row['sha256'] != run_sha:
        with con.cursor() as c:
            c.execute("INSERT INTO bm_dmn_version (source, author, note, bm_version, dmn, sha256, applied_at, applied_note) "
                      "VALUES ('pi', 'bm_params_pull', %s, %s, %s, %s, NOW(), 'running on the Pi')",
                      ('file on the Pi (deploy or after a refusal)', running_version(), running, run_sha))
        con.commit()
        log('DMN: running file published (sha %s)' % run_sha[:12])


def main():
    con = pymysql.connect(**read_conf())
    m = load_logic()
    sync_schema(con, m)
    try:
        sync_dmn(con, m)
    except Exception as e:                       # the parameter part below still runs
        log('DMN error: %s: %s' % (type(e).__name__, e))
    with con.cursor() as c:
        c.execute("SELECT id, params, sha256 FROM bm_param_version ORDER BY id DESC LIMIT 1")
        row = c.fetchone()
    if not row:
        return
    tag = '%d %s' % (row['id'], row['sha256'])
    try:
        with open(APPLIED) as f:
            done = f.read().split()
    except FileNotFoundError:
        done = []
    if done[:2] == tag.split() and 'rejected' in done[2:]:
        return                                       # rejected once, not again
    first_time = done[:2] != tag.split()

    limits = limits_of(m)
    try:
        new, ignored = check(row, limits)
    except (ValueError, json.JSONDecodeError) as e:
        log('version %d rejected: %s' % (row['id'], e))
        mark(con, row['id'], 'rejected: %s' % e)
        with open(APPLIED, 'w') as f:
            f.write(tag + ' rejected\n')
        return

    want = {k: fmt(v) for k, v in sorted(new.items())}
    keep, old = env_split(limits)
    if has_site_params(m):
        changed, was = apply_tables(m, want, keep, limits)
        where = 'bm_tables.h'
    else:
        changed, was = (apply_env(want, keep, old) if first_time else False), old   # as before: only a new version
        where = 'env'
    if changed:
        restart_batmonitor()
        note = 'ok (%s)' % where
        log('version %d %s: %s %s (was %s), batmonitor restarted'
            % (row['id'], 'applied' if first_time else 're-applied', where, want, was))
    else:
        note = 'unchanged (%s)' % where
    if ignored:
        note += '; ignored (not in batmonitor %s): %s' % (running_version(), ' '.join(ignored))
    if first_time or changed:
        mark(con, row['id'], note)
        with open(APPLIED, 'w') as f:
            f.write(tag + '\n')
        if not changed:
            log('version %d: same values as the %s, no restart' % (row['id'], where))


if __name__ == '__main__':
    try:
        main()
    except Exception as e:                       # cron: one line, try again in 5 min
        log('error: %s: %s' % (type(e).__name__, e))
        sys.exit(1)
