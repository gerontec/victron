#!/usr/bin/env python3
"""
gen_docs.py - the batmonitor diagrams in docs/.

1. decision_tables.dot and decision_flow.pdf (one A4 page per topic, FLOW_PAGES) from venus-addons/batmonitor/bm_tables.h (read with the loader batmonitor itself uses), plus
   the effect tables BM_MODE_RULE / BM_SRC_RULE of bm_logic.py. Arrows: the output of one table is a column of
   another. Per row the share of all input combinations it decides (hit policy FIRST); a row that decides none is
   dead (shadowed by the rows above) and is reported.
2. observed_states.dot: the states batmonitor was in during the last --days days (default 4) from wagodb.pv_victron
   (one row per minute): coverage, the D1 cells run through the D1 tables of bm_tables.h, every rule state. It is
   appended as the last page of decision_tables.pdf. DB login from ~/.my.cnf [wagodb], host --db-host (default
   192.168.178.218); without the DB (or with --no-db) the page is left out.
3. Renders every docs/*.dot (also the hand-written control_flow / charge_flow / discharge_flow / wp_matrix / opl_flow)
   to .pdf and .png.

usage: python3 docs/gen_docs.py [--no-render] [--no-db] [--days 4] [--db-host HOST]
"""
import collections
import datetime
import glob
import itertools
import os
import re
import subprocess
import sys

DOCS = os.path.dirname(os.path.abspath(__file__))
BM = os.path.join(DOCS, "..", "venus-addons", "batmonitor")
sys.path.insert(0, BM)
import bm_logic as L  # noqa: E402

GROUPS = [("D1", "D1 Sofar first, source, winter heat pump rule"), ("D2", "D2 per stack: latches and mode"),
          ("DLEAD", "SoC balancing"), ("D3", "D3 discharge PROP / IDLE"), ("D4", "D4 summer peak window"),
          ("D5", "D5 DO4 pulse"), ("D7", "D7 house on AC-out1")]
COLOUR = {1: "#cfe8c6", 0: "#f6d5d5", None: "#f4f4f4"}
EFFECT_IN = {"mode": "mode_rule", "src": "src_rule"}      # outputs that select a row of an effect table


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def notes(path):
    """the comment block right above each TABLE line of bm_tables.h, as one line of text"""
    out, buf = {}, []
    for line in open(path):
        s = line.strip()
        m = re.match(r"TABLE\((\w+),", s)
        if m:
            out[m.group(1)] = " ".join(buf)
            buf = []
        elif s.startswith("/*") or s.startswith("*"):
            buf.append(s.strip("/* ").rstrip("*/ ").strip())
        else:
            buf = []
    return out


def raw_outputs(path):
    """{table: [[output word, ...] per row]} exactly as written in bm_tables.h, the BM_SRC_ / BM_WP_ / BM_M_ / LEAD_
    prefix dropped (the loader turns the words into values; 0 and BM_SRC_DAY are both 0 there)"""
    out = {}
    for line in open(path):
        m = re.match(r'\s*ROW\((\w+),\s*"[01\- ]+",\s*([\w, ]+)\)', line)
        if m:
            words = [re.sub(r"^(BM_SRC_|BM_WP_|BM_M_|LEAD_)", "", w.strip()) for w in m.group(2).split(",")]
            out.setdefault(m.group(1), []).append(words)
    return out


RAW = raw_outputs(L.TABLES_FILE)


def hits(cols, rows):
    """per row: how many of the 2^n input combinations it decides (first match)"""
    n = [0] * len(rows)
    for bits in itertools.product((0, 1), repeat=len(cols)):
        for i, row in enumerate(rows):
            if all(c is None or c == b for c, b in zip(row, bits)):
                n[i] += 1
                break
    return n


