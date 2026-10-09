#!/usr/bin/env python3
"""Generate the FUXA project for venus.heissa.de: the Victron three-phase MultiPlus system in Lenggries.
WebAPI device polling victron_api.php (one tag per value), one view with the grid / ESS, the ATS changeover switch,
ATS changeover switch (AC-in), the Venus Pi 4, the three MultiPlus-II and the two battery stacks:
dev1 (L2, middle) on Stack1 (MUST, CAN BMS), dev0 (L1) and dev2 (L3) on Stack2 (Pytes, EBox).
Same widget formats as fuxa_ww_project.py (gerontec/FUXA branch heissa).
usage: python3 fuxa_victron_project.py victron_api.json > /tmp/fuxa_project.json"""
import json, sys
from html import escape

API, DEV = 'https://heissa.de/web1/victron_api.php', 'd_victron_api'
FG, MUTED, STROKE = '#e5e7eb', '#9ca3af', '#cbd5e1'
GREEN, ORANGE, BLUE, GREY = '#22c55e', '#f59e0b', '#60a5fa', '#6b7280'
SCALE = 1.6
W, H = 1460, 800

# ---- tags: one per victron_api.php value -------------------------------------------------------
api = json.load(open(sys.argv[1]))
tags = {}
for k, e in api['values'].items():
    typ = ('boolean' if isinstance(e['value'], bool) or e['unit'] == 'bool'
           else 'string' if isinstance(e['value'], str) or e['unit'] == 'text' else 'number')
    tags['t_' + k] = {'id': 't_' + k, 'name': k, 'label': k, 'type': typ, 'address': f'values:{k}:value',
                      'description': e['description'], 'daq': {'enabled': False, 'interval': 60, 'changed': True}}
tags['t_timestamp'] = {'id': 't_timestamp', 'name': 'timestamp', 'label': 'timestamp', 'type': 'string',
                       'address': 'timestamp', 'daq': {'enabled': False, 'interval': 60, 'changed': True}}

svg, items, n = [], {}, [0]


def nid(p='svg_'):
    n[0] += 1
    return f'{p}{n[0]:04d}'


def text(x, y, s, size=15, weight='normal', anchor='start', fill=FG):
    svg.append(f'<text id="{nid()}" x="{x}" y="{y}" font-size="{size}" font-weight="{weight}" text-anchor="{anchor}" '
               f'font-family="sans-serif" fill="{fill}" stroke-width="0" xml:space="preserve">{escape(s, quote=False)}</text>')


def value(x, y, key, unit='', digits=1, size=17, weight='bold', anchor='start', fill=FG):
    assert 't_' + key in tags, key
    gid, tid = nid('VAL_'), nid('VAL_')
    svg.append(f'<g id="{gid}" type="svg-ext-value" fill="{fill}" font-size="{size}" font-family="sans-serif" text-anchor="{anchor}" stroke-width="0">'
               f'<text id="{tid}" x="{x}" y="{y}" font-size="{size}" font-weight="{weight}" text-anchor="{anchor}" font-family="sans-serif" '
               f'fill="{fill}" stroke-width="0" xml:space="preserve">–</text></g>')
    items[gid] = {'id': gid, 'type': 'svg-ext-value', 'name': key, 'label': 'Value',
                  'property': {'variableId': 't_' + key, 'variableSrc': DEV, 'events': [], 'actions': [],
                               'ranges': [{'type': 1, 'text': unit, 'fractionDigits': digits}]}}


def shape(tag, attrs, key=None, ranges=None):
    sid = nid()
    a = ' '.join(f'{k}="{v}"' for k, v in attrs.items())
    svg.append(f'<{tag} id="{sid}" {a}/>')
    if key:
        assert 't_' + key in tags, key
        items[sid] = {'id': sid, 'type': f'svg-ext-shapes-{tag}', 'name': key, 'label': 'Shapes',
                      'property': {'variableId': 't_' + key, 'variableSrc': DEV, 'events': [], 'actions': [], 'ranges': ranges}}
    return sid


