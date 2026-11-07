"""
check_dmn.py - bm_decisions.dmn through a ready-made DMN engine (SpiffWorkflow) against the reader of bm_logic.py.

1. table logic, exhaustive: every decision with its inputExpressions replaced by the bare column names, all 2^n
   true / false combinations; Spiff (hit policy FIRST) and bm_logic.first_hit must give the same outputs.
2. column meaning, sampled: the original inputExpressions on random measured values around the DMN thresholds plus
   random flags; Spiff evaluates the expressions itself, bm_logic via table_bits.
Spiff takes one decision per DMN document, so each decision is cut out into its own document.
Needs lxml + SpiffWorkflow (pip install --user SpiffWorkflow, notebook / dell; not on Venus).
usage: python3 tests/check_dmn.py [samples per table, default 3000]
"""
import copy
import os
import random
import sys
from types import SimpleNamespace

from lxml import etree
from SpiffWorkflow.bpmn.script_engine import PythonScriptEngine
from SpiffWorkflow.dmn.parser.BpmnDmnParser import BpmnDmnParser

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import bm_logic as L  # noqa: E402

NS = {"d": L.DMN_NS.strip("{}")}
BOOL_INPUTS = {"have_pv", "wp_fresh", "have_soc_bat1", "stale", "power_ok", "lim_ok", "cell_ok", "have_pcc"}
ENGINE = PythonScriptEngine()
fails = 0


def check(cond, what):
    global fails
    print(("ok   " if cond else "FAIL ") + what)
    fails += 0 if cond else 1


def site_cfg():
    c = L.Cfg()
    for i, p in enumerate(L.BM_PARAMS):
        if p.name in L.SITE_PARAMS:
            c.set_param(i, L.SITE_PARAMS[p.name])
    return c


def spiff_engine(root, name, bare):
    """a DMNEngine for one decision of the file; bare: inputExpression text = the column name"""
    doc = copy.deepcopy(root)
    for dec in doc.findall("d:decision", NS):
        if dec.get("name") != name:
            doc.remove(dec)
    if bare:
        for inp in doc.iterfind(".//d:input", NS):
            inp.find("d:inputExpression/d:text", NS).text = inp.get("label")
    p = BpmnDmnParser()
    p.add_dmn_xml(doc)
    return p.get_engine(name, None)


def spiff_result(engine, ctx, outs):
    task = SimpleNamespace(data=dict(ctx), workflow=SimpleNamespace(script_engine=ENGINE))
    r = engine.result(task)
    vals = tuple(r.get(o) for o in outs)
    return vals[0] if len(vals) == 1 else vals


def constants_ctx(cfg):
    ctx = L._expr_env(cfg)
    ctx.update({k: v for k, v in vars(L).items() if k.startswith(("BM_SRC_", "BM_WP_", "BM_M_")) and isinstance(v, int)})
    ctx.update(true=True, false=False)
    return ctx


def main():
    samples = int(sys.argv[1]) if len(sys.argv) > 1 else 3000
    cfg = site_cfg()
    root = etree.parse(L.DMN_FILE).getroot()
    outs_of = {d.get("name"): [o.get("name") for o in d.iterfind(".//d:output", NS)] for d in root.findall("d:decision", NS)}
    base = constants_ctx(cfg)
    # thresholds for the samples: the comparison constants, as tests/check_reach.py takes them
    th = {}
    for cols in L.COLUMNS.values():
        for col, ex in cols:
            if ex is None:
                continue
            code = ex[1]
            for n in code.co_names:
                if n in base and isinstance(base[n], (int, float)):
                    th.setdefault("_any", set()).add(float(base[n]))
    points = sorted(th.get("_any", {0.0}))
    r = random.Random(7)
    n1 = n2 = 0
    for name, (cols, rows) in L.TABLES.items():
        outs = outs_of[name]
        # 1. exhaustive table logic
        eng = spiff_engine(root, name, bare=True)
        bad = 0
        for k in range(2 ** len(cols)):
            bits = {c: bool(k >> j & 1) for j, c in enumerate(cols)}
            a = L.first_hit(L.TABLES[name], bits)
            b = spiff_result(eng, dict(base, **bits), outs)
            n1 += 1
            if a != b:
                bad += 1
                if bad <= 3:
                    print("  %s %s: bm_logic %r, Spiff %r" % (name, bits, a, b))
        check(not bad, "%-15s logic: %d combinations identical" % (name, 2 ** len(cols)))
        # 2. sampled column meaning
        eng = spiff_engine(root, name, bare=False)
        flags = [c for c, ex in L.COLUMNS[name] if ex is None]
        bad = 0
        for _ in range(samples if len(flags) < len(cols) else 0):
            raw = {}
            for v in L.DMN_INPUTS:
                raw[v] = (r.random() < 0.6) if v in BOOL_INPUTS else (
                    r.choice(points) + r.choice((-1, 0, 1)) * r.choice((0.001, 0.5)) if r.random() < 0.8
                    else r.uniform(-30000, 30000))
            fl = {c: r.random() < 0.5 for c in flags}
            raw.update({c: v for c, v in fl.items() if c in raw})     # a flag column that is also an input (stale)
            bits = dict(L.table_bits(cfg, (name,), **raw))
            bits.update(fl)
            ctx = dict(base)
            ctx.update(raw)
            ctx.update(fl)
            a = L.first_hit(L.TABLES[name], bits)
            b = spiff_result(eng, ctx, outs)
            n2 += 1
            if a != b:
                bad += 1
                if bad <= 3:
                    print("  %s raw %s: bm_logic %r, Spiff %r" % (name, {k: raw[k] for k in raw}, a, b))
        if len(flags) < len(cols):
            check(not bad, "%-15s meaning: %d samples identical" % (name, samples))
    print("%d exhaustive + %d sampled evaluations, %d failed" % (n1, n2, fails))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