def table_html(name, cols, outs, rows, note=""):
    h = hits(cols, [r[:len(cols)] for r in rows])
    total = 2 ** len(cols)
    t = ['<table border="0" cellborder="1" cellspacing="0" cellpadding="3" bgcolor="#ffffff">',
         '<tr><td colspan="%d" bgcolor="#dbe8fb"><b>%s</b></td></tr>' % (len(cols) + len(outs) + 1, name),
         "<tr>" + "".join('<td><font point-size="9">%s</font></td>' % esc(c) for c in cols)
         + "".join('<td bgcolor="#e8e8e8"><b>%s</b></td>' % esc(o) for o in outs)
         + '<td><font point-size="8">share</font></td></tr>']
    for i, (row, k) in enumerate(zip(rows, h)):
        cells = "".join('<td bgcolor="%s">%s</td>' % (COLOUR[c], "-" if c is None else c) for c in row[:len(cols)])
        o = "".join('<td bgcolor="#e8e8e8"><b>%s</b></td>' % esc(w) for w in RAW[name][i])
        share = '<td bgcolor="%s"><font point-size="8">%s</font></td>' % (
            "#ff9a9a" if k == 0 else "#ffffff", "%d/%d" % (k, total) if k else "DEAD")
        t.append("<tr>" + cells + o + share + "</tr>")
    if note:
        t.append('<tr><td colspan="%d" align="left" balign="left"><font point-size="8">%s</font></td></tr>' % (
            len(cols) + len(outs) + 1, "<br/>".join(esc(x) for x in wrap(note, 70))))
    t.append("</table>")
    return "\n".join(t)


def wrap(text, width):
    lines, cur = [], ""
    for w in text.split():
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    return lines + ([cur] if cur else [])


def effect_tables():
    a = {L.BM_A_ZERO: "0 W", L.BM_A_FORCE: "FORCE_CHARGE_W", L.BM_A_CHARGE: "charge amount",
         L.BM_A_DISCHARGE: "discharge amount"}
    w = {L.BM_W_NONE: "(discharge path)", L.BM_W_NAME: "NAME", L.BM_W_SOC: "NAME(soc %)",
         L.BM_W_PEAK_H: "NAME(peak h)", L.BM_W_POWER: "NAME(BMS W)", L.BM_W_CHAIN: "NAME SRC:.."}
    m = ['<table border="0" cellborder="1" cellspacing="0" cellpadding="3" bgcolor="#ffffff">',
         '<tr><td colspan="4" bgcolor="#eaf6e6"><b>mode_rule</b> BM_MODE_RULE: what a mode does</td></tr>',
         "<tr><td><b>mode</b></td><td><b>action</b></td><td><b>capped</b></td><td><b>rule text</b></td></tr>"]
    for k in sorted(L.BM_MODE_RULE):
        r = L.BM_MODE_RULE[k]
        m.append("<tr><td>%s</td><td>%s</td><td>%d</td><td>%s</td></tr>" % (L.BM_MODE_NAME[k], a[r.action], r.capped,
                                                                            w[r.why]))
    m.append("</table>")
    s = ['<table border="0" cellborder="1" cellspacing="0" cellpadding="3" bgcolor="#ffffff">',
         '<tr><td colspan="5" bgcolor="#eaf6e6"><b>src_rule</b> BM_SRC_RULE: the discharge per source</td></tr>',
         "<tr><td><b>src</b></td><td>b1_signed</td><td>trickle</td><td>idle_w</td><td>prop_hold</td></tr>"]
    for k in sorted(L.BM_SRC_RULE):
        r = L.BM_SRC_RULE[k]
        s.append("<tr><td>%s</td><td>%d</td><td>%d</td><td>%d</td><td>%d</td></tr>" % (
            L.BM_SRC_NAME[k], r.b1_signed, r.trickle, r.idle_w, r.prop_hold))
    s.append("</table>")
    return "\n".join(m), "\n".join(s)