def box(x, y, w, h, fill, rx=12, key=None, ranges=None):
    shape('rect', {'x': x, 'y': y, 'width': w, 'height': h, 'rx': rx, 'fill': fill, 'stroke': STROKE, 'stroke-width': 1.2}, key, ranges)


def flow_ranges(prop, neg, zero, pos, dead):
    """colour by sign: value < -dead -> neg, |value| <= dead -> zero, > dead -> pos (prop 'stroke' or 'color' = fill)"""
    def r(lo, hi, c):
        return {'type': 2, 'min': lo, 'max': hi, 'color': c if prop == 'color' else '', 'stroke': c if prop == 'stroke' else ''}
    return [r(-1e6, -dead, neg), r(-dead, dead, zero), r(dead, 1e6, pos)]


def line(d, key=None, dead=0.05, width=7, dash=None, color=GREY):
    attrs = {'d': d, 'fill': 'none', 'stroke': color, 'stroke-width': width, 'stroke-linecap': 'round', 'stroke-linejoin': 'round'}
    if dash:
        attrs['stroke-dasharray'] = dash
    shape('path', attrs, key, flow_ranges('stroke', ORANGE, GREY, GREEN, dead) if key else None)


def soc_bar(x, y, key, seg_w=26, seg_h=22):
    """10 segments, segment i lights from SoC >= 10*i + 5; the lowest two orange"""
    for i in range(10):
        col = GREEN if i >= 2 else ORANGE
        shape('rect', {'x': x + i * (seg_w + 4), 'y': y, 'width': seg_w, 'height': seg_h, 'rx': 3, 'fill': '#1f2937',
                       'stroke': STROKE, 'stroke-width': 0.8},
              key, [{'type': 2, 'min': -1, 'max': 10 * i + 5, 'color': '#1f2937', 'stroke': ''},
                    {'type': 2, 'min': 10 * i + 5, 'max': 1000, 'color': col, 'stroke': ''}])


def click_tile(x, y, w, h, name, event):
    sid = shape('rect', {'x': x, 'y': y, 'width': w, 'height': h, 'rx': 6, 'fill': '#ffffff', 'fill-opacity': 0.001, 'stroke': 'none'})
    items[sid] = {'id': sid, 'type': 'svg-ext-shapes-rect', 'name': name, 'label': 'Shapes',
                  'property': {'variableId': '', 'events': [dict(type='click', **event)], 'actions': [], 'ranges': []}}


# ---- header ------------------------------------------------------------------------------------
text(30, 34, 'Victron MultiPlus-II · three-phase ESS', 20, 'bold')
text(560, 34, 'as of', 13, fill=MUTED)
value(600, 34, 'timestamp', '', None, 13, 'normal', fill=MUTED)
text(790, 34, 'victron_api.php, 1/min', 13, fill=MUTED)
box(1250, 14, 180, 28, '#16263a', 6)
text(1340, 33, '◂ Heat pump', 13, 'bold', 'middle', '#93c5fd')
click_tile(1250, 14, 180, 28, 'nav_ww', {'action': 'onOpenTab', 'actparam': 'https://fuxa.heissa.de/', 'actoptions': {'newTab': False}})

# ---- Pi 4 / Venus (far left) --------------------------------------------------------------------
box(30, 60, 310, 150, '#1e1b3a')
text(48, 86, 'Raspberry Pi 4 · Venus OS v3.81', 15, 'bold')
text(48, 106, '192.168.178.119 · MK3-USB → VE.Bus', 12, fill=MUTED)
rows = [('VE.Bus', 'vebus_state_text', '', None), ('switch', 'vebus_mode_text', '', None), ('error', 'vebus_error', '', 0),
        ('DC', 'dc_v', 'V', 2), ('DC curr.', 'dc_a', 'A', 1), ('AC-out', 'ac_out_kw', 'kW', 2)]
