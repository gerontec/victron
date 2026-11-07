"""
check_reach.py - reachability matrix of the batmonitor decision states (Python, numpy; networkx as a cross-check).

What is enumerated: the decision layer of bm_logic.py (the tables of bm_decisions.dmn through bm_d1 / bm_d2 /
bm_d3 / bm_d7 and chain_split / chain_rotate), not bm_step with its forecast. The site parameters (PARAM lines) apply.

1. Inputs = only consistent column bits: the thresholds come from the DMN inputExpressions (dmn_thresholds), every
   expression is evaluated on representative values of its measured quantity (each threshold - eps, itself, + eps,
   the domain ends), so a bit vector like "SoC < 8 and SoC >= 10" never occurs. Every expression compares one
   quantity only, so sorting the thresholds is the exact LP feasibility test; one that is not is reported.
2. State vector of the persistent decision state (component B): prot, force, low, chg_prev, prop_prev, split_all
   = 2^6 = 64 candidate states. T[i, j] = 1 if some consistent input leads from i to j in one cycle.
3. R = (I + T)^n boolean (repeated squaring up to the fixpoint) = reachability matrix; reachable from the start
   state (State(): all 0) = row R[s0]. Components that do not couple with B are done alone and multiplied:
   A = bat1_first (D1 latch), C = rot (L1 -> L2 -> L3 every ROTATE_S), D = house_on (D7).
   Coupling A -> B is only prop_hold (S3 source STK); STK is possible with either bat1_first (heat pump off), so
   prop_hold is a free input of B.

usage: python3 tests/check_reach.py        exit 1 if an invariant fails
"""
import ast
import itertools
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import bm_logic as L  # noqa: E402

EPS = 1e-3
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


def reps(thresholds, lo, hi):
    """representative values: every threshold -eps / itself / +eps inside [lo, hi], plus lo and hi"""
    v = {lo, hi}
    for t in thresholds:
        for x in (t - EPS, t, t + EPS):
            if lo <= x <= hi:
                v.add(x)
    return sorted(v)


def dmn_thresholds(L, cfg):
    """{input: sorted thresholds} of every inputExpression comparison linear in exactly one input with coefficient
    +-1, plus [(column, comparison)] of those that are not (they would need an LP); representatives around
    these values, so every consistent bit vector occurs"""
    env = L._expr_env(cfg)
    th, other = {}, []

    def lin(node):                      # -> ({input: coef}, const) or None
        if isinstance(node, ast.Constant):
            return {}, float(node.value)
        if isinstance(node, ast.Name):
            if node.id in L.DMN_INPUTS:
                return {node.id: 1.0}, 0.0
            return ({}, float(env[node.id])) if node.id in env else None
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            r = lin(node.operand)
            return ({k: -v for k, v in r[0].items()}, -r[1]) if r else None
        if isinstance(node, ast.BinOp):
            a, b = lin(node.left), lin(node.right)
            if a is None or b is None:
                return None
            sg = 1.0 if isinstance(node.op, ast.Add) else -1.0
            co = dict(a[0])
            for k, v in b[0].items():
                co[k] = co.get(k, 0.0) + sg * v
            return co, a[1] + sg * b[1]
        return None

    seen = set()
    for cols in L.COLUMNS.values():
        for col, ex in cols:
            if ex is None or col in seen:
                continue
            seen.add(col)
            for node in ast.walk(ex[0]):
                if not isinstance(node, ast.Compare):
                    continue
                a, b = lin(node.left), lin(node.comparators[0])
                co = None if a is None or b is None else \
                    {k: v for k in set(a[0]) | set(b[0]) for v in [a[0].get(k, 0.0) - b[0].get(k, 0.0)] if v}
                if co and len(co) == 1 and abs(next(iter(co.values()))) == 1.0:
                    k, v = next(iter(co.items()))
                    th.setdefault(k, set()).add(-(a[1] - b[1]) / v)
                else:
                    other.append((col, ast.unparse(node)))
    return {k: sorted(v) for k, v in th.items()}, other