def gen_tables_dot():
    tables = L.load_tables()
    outs = {}
    for line in open(L.TABLES_FILE):
        m = re.match(r'\s*TABLE\((\w+),\s*"[\w ]+",\s*"([\w ]+)"\)', line)
        if m:
            outs[m.group(1)] = m.group(2).split()
    note = notes(L.TABLES_FILE)
    dead = []
    d = ["// generated by docs/gen_docs.py from venus-addons/batmonitor/bm_tables.h - do not edit",
         "digraph tables {",
         '\tgraph [rankdir=LR, fontname="Helvetica", fontsize=12, nodesep=0.25, ranksep=0.5, labelloc=t, '
         'label=<<b>batmonitor %s: decision tables</b> (bm_tables.h, %d tables, %d rows) - first matching row wins, '
         '- = any; green 1, red 0; share = input combinations the row decides (DEAD = never reached)>];'
         % (L_version(), len(tables), sum(len(r) for _, r in tables.values())),
         '\tnode [shape=plain, fontname="Helvetica", fontsize=10];',
         '\tedge [fontname="Helvetica", fontsize=9, color="#4a7fb5"];']
    for pre, title in GROUPS:
        names = [n for n in tables if n == pre or n.startswith(pre + "_")]
        d.append('\tsubgraph cluster_g_%s {' % pre)
        d.append('\t\tlabel=<<b>%s</b>>; style="rounded,filled"; fillcolor="#f7f9fc"; color="#4a7fb5";' % esc(title))
        for n in names:
            cols, rows = tables[n]
            if 0 in hits(cols, [r[:len(cols)] for r in rows]):
                dead.append(n)
            d.append('\t\t%s [label=<%s>];' % (n, table_html(n, cols, outs[n], rows, note.get(n, ""))))
        d.append("\t}")
    mode_html, src_html = effect_tables()
    d.append('\tsubgraph cluster_effect {\n\t\tlabel=<<b>effect tables</b> (bm_logic.py / bm_logic.c)>; '
             'style="rounded,filled"; fillcolor="#f3faf0"; color="#5a9e4b";')
    d.append("\t\tmode_rule [label=<%s>];\n\t\tsrc_rule [label=<%s>];\n\t}" % (mode_html, src_html))
    for a in tables:                     # the output of a is a column of b
        for o in outs[a]:
            for b, (cols, _) in tables.items():
                if a != b and o in cols:
                    d.append('\t%s -> %s [label=" %s"];' % (a, b, o))
            if o in EFFECT_IN:
                d.append('\t%s -> %s [label=" %s", color="#5a9e4b"];' % (a, EFFECT_IN[o], o))
    d.append("}")
    open(os.path.join(DOCS, "decision_tables.dot"), "w").write("\n".join(d) + "\n")
    return dead


def cond_text(cols, row):
    """the conditions of one row as text: a, not b (the - columns left out)"""
    parts = [c if v == 1 else "not " + c for c, v in zip(cols, row) if v is not None]
    return " and ".join(parts) if parts else "otherwise"


# decision_flow.pdf: one A4 portrait page per topic, the tables of a page one below the other
FLOW_PAGES = [
    ("D1 - Sofar first, S3 source of the discharge, winter heat pump rule",
     ["D1_LATCH", "D1_B1_FIRST", "D1_SRC", "D1_WP_CAP"]),
    ("D2 - per stack: charge mode and the latches protection / force charge / LOW",
     ["D2_CHARGE_MODE", "D2_PROT", "D2_FORCE", "D2_LOW"]),
    ("D2 - mode per stack (priority top to bottom)", ["D2_MODE"]),
    ("SoC balancing and D3 discharge PROP / IDLE", ["DLEAD", "D3"]),
    ("D4 - summer peak window", ["D4", "D4_PEAK"]),
    ("D5 - DO4 pulse, D7 - house on AC-out1", ["D5", "D7"]),
]


def links(tables, outs):
    """per table: (inputs from [(table, column)], used by [(table, column)])"""
    frm, to = collections.defaultdict(list), collections.defaultdict(list)
    for a in tables:
        for o in outs[a]:
            for b, (cols, _) in tables.items():
                if a != b and o in cols:
                    to[a].append((b, o))
                    frm[b].append((a, o))
            if o in EFFECT_IN:
                to[a].append((EFFECT_IN[o], o))
    return frm, to