for i, (lab, key, unit, dig) in enumerate(rows):
    x, y = 48 + (i % 2) * 145, 132 + (i // 2) * 24
    text(x, y, lab, 13, fill=MUTED)
    value(x + 132, y, key, unit, dig, 14, anchor='end')

# ---- grid side (user 2026-10-09): Z1 -> Z2 -> junction: A to the rotary switch (position I, normal),
#      B through the MCB to AC-in of the three MultiPlus; their AC-out1 goes to position II (only on grid failure)
GRIDC = '#94a3b8'
G = {'fill': 'none', 'stroke': GRIDC, 'stroke-width': 7, 'stroke-linecap': 'round', 'stroke-linejoin': 'round'}
box(370, 70, 100, 50, '#1f2937', 8)
text(420, 92, 'Z1', 15, 'bold', 'middle')
text(420, 111, 'grid meter', 11, 'normal', 'middle', MUTED)
box(510, 70, 100, 50, '#1f2937', 8)
text(560, 92, 'Z2', 15, 'bold', 'middle')
text(560, 111, 'house meter', 11, 'normal', 'middle', MUTED)
shape('path', dict(G, d='M470,95 L510,95'))
shape('path', dict(G, d='M610,95 L840,95'))                 # branch A -> rotary switch I
shape('circle', {'cx': 660, 'cy': 95, 'r': 7, 'fill': GRIDC, 'stroke': 'none'})
text(700, 86, 'A', 13, 'bold', fill=GRIDC)
shape('path', dict(G, d='M660,95 L660,140'))                # branch B -> MCB
text(668, 132, 'B', 13, 'bold', fill=GRIDC)
box(590, 140, 140, 50, '#2a1a1c', 6)
text(604, 160, 'MCB 3~ → AC-in', 13, 'bold')
text(604, 181, 'AC-in limit', 11, fill=MUTED)
value(720, 181, 'ac_in_limit_a', 'A', 0, 12, anchor='end')

# rotary changeover switch 3~ (ATS): common -> house; I = grid (branch A, normal), II = AC-out1 of the MultiPlus.
# blade on II only while Venus reports a grid failure (grid_lost: VE.Bus active input 240, the Multis invert)
ATS_BG = '#2a1a1c'
box(840, 60, 230, 130, ATS_BG)
shape('path', dict(G, d='M840,95 L890,95'))                 # I
shape('path', {'d': 'M955,190 L955,152', 'fill': 'none', 'stroke': '#a78bfa', 'stroke-width': 5, 'stroke-linecap': 'round'})  # II
shape('path', dict(G, d='M1010,125 L1070,125'))             # common -> house


def blade(d, on_lost):
    """switch blade, visible while grid_lost == on_lost, else drawn in the box colour"""
    on, off = {'type': 2, 'color': '', 'stroke': FG}, {'type': 2, 'color': '', 'stroke': ATS_BG}
    lo, hi = (on, off) if not on_lost else (off, on)
    shape('path', {'d': d, 'fill': 'none', 'stroke': FG if not on_lost else ATS_BG, 'stroke-width': 5, 'stroke-linecap': 'round'},
          'grid_lost', [dict(lo, min=-1, max=0.5), dict(hi, min=0.5, max=2)])


blade('M1010,125 L892,97', 0)                                 # default: grid -> house
blade('M1010,125 L957,150', 1)                                # grid failure: UPS (AC-out1) -> house
for cx, cy in ((890, 95), (955, 152), (1010, 125)):
    shape('circle', {'cx': cx, 'cy': cy, 'r': 6, 'fill': FG, 'stroke': 'none'})

# ---- house: fed by the rotary switch; AC-out1 per unit (backup leg) -------------------------------
PURPLE = '#a78bfa'
box(1110, 60, 320, 130, '#251a33')
text(1126, 84, 'House', 15, 'bold')
text(1126, 104, 'AC-out1 = UPS leg of the ATS', 11, fill=MUTED)
for i, (ph, key) in enumerate((('L1', 'l1_ac_out_w'), ('L2', 'l2_ac_out_w'), ('L3', 'l3_ac_out_w'))):
    text(1126, 126 + i * 20, f'AC-out1 {ph}', 13, fill=MUTED)
    value(1300, 126 + i * 20, key, 'W', 0, 14, anchor='end')
text(1320, 146, 'total', 11, fill=MUTED)
value(1420, 146, 'ac_out_kw', 'kW', 2, 12, 'normal', anchor='end')
text(30, 52, 'AC-in / DC: + charging (green) · − feeding the house (orange) · AC-out1 → rotary switch II (purple)', 11, fill=MUTED)

# AC bus from the MCB to the three MultiPlus
# physical order on the wall (user 2026-10-08): unit 1 left = L2, unit 2 middle = L1 (master, Stack1), unit 3 right = L3
CX = {'l2': 230, 'l1': 730, 'l3': 1230}
LEFT, RIGHT = min(CX.values()), max(CX.values())
line('M660,190 L660,235', 'ac_in_kw', 50 / 1000, color=GREY)
line(f'M{LEFT - 50},235 L{RIGHT - 50},235', 'ac_in_kw', 50 / 1000)
for p, x in CX.items():
    line(f'M{x - 50},235 L{x - 50},275', f'{p}_ac_in_w', 50)
text(LEFT - 40, 228, 'AC-in 3~', 12, fill=MUTED)
# AC-out1 of each unit up to the rotary switch, position II (purple)
AO = {'fill': 'none', 'stroke': PURPLE, 'stroke-width': 5, 'stroke-linecap': 'round', 'stroke-linejoin': 'round'}
for x in CX.values():
    shape('path', dict(AO, d=f'M{x + 120},275 L{x + 120},255'))
shape('path', dict(AO, d=f'M{LEFT + 120},255 L{RIGHT + 120},255'))
shape('path', dict(AO, d='M955,255 L955,190'))
text(963, 222, 'AC-out1 · UPS', 12, fill=PURPLE)
# MK3-USB from the Pi only to unit 1 (left, L2); the other units hang on the VE.Bus chain unit 1 - 2 - 3
VB = {'fill': 'none', 'stroke': BLUE, 'stroke-width': 2.5, 'stroke-dasharray': '7 5'}
shape('path', dict(VB, d=f'M{CX["l2"] + 50},210 L{CX["l2"] + 50},275'))
text(CX['l2'] + 58, 262, 'MK3-USB', 12, fill=BLUE)
for a_, b_ in (('l2', 'l1'), ('l1', 'l3')):
    shape('path', dict(VB, d=f'M{CX[a_] + 160},300 L{CX[b_] - 160},300'))
    text((CX[a_] + CX[b_]) // 2, 316, 'VE.Bus', 12, anchor='middle', fill=BLUE)

# ---- the three MultiPlus -----------------------------------------------------------------------
MP = [('l2', 'Unit 1 (left)', 'L2', 'dev1 · HQ2606Y2Y4U', 'Stack2 · Pytes'),
      ('l1', 'Unit 2 (middle)', 'L1', 'dev0 · HQ2606P4NCH · master', 'Stack1 · MUST'),
      ('l3', 'Unit 3 (right)', 'L3', 'dev2 · HQ2605CWUWH', 'Stack2 · Pytes')]
for p, dev, ph, note, bat in MP:
    x = CX[p] - 160
    box(x, 275, 320, 165, '#16263a', 12, f'{p}_ac_in_w', flow_ranges('color', '#3a2a10', '#16263a', '#0f2a1c', 50))
    text(x + 18, 302, f'{dev} · {ph}', 15, 'bold')
    text(x + 18, 322, f'MultiPlus-II 48/5000 · {note}', 11, fill=MUTED)
    text(x + 18, 352, 'AC-in', 13, fill=MUTED)
    value(x + 300, 352, f'{p}_ac_in_w', 'W', 0, 22, anchor='end')
    text(x + 18, 378, 'AC-in voltage', 13, fill=MUTED)
    value(x + 300, 378, f'{p}_ac_in_v', 'V', 1, 14, anchor='end')
    text(x + 18, 400, 'AC-out1', 13, fill=MUTED)
    value(x + 300, 400, f'{p}_ac_out_w', 'W', 0, 14, anchor='end')
    text(x + 18, 428, f'DC: {bat}', 12, 'bold', fill=ORANGE if 'Pytes' in bat else BLUE)

# ---- DC lines: L2 -> Stack1, L1 + L3 -> Stack2 ---------------------------------------------------
line(f'M{CX["l1"]},440 L{CX["l1"]},480', 's1_bat_kw', 0.05, 9)
line(f'M{CX["l2"]},440 L{CX["l2"]},612', 's2_bat_kw', 0.05, 9)
line(f'M{CX["l3"]},440 L{CX["l3"]},612', 's2_bat_kw', 0.05, 9)

# ---- Stack1: MUST, CAN BMS ---------------------------------------------------------------------
x0, y0 = 450, 480
box(x0, y0, 560, 120, '#0f1d33')
text(x0 + 18, y0 + 26, 'Stack1 · MUST · 300 Ah LFP · CAN BMS', 15, 'bold', fill=BLUE)
text(x0 + 350, y0 + 26, 'full at', 12, fill=MUTED)
value(x0 + 395, y0 + 26, 's1_full_at', '', None, 14)
value(x0 + 540, y0 + 26, 's1_chg_avg5_kw', 'kW (5 min)', 2, 11, 'normal', anchor='end', fill=MUTED)
value(x0 + 18, y0 + 72, 's1_soc', '%', 0, 34)
soc_bar(x0 + 120, y0 + 46, 's1_soc', 18, 18)
text(x0 + 18, y0 + 104, '24 h', 12, fill=MUTED)
value(x0 + 52, y0 + 104, 's1_soc_min_24h', '%', 0, 12, 'normal')
value(x0 + 100, y0 + 104, 's1_soc_max_24h', '%', 0, 12, 'normal')
for i, (lab, key, unit, dig) in enumerate([('power', 's1_bat_kw', 'kW', 2), ('voltage', 's1_bat_v', 'V', 2),
                                          ('current', 's1_bat_a', 'A', 1), ('temp', 's1_temp_c', '°C', 1),
                                          ('cell max', 's1_cell_max_v', 'V', 3), ('cell min', 's1_cell_min_v', 'V', 3)]):
    x, y = x0 + 350 + (i % 2) * 105, y0 + 46 + (i // 2) * 20
    text(x, y, lab, 11, fill=MUTED)
    value(x + 100, y, key, unit, dig, 12, anchor='end')
text(x0 + 160, y0 + 104, 'BMS: CVL', 11, fill=MUTED)
value(x0 + 258, y0 + 104, 's1_cvl_v', 'V', 1, 11, 'normal', anchor='end')
text(x0 + 266, y0 + 104, 'CCL', 11, fill=MUTED)
value(x0 + 330, y0 + 104, 's1_ccl_a', 'A', 0, 11, 'normal', anchor='end')
text(x0 + 338, y0 + 104, 'DCL', 11, fill=MUTED)
value(x0 + 400, y0 + 104, 's1_dcl_a', 'A', 0, 11, 'normal', anchor='end')

# ---- Stack2: Pytes (EBox), active BMS for DVCC --------------------------------------------------
x0, y0 = 30, 612
box(x0, y0, 1400, 125, '#2a1f0e')
text(x0 + 18, y0 + 26, 'Stack2 · Pytes (EBox) · 300 Ah LFP 16S · /home/pi/python/ebox_mqtt.py → /home/pi/sofar/ebox', 15, 'bold', fill=ORANGE)
text(x0 + 1090, y0 + 26, 'full at', 12, fill=MUTED)
value(x0 + 1135, y0 + 26, 's2_full_at', '', None, 14)
value(x0 + 1380, y0 + 26, 's2_chg_avg5_kw', 'kW (5 min)', 2, 11, 'normal', anchor='end', fill=MUTED)
value(x0 + 18, y0 + 76, 's2_soc', '%', 0, 34)
soc_bar(x0 + 120, y0 + 50, 's2_soc', 20, 20)
text(x0 + 18, y0 + 108, '24 h', 12, fill=MUTED)
value(x0 + 52, y0 + 108, 's2_soc_min_24h', '%', 0, 12, 'normal')
value(x0 + 100, y0 + 108, 's2_soc_max_24h', '%', 0, 12, 'normal')
text(x0 + 170, y0 + 108, 'cycles (packs 1/2/3)', 12, fill=MUTED)
for i in range(3):
    value(x0 + 330 + i * 45, y0 + 108, f's2_p{i + 1}_cycles', '', 0, 12, anchor='end')
for i, (lab, key, unit, dig) in enumerate([('power', 's2_bat_kw', 'kW', 2), ('voltage', 's2_bat_v', 'V', 2), ('current', 's2_bat_a', 'A', 1),
                                          ('cell max', 's2_cell_max_v', 'V', 3), ('cell min', 's2_cell_min_v', 'V', 3),
                                          ('CVL', 's2_cvl_v', 'V', 1), ('CCL', 's2_ccl_a', 'A', 0), ('DCL', 's2_dcl_a', 'A', 0)]):
    x, y = x0 + 470 + (i % 4) * 230, y0 + 58 + (i // 4) * 28
    text(x, y, lab, 13, fill=MUTED)
    value(x + 200, y, key, unit, dig, 15, anchor='end')
text(x0 + 470, y0 + 112, 'batmonitor (both stacks): discharge stop < 5 % (free at 7 %) · forced grid charge < 3 % until 5 %', 12, fill=MUTED)

# ---- chart tile at the bottom: click opens the Grafana wagodb panel (1 h) in a new tab --------------
GRAFANA_1H = ('https://heissa.de:2999/d/adfjrkc/wagodb?orgId=1&from=now-1h&to=now&timezone=browser'
              '&refresh=1m&viewPanel=panel-2')
box(30, 750, 1400, 40, '#111827', 8)
shape('path', {'d': 'M48,782 L70,774 L88,778 L108,764 L128,770 L150,758 L170,766 L190,760', 'fill': 'none', 'stroke': GREEN, 'stroke-width': 2})
shape('path', {'d': 'M48,784 L72,782 L92,784 L112,778 L132,781 L152,776 L172,779 L190,775', 'fill': 'none', 'stroke': ORANGE, 'stroke-width': 2})
text(210, 776, 'Chart last hour (Grafana wagodb) ▸', 15, 'bold')
text(560, 776, 'click opens heissa.de:2999 in a new tab', 13, fill=MUTED)
click_tile(30, 750, 1400, 40, 'grafana_1h', {'action': 'onOpenTab', 'actparam': GRAFANA_1H, 'actoptions': {'newTab': True}})

# ---- view / project ----------------------------------------------------------------------------
svgcontent = (f'<svg width="{W * SCALE}" height="{H * SCALE}" xmlns="http://www.w3.org/2000/svg" xmlns:svg="http://www.w3.org/2000/svg" '
              'xmlns:xlink="http://www.w3.org/1999/xlink">'
              f'<g><title>Layer 1</title><g id="svg_shift" transform="scale({SCALE})">' + ''.join(svg) + '</g></g></svg>')
title = 'Victron MultiPlus'
view = {'id': 'v_victron', 'name': title, 'profile': {'width': W * SCALE, 'height': H * SCALE, 'bkcolor': '#000000ff', 'margin': 0},
        'items': items, 'variables': {}, 'svgcontent': svgcontent, 'type': 'svg'}
project = {
    'version': '1.01', 'name': title,
    'server': {'id': '0', 'name': 'FUXA Server', 'type': 'FuxaServer', 'property': {}},
    'devices': {DEV: {'id': DEV, 'name': 'victron_api', 'type': 'WebAPI', 'enabled': True, 'polling': 60000,
                      'property': {'address': API, 'method': 'GET', 'format': 'JSON'}, 'tags': tags}},
    'hmi': {'views': [view], 'layout': {'start': 'v_victron', 'navigation': {'mode': 'void', 'type': 'inline', 'items': []},
                                         'header': {'title': title, 'bkcolor': '#000000', 'fontcolor': FG}, 'showdev': False,
                                         'zoom': 'autoresize', 'inputdialog': 'false', 'hidenavigation': True}},
    'charts': [], 'alarms': [], 'notifications': [], 'scripts': [], 'reports': [], 'texts': [], 'plugin': [],
}
json.dump(project, sys.stdout, ensure_ascii=False)