def closure(T):
    """boolean reachability R = (I + T)^n by squaring until the fixpoint"""
    R = np.eye(len(T), dtype=bool) | T
    while True:
        R2 = (R.astype(np.uint8) @ R.astype(np.uint8)) > 0
        if (R2 == R).all():
            return R
        R = R2


cfg = site_cfg()
TH, NONLIN = dmn_thresholds(L, cfg)
check(not NONLIN, "every DMN comparison is in one quantity (no LP needed): %s" % (NONLIN or "-"))
print("thresholds from bm_decisions.dmn: %s" % TH)

# ---- consistent input alphabets (step 1) ----------------------------------------------------------------------
bank_bits = set()
for bms_ok in (0, 1):
    for soc in reps(TH["soc"], 0.0, 100.0):
        for cell_ok in (0, 1):
            for cell in reps(TH["cell_min"], 2.5, 3.65):
                for lim_ok in (0, 1):
                    for dcl in reps(TH["dcl"], 0.0, 600.0):
                        for power_ok in (0, 1):
                            for pw in reps(TH["power"], -5000.0, 5000.0):
                                b = L.d2_bank_bits(cfg, bms_ok, soc, power_ok, pw, lim_ok, dcl, cell_ok, cell)
                                bank_bits.add(tuple(sorted(b.items())))
surplus_bits = {tuple(L.table_bits(cfg, ("D2_CHARGE_MODE",), surplus=s)[k] for k in ("surplus_gt_on", "surplus_gt_hold"))
                for s in reps(TH["surplus"], -30000.0, 30000.0)}
split_w = reps([cfg.single_max_w - cfg.single_hyst_w, cfg.single_max_w], 0.0, 12000.0)
print("inputs: %d bank bit vectors (of 2^9 = 512), %d surplus vectors (of 4), %d split amounts"
      % (len(bank_bits), len(surplus_bits), len(split_w)))

# ---- component B: prot, force, low, chg_prev, prop_prev, split_all -------------------------------------------
NAMES = ("prot", "force", "low", "chg_prev", "prop_prev", "split_all")
STATES = list(itertools.product((0, 1), repeat=len(NAMES)))
IDX = {s: i for i, s in enumerate(STATES)}
T = np.zeros((len(STATES), len(STATES)), dtype=bool)
modes_of = {s: set() for s in STATES}          # mode reached in the cycle that leaves state s
for s in STATES:
    prot, force, low, chg_prev, prop_prev, split_all = s
    seen = set()
    for bb in bank_bits:
        bank = dict(bb)
        for (on, hold), stale, block, ff, house_import, deficit, prop_hold in itertools.product(
                surplus_bits, (0, 1), (0, 1), (0, 1), (0, 1), (0, 1), (0, 1)):
            d = {"stale": stale, "block": block, "surplus_gt_on": on, "surplus_gt_hold": hold,
                 "bank": [dict(bank, ff=ff)]}
            p2, f2, l2, _, _, mode = L.bm_d2(d, [prot], [force], [low], chg_prev)
            mode = mode[0]
            n_chg, n_dis = mode == L.BM_M_CHARGE, mode == L.BM_M_DIS
            prop = L.bm_d3(1, house_import, deficit, prop_hold, prop_prev) if n_dis else 0
            for w in (split_w if (n_chg or n_dis) else [0.0]):
                st = L.State()
                st.split_all = split_all
                if n_chg or n_dis:
                    L.chain_split(cfg, st, L.Out(), w)
                else:
                    st.split_all = 0
                nxt = (p2[0], f2[0], l2[0], int(n_chg), int(prop), st.split_all)
                key = (nxt, mode)
                if key not in seen:
                    seen.add(key)
                    T[IDX[s], IDX[nxt]] = True
                    modes_of[s].add(mode)