def flow_page_dot(no, n_pages, title, names, tables, outs, note, frm, to):
    """one page: each table a chain of questions in row order (first match), the tables one below the other"""
    d = ["digraph flow {",
         '\tgraph [rankdir=TB, fontname="Helvetica", fontsize=14, nodesep=0.3, ranksep=0.3, labelloc=t, '
         'compound=true, newrank=true, label=<<b>batmonitor %s - decision flow %d / %d: %s</b><br/>'
         '<font point-size="10">from bm_tables.h: each table is asked row by row, top to bottom; the first row that '
         'matches decides (yes -&gt; its result, no -&gt; the next row)</font>>];' % (
             L_version(), no, n_pages, esc(title)),
         '\tnode [fontname="Helvetica", fontsize=11];',
         '\tedge [fontname="Helvetica", fontsize=10];']
    last = None
    for n in names:
        cols, rows = tables[n]
        extra = []
        if note.get(n):
            extra += [esc(x) for x in wrap(note[n], 90)]
        if frm[n]:
            extra.append("<i>inputs from: %s</i>" % esc(", ".join("%s (%s)" % x for x in frm[n])))
        if to[n]:
            extra.append("<i>used by: %s</i>" % esc(", ".join("%s (%s)" % x for x in to[n])))
        d.append('\tsubgraph cluster_t_%s {' % n)
        d.append('\t\tlabel=<<b>%s</b> -&gt; %s<br/><font point-size="9">%s</font>>; style="rounded,filled"; '
                 'fillcolor="#f7f9fc"; color="#4a7fb5"; fontsize=13;' % (n, esc(", ".join(outs[n])),
                                                                         "<br/>".join(extra)))
        d.append('\t\t%s_in [shape=point, width=0.1];' % n)
        prev = n + "_in"
        for i, row in enumerate(rows):
            res = ", ".join("%s = %s" % (o, w) for o, w in zip(outs[n], RAW[n][i]))
            res_id = "%s_r%d" % (n, i)
            d.append('\t\t%s [shape=box, style="rounded,filled", fillcolor="#e8e8e8", label=<<b>%s</b>>];' % (
                res_id, esc(res)))
            if all(c is None for c in row[:len(cols)]):
                d.append('\t\t%s -> %s [label=" otherwise"];' % (prev, res_id))
                last = res_id
                break
            q = "%s_q%d" % (n, i)
            d.append('\t\t%s [shape=diamond, style=filled, fillcolor="#dbe8fb", margin=0.03, label=<%s?>];' % (
                q, "<br/>".join(esc(x) for x in wrap(cond_text(cols, row[:len(cols)]), 30))))
            d.append('\t\t%s -> %s%s;' % (prev, q, ' [label=" no"]' if i else ""))
            d.append('\t\t%s -> %s [label=" yes", color="#3c7a30", fontcolor="#3c7a30"];' % (q, res_id))
            prev = q
        d.append("\t}")
        if last and names.index(n) + 1 < len(names):     # the next table below this one
            d.append("\t%s -> %s_in [style=invis, weight=10];" % (last, names[names.index(n) + 1]))
    for a in names:                                       # links inside the page
        for b, o in to[a]:
            if b in names:
                d.append('\t%s_r0 -> %s_in [ltail=cluster_t_%s, lhead=cluster_t_%s, color="#4a7fb5", style=dashed, '
                         'label=" %s", constraint=false];' % (a, b, a, b, o))
    d.append("}")
    return "\n".join(d) + "\n"


def gen_flow_pdf(render_it=True):
    """decision_flow.pdf: the pages of FLOW_PAGES, each fitted to A4 portrait (ghostscript)"""
    import tempfile
    tables = L.load_tables()
    outs = {}
    for line in open(L.TABLES_FILE):
        m = re.match(r'\s*TABLE\((\w+),\s*"[\w ]+",\s*"([\w ]+)"\)', line)
        if m:
            outs[m.group(1)] = m.group(2).split()
    paged = [n for _, ns in FLOW_PAGES for n in ns]
    missing = [n for n in tables if n not in paged]
    if missing or len(paged) != len(set(paged)):
        raise SystemExit("FLOW_PAGES must hold every table of bm_tables.h once; missing: %s" % missing)
    note = notes(L.TABLES_FILE)
    frm, to = links(tables, outs)
    if not render_it:
        return
    with tempfile.TemporaryDirectory() as tmp:
        pdfs = []
        for i, (title, names) in enumerate(FLOW_PAGES, 1):
            dot = os.path.join(tmp, "p%02d.dot" % i)
            open(dot, "w").write(flow_page_dot(i, len(FLOW_PAGES), title, names, tables, outs, note, frm, to))
            pdfs.append(dot[:-4] + ".pdf")
            subprocess.run(["dot", "-Tpdf", dot, "-o", pdfs[-1]], check=True)
        subprocess.run(["gs", "-q", "-dBATCH", "-dNOPAUSE", "-sDEVICE=pdfwrite", "-sPAPERSIZE=a4", "-dFIXEDMEDIA",
                        "-dPDFFitPage", "-o", os.path.join(DOCS, "decision_flow.pdf")] + pdfs, check=True)
    print("rendered decision_flow.pdf (%d A4 pages)" % len(FLOW_PAGES))


MEASURED = re.compile(r"\b(FORCE|PROT|CHARGING|LADESPERRE|HOLD)\([^)]*\)")   # measured values, not part of a state
D1_BITS = ["night", "wp_on", "b1_above_min", "b1_ge_release", "winter", "b1_dis"]  # bm_d1_id bits 0..5, bit 6 bat1_first


def observed(days, host):
    """the last `days` local days of wagodb.pv_victron -> dict for observed_states.dot, None without the DB"""
    try:
        import pymysql
        con = pymysql.connect(read_default_file=os.path.expanduser("~/.my.cnf"), read_default_group="wagodb",
                              host=host, database="wagodb", connect_timeout=10, read_timeout=60)
    except Exception as e:                                  # noqa: BLE001 - any DB problem: page left out
        print("observed states: no DB (%s), page left out" % e)
        return None
    since = (datetime.datetime.now() - datetime.timedelta(days=days - 1)).replace(hour=0, minute=0, second=0,
                                                                                   microsecond=0)
    with con.cursor() as c:
        c.execute("SELECT ts, bm_d1_id, bm_l1_rule, bm_l2_rule, bm_l3_rule FROM pv_victron WHERE ts >= %s ORDER BY ts",
                  (since,))
        rows = c.fetchall()
    con.close()
    if not rows:
        return None
    end = rows[-1][0]
    expected = int((end - since).total_seconds() // 60) + 1
    have = {r[0] for r in rows}
    gaps, t, run = [], since, None
    while t <= end:                                         # runs of minutes without a row or without a state
        r = t in have
        if not r:
            run = [run[0], t] if run and run[1] == t - datetime.timedelta(minutes=1) else [t, t]
            if not gaps or gaps[-1][0] != run[0]:
                gaps.append(run)
            else:
                gaps[-1] = run
        t += datetime.timedelta(minutes=1)
    ok = [r for r in rows if r[1] is not None and all(r[2:5])]
    state = lambda r: tuple(MEASURED.sub(r"\1", x) for x in r[2:5])
    days_t = collections.OrderedDict()
    for r in ok:
        d = days_t.setdefault(r[0].date(), [0, set(), set()])
        d[0] += 1
        d[1].add(r[1])
        d[2].add(state(r))
    cells = collections.Counter(r[1] for r in ok)
    cell_rows = []
    for cell, n in cells.most_common():
        b = {k: (cell >> i) & 1 for i, k in enumerate(D1_BITS)}
        o = L.bm_d1(b, (cell >> 6) & 1)
        cell_rows.append((cell, "+".join(str(1 << i) for i in range(8) if cell >> i & 1), n,
                          ", ".join(k for k in D1_BITS + ["bat1_first"] if (cell >> (D1_BITS + ["bat1_first"]).index(k)) & 1),
                          L.BM_SRC_NAME[o["src_reason"]], {L.BM_WP_NONE: "none", L.BM_WP_ZERO: "0 W",
                                                            L.BM_WP_MAX: "WP_WINTER_W"}[o["wp_cap"]]))
    return {"since": since, "end": end, "expected": expected, "rows": len(rows), "ok": len(ok),
            "gaps": [g for g in gaps], "days": days_t, "cells": cell_rows,
            "states": collections.Counter(state(r) for r in ok).most_common()}


def gen_observed_dot(o, days):
    tab = '<table border="0" cellborder="1" cellspacing="0" cellpadding="3" bgcolor="#ffffff">'
    cov = 100.0 * o["ok"] / o["expected"]
    g = "".join("<tr><td>%s .. %s</td><td>%d min</td></tr>" % (a.strftime("%d.%m. %H:%M"), b.strftime("%H:%M"),
                int((b - a).total_seconds() // 60) + 1) for a, b in o["gaps"]) or '<tr><td colspan="2">none</td></tr>'
    day = "".join("<tr><td>%s</td><td>%d</td><td>%d</td><td>%d</td></tr>" % (d.strftime("%a %d.%m."), v[0], len(v[1]),
                  len(v[2])) for d, v in o["days"].items())
    head = (tab + '<tr><td colspan="4" bgcolor="#dbe8fb"><b>coverage</b> %s .. %s: %d of %d minutes with a state '
            '(%.2f %%)</td></tr><tr><td><b>day</b></td><td><b>minutes</b></td><td><b>D1 cells</b></td>'
            '<td><b>rule states</b></td></tr>%s<tr><td colspan="4"><b>gaps</b> (minutes without a row)</td></tr>'
            '<tr><td colspan="4">%s</td></tr></table>') % (
        o["since"].strftime("%d.%m. %H:%M"), o["end"].strftime("%d.%m. %H:%M"), o["ok"], o["expected"], cov, day,
        "<br/>".join(re.findall(r"<td>(.*?)</td><td>(.*?)</td>", g) and
                     ["%s  %s" % x for x in re.findall(r"<td>(.*?)</td><td>(.*?)</td>", g)] or ["none"]))
    cells = (tab + '<tr><td colspan="6" bgcolor="#dbe8fb"><b>D1 cells</b> (pv_victron.bm_d1_id) through the D1 tables'
             ' of bm_tables.h</td></tr><tr><td><b>cell</b></td><td><b>bits</b></td><td><b>minutes</b></td>'
             '<td><b>set bits</b></td><td><b>src</b></td><td><b>wp_cap</b></td></tr>'
             + "".join("<tr><td>%d</td><td>%s</td><td>%d</td><td align=\"left\">%s</td><td>%s</td><td>%s</td></tr>"
                       % (c, b, n, esc(t), s_, w) for c, b, n, t, s_, w in o["cells"]) + "</table>")
    st = (tab + '<tr><td colspan="3" bgcolor="#dbe8fb"><b>rule states</b> L1 | L2 | L3 (measured values in brackets '
          'removed), %d different</td></tr><tr><td><b>minutes</b></td><td><b>%%</b></td><td><b>L1 | L2 | L3</b></td></tr>'
          % len(o["states"])
          + "".join('<tr><td>%d</td><td>%.1f</td><td align="left">%s</td></tr>' % (n, 100.0 * n / o["ok"],
                    esc(" | ".join(s_))) for s_, n in o["states"]) + "</table>")
    d = ["// generated by docs/gen_docs.py from wagodb.pv_victron - do not edit", "digraph observed {",
         '\tgraph [rankdir=TB, fontname="Helvetica", fontsize=12, labelloc=t, nodesep=0.4, label=<<b>batmonitor: '
         'observed states, last %d days</b> (wagodb.pv_victron, one row per minute = the state of the last 5 s cycle;'
         ' generated %s)>];' % (days, datetime.datetime.now().strftime("%Y-%m-%d %H:%M")),
         '\tnode [shape=plain, fontname="Helvetica", fontsize=10];',
         "\thead [label=<%s>];" % head, "\tcells [label=<%s>];" % cells, "\tstates [label=<%s>];" % st,
         "\thead -> cells -> states [style=invis];", "}"]
    open(os.path.join(DOCS, "observed_states.dot"), "w").write("\n".join(d) + "\n")


def L_version():
    m = re.search(r'^VERSION = "([^"]+)"', open(os.path.join(BM, "batmonitor.py")).read(), re.M)
    return m.group(1) if m else "?"


def render():
    for dot in sorted(glob.glob(os.path.join(DOCS, "*.dot"))):
        base = dot[:-4]
        for fmt, extra in (("pdf", []), ("png", ["-Gdpi=110"])):
            subprocess.run(["dot", "-T" + fmt, *extra, dot, "-o", base + "." + fmt], check=True)
        print("rendered", os.path.basename(base) + ".pdf / .png")


def arg(name, default):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


if __name__ == "__main__":
    days = int(arg("--days", 4))
    obs = None if "--no-db" in sys.argv else observed(days, arg("--db-host", "192.168.178.218"))
    if obs:
        gen_observed_dot(obs, days)
        print("observed_states.dot written: %d of %d minutes with a state, %d rule states" % (
            obs["ok"], obs["expected"], len(obs["states"])))
    elif os.path.exists(os.path.join(DOCS, "observed_states.dot")):
        os.remove(os.path.join(DOCS, "observed_states.dot"))
    dead = gen_tables_dot()
    gen_flow_pdf(render_it=False)                          # checks that every table has a page
    print("decision_tables.dot written from", os.path.relpath(L.TABLES_FILE))
    for n in dead:
        print("WARNING: table %s has a DEAD row (shadowed by the rows above)" % n)
    if "--no-render" not in sys.argv:
        render()
        gen_flow_pdf()
        if obs:                                             # the observed states as the last page of decision_tables.pdf
            t = os.path.join(DOCS, "decision_tables.pdf")
            subprocess.run(["pdfunite", t, os.path.join(DOCS, "observed_states.pdf"), t + ".tmp"], check=True)
            os.replace(t + ".tmp", t)
            print("decision_tables.pdf: observed states appended as the last page")
    sys.exit(1 if dead else 0)