R = closure(T)
s0 = IDX[(0, 0, 0, 0, 0, 0)]
reach = [STATES[j] for j in range(len(STATES)) if R[s0, j]]
print("\ncomponent B: %d of %d candidate states reachable from the start state" % (len(reach), len(STATES)))
for s in reach:
    print("  " + " ".join("%s=%d" % (n, v) for n, v in zip(NAMES, s)) + "   modes: "
          + ",".join(sorted(L.BM_MODE_NAME[m] for m in modes_of[s])))
never = sorted({n for s in STATES if not R[s0, IDX[s]] for n, v in zip(NAMES, s) if v and
                all(not R[s0, IDX[t]] for t in STATES if t[NAMES.index(n)])})
print("bits never 1 from the start: %s" % (", ".join(never) or "none"))

try:                                            # cross-check with networkx
    import networkx as nx
    g = nx.DiGraph()
    g.add_nodes_from(range(len(STATES)))
    g.add_edges_from(zip(*np.nonzero(T)))
    check(set(nx.descendants(g, s0)) | {s0} == {IDX[s] for s in reach}, "networkx descendants = numpy closure")
except ImportError:
    print("networkx missing, cross-check skipped")

# ---- component A: bat1_first (D1 latch) ----------------------------------------------------------------------
TA = np.zeros((2, 2), dtype=bool)
for bf in (0, 1):
    for have in (0, 1):
        for soc_b1 in reps(TH["soc_bat1"], 0.0, 100.0):
            for night, wp_on, winter, b1_dis in itertools.product((0, 1), repeat=4):
                b = dict(L.table_bits(cfg, ("D1_LATCH",), have_soc_bat1=have, soc_bat1=soc_b1),
                         night=night, wp_on=wp_on, winter=winter, b1_dis=b1_dis)
                TA[bf, L.bm_d1(b, bf)["bat1_first"]] = True
RA = closure(TA)
nA = int(RA[0].sum())

# ---- component D: house_on (D7) with the site HOUSE_ACOUT --------------------------------------------------------
mode = L.lround(cfg.house_acout)
TD = np.zeros((2, 2), dtype=bool)
for on in (0, 1):
    for total in reps(TH["acout_total"], -5000.0, 15000.0):
        for under_long in (0, 1):
            hb = L.table_bits(cfg, ("D7",), acout_total=total)
            nxt, _ = L.bm_d7(mode == 0, mode == 1, hb["over_on"], hb["under_off"], under_long and hb["under_off"], on)
            TD[on, nxt] = True
RD = closure(TD)
nD = int(RD[0].sum())

# ---- component C: rotation, a pure 3-cycle -------------------------------------------------------------------
TC = np.zeros((L.NPH, L.NPH), dtype=bool)
for r in range(L.NPH):
    TC[r, r] = TC[r, (r + 1) % L.NPH] = True
nC = int(closure(TC)[0].sum())

print("\ncomponents: A bat1_first %d · B %d · C rot %d · D house_on %d (HOUSE_ACOUT=%d)" % (nA, len(reach), nC, nD, mode))
cells = sum(len(modes_of[s]) for s in reach)
print("reachable persistent states A x B x C x D = %d x %d x %d x %d = %d" % (nA, len(reach), nC, nD,
                                                                            nA * len(reach) * nC * nD))
print("reachable (state B, mode) cells: %d" % cells)

# ---- invariants --------------------------------------------------------------------------------------------------
check(all(not s[1] for s in reach) == (cfg.soc_force <= 0), "force latch never set exactly when SOC_FORCE = 0")
check(all(not (s[4] and s[3]) for s in reach), "never charging and proportional discharging at once (chg_prev & prop_prev)")
check(all(R[IDX[s], s0] for s in reach), "the start state is reachable again from every reachable state (no trap)")
print("%d failed" % fails)
sys.exit(1 if fails else 0)
