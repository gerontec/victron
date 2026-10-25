#!/usr/bin/env python3
"""
gen_flows.py - Node-RED flow of the local Venus dashboard (Dashboard 2.0, @flowfuse/node-red-dashboard).

Live fallback of the FUXA view venus.heissa.de that needs neither heissa nor wagodb: values straight from the local
Venus MQTT broker (dbus-flashmq mirrors every dbus path) plus /data/batmonitor/state.json, standard layout, no 1:1
picture. Page: https://<gx>:1881/dashboard/venus

  python3 gen_flows.py > flows.json      then install.sh (copies it to /data/home/nodered/.node-red and restarts)

Flow: keepalive R/<portal>/keepalive (full republish at start, then every 30 s with suppress-republish)
  -> mqtt in N/<portal>/{vebus,battery,grid,system,settings}/... -> store (flow context 'v', key = topic after portal)
  inject 5 s -> file in state.json -> store 'bm'
  inject 2 s -> snapshot (view model + 6 h history at 1/min) -> ui-template
"""
import json
import sys

PORTAL = 'e45f01114b2c'
VEBUS, BAT_A, BAT_B, GRID = 290, 512, 513, 30   # dbus instances on this GX (Speicher A = CAN-bus BMS MUST, B = EBox)
TAB = 'vdash_tab'

TOPICS = [f'N/{PORTAL}/vebus/{VEBUS}/#', f'N/{PORTAL}/battery/+/#', f'N/{PORTAL}/grid/{GRID}/#',
		  f'N/{PORTAL}/system/0/#', f'N/{PORTAL}/settings/0/Settings/CGwacs/#']

STORE = r"""
// N/<portal>/<service>/<instance>/<path> {"value": x} -> flow 'v'[<service>/<instance>/<path>]
const v = flow.get('v') || {};
const key = msg.topic.split('/').slice(2).join('/');
let val = null;
try { val = JSON.parse(msg.payload).value; } catch (e) { return null; }
v[key] = val;
flow.set('v', v);
flow.set('v_ts', Date.now());
return null;
"""

STORE_BM = r"""
if (msg.payload && typeof msg.payload === 'object') flow.set('bm', msg.payload);
return null;
"""

SNAPSHOT = r"""
const v = flow.get('v') || {}, bm = flow.get('bm') || {};
const VB = 'vebus/%(vebus)d/', A = 'battery/%(bat_a)d/', B = 'battery/%(bat_b)d/', G = 'grid/%(grid)d/';
const g = k => (v[k] === undefined ? null : v[k]);
const STATE = {0:'Off',1:'Low power',2:'Fault',3:'Bulk',4:'Absorption',5:'Float',6:'Storage',7:'Equalize',8:'Passthru',
	9:'Inverting',10:'Power assist',11:'Power supply',244:'Sustain',252:'External control'};
const MODE = {1:'Charger only',2:'Inverter only',3:'On',4:'Off'};
const D1 = ['night','wp_on','Bat1>min','Bat1>=release','winter (Oct-Mar)','Bat1 discharging','bat1_first','-'];
const sum = ks => ks.reduce((s, k) => (g(k) === null ? s : s + g(k)), 0);
const phases = [1, 2, 3].map(p => ({
	name: 'L' + p,
	in_v: g(VB + 'Ac/ActiveIn/L' + p + '/V'), in_w: g(VB + 'Ac/ActiveIn/L' + p + '/P'),
	out_w: g(VB + 'Ac/Out/L' + p + '/P'), grid_w: g(G + 'Ac/L' + p + '/Power'),
	sp_w: (bm.sp || {})['L' + p], rule: (bm.rule || {})['L' + p] || '',
}));
function bat(pre, name, title) {
	const fc = (bm.forecast || {})[name] || {}, lim = (bm.bms_limits || {})[name] || {};
	return { title, soc: g(pre + 'Soc'), v: g(pre + 'Dc/0/Voltage'), a: g(pre + 'Dc/0/Current'), w: g(pre + 'Dc/0/Power'),
		t: g(pre + 'Dc/0/Temperature'), cmin: g(pre + 'System/MinCellVoltage'), cmax: g(pre + 'System/MaxCellVoltage'),
		cvl: g(pre + 'Info/MaxChargeVoltage'), ccl: g(pre + 'Info/MaxChargeCurrent'), dcl: g(pre + 'Info/MaxDischargeCurrent'),
		prot: (bm.prot || {})[name], force: (bm.force || {})[name], low: (bm.low || {})[name],
		full_at: fc.full_at ? new Date(fc.full_at * 1000).toTimeString().slice(0, 5) : null, soc_sunset: fc.soc_sunset,
		avg5: fc.avg5_w };
}
const leds = ['Mains','Bulk','Absorption','Float','Inverter','Overload','LowBattery','Temperature']
	.map(n => ({ name: n, v: g(VB + 'Leds/' + n) }));
const d1 = bm.d1_id === undefined ? null : D1.filter((t, i) => bm.d1_id & (1 << i));
const now = Date.now();
const snap = {
	ts: now, age_s: Math.round((now - (flow.get('v_ts') || 0)) / 1000), bm_age_s: bm.ts ? Math.round(now / 1000 - bm.ts) : null,
	system_state: STATE[g('system/0/SystemState/State')] || g('system/0/SystemState/State'),
	vebus_state: STATE[g(VB + 'State')] || g(VB + 'State'), vebus_mode: MODE[g(VB + 'Mode')] || g(VB + 'Mode'),
	vebus_error: g(VB + 'VebusError'), dc_v: g(VB + 'Dc/0/Voltage'), f_hz: g(VB + 'Ac/ActiveIn/L1/F'),
	hub4mode: g('settings/0/Settings/CGwacs/Hub4Mode'), max_charge_w: g('settings/0/Settings/CGwacs/MaxChargePower'),
	grid_w: g(G + 'Ac/Power'), ac_in_w: sum(phases.map((p, i) => VB + 'Ac/ActiveIn/L' + (i + 1) + '/P')),
	ac_out_w: sum(phases.map((p, i) => VB + 'Ac/Out/L' + (i + 1) + '/P')), surplus_w: bm.surplus_w,
	phases, leds, a: bat(A, 'STACK1_MUST', 'Speicher A · MUST (CAN-bus BMS)'), b: bat(B, 'STACK2_PYTES', 'Speicher B · Pytes (EBox)'),
	bm: { version: bm.version, season: bm.season, season_source: bm.season_source, src: (bm.chain || {}).src,
		method: (bm.forecast || {}).method, kt: (bm.forecast || {}).kt_now, pv_rest_kwh: (bm.forecast || {}).pv_rest_kwh,
		pv_model_w: (bm.forecast || {}).pv_model_w, d1_id: bm.d1_id, d1, balance_lead: bm.balance_lead,
		ladesperre: (bm.ladesperre || {}).active, bat1_first: bm.bat1_first, wp_max_w: bm.wp_max_w },
};
// history: one point per minute, 6 h
const h = flow.get('hist') || [];
const minute = Math.floor(now / 60000);
if (!h.length || h[h.length - 1].m !== minute) {
	h.push({ m: minute, soc_a: snap.a.soc, soc_b: snap.b.soc, grid: snap.grid_w, bat: (snap.a.w || 0) + (snap.b.w || 0) });
	while (h.length > 360) h.shift();
	flow.set('hist', h);
}
snap.hist = h;
msg.payload = snap;
return msg;
""" % dict(vebus=VEBUS, bat_a=BAT_A, bat_b=BAT_B, grid=GRID)

TEMPLATE = r"""<template>
<div class="vd">
  <div class="hdr">
    <span class="pill" :class="d.age_s > 30 ? 'bad' : 'ok'">{{ d.age_s > 30 ? 'MQTT stale ' + d.age_s + ' s' : 'live' }}</span>
    <span>System: <b>{{ d.system_state }}</b></span>
    <span>VE.Bus: <b>{{ d.vebus_state }}</b> · {{ d.vebus_mode }}</span>
    <span :class="d.vebus_error ? 'bad' : ''">Error {{ d.vebus_error }}</span>
    <span>Hub4Mode {{ d.hub4mode }}</span>
    <span>DC {{ n(d.dc_v, 2) }} V · {{ n(d.f_hz, 2) }} Hz</span>
  </div>

  <div class="tiles">
    <div class="tile"><div class="k">Grid (PCC)</div><div class="v">{{ kw(d.grid_w) }}</div><div class="s">{{ d.grid_w > 0 ? 'import' : 'export' }}</div></div>
    <div class="tile"><div class="k">Multis AC-in</div><div class="v">{{ kw(d.ac_in_w) }}</div><div class="s">+ = charging from AC</div></div>
    <div class="tile"><div class="k">Multis AC-out</div><div class="v">{{ kw(d.ac_out_w) }}</div></div>
    <div class="tile"><div class="k">Surplus (batmonitor)</div><div class="v">{{ kw(d.surplus_w) }}</div></div>
    <div class="tile"><div class="k">SoC A / B</div><div class="v">{{ n(d.a && d.a.soc, 1) }} / {{ n(d.b && d.b.soc, 1) }} %</div></div>
  </div>

  <h3>Phases</h3>
  <table>
    <tr><th></th><th>AC-in V</th><th>AC-in W</th><th>AC-out W</th><th>Grid W</th><th>Setpoint W</th><th>batmonitor rule</th></tr>
    <tr v-for="p in d.phases || []" :key="p.name">
      <td><b>{{ p.name }}</b></td><td>{{ n(p.in_v, 1) }}</td><td>{{ n(p.in_w, 0) }}</td><td>{{ n(p.out_w, 0) }}</td>
      <td>{{ n(p.grid_w, 0) }}</td><td>{{ n(p.sp_w, 0) }}</td><td class="mono">{{ p.rule }}</td>
    </tr>
  </table>

  <div class="cols">
    <div class="card" v-for="s in stacks" :key="s.title">
      <h3>{{ s.title }}</h3>
      <div class="bar"><div :style="{ width: (s.soc || 0) + '%' }"></div><span>{{ n(s.soc, 1) }} %</span></div>
      <table class="kv">
        <tr><td>Power</td><td>{{ n(s.w, 0) }} W ({{ n(s.v, 2) }} V · {{ n(s.a, 1) }} A)</td></tr>
        <tr><td>5 min avg</td><td>{{ n(s.avg5, 0) }} W</td></tr>
        <tr><td>Cells</td><td>{{ n(s.cmin, 3) }} … {{ n(s.cmax, 3) }} V</td></tr>
        <tr><td>BMS CVL / CCL / DCL</td><td>{{ n(s.cvl, 2) }} V · {{ n(s.ccl, 0) }} A · {{ n(s.dcl, 0) }} A</td></tr>
        <tr v-if="s.t !== null"><td>Temperature</td><td>{{ n(s.t, 1) }} °C</td></tr>
        <tr><td>Full at / SoC sunset</td><td>{{ s.full_at || '–' }} · {{ n(s.soc_sunset, 1) }} %</td></tr>
        <tr><td>Flags</td><td>
          <span class="pill" :class="s.prot ? 'warn' : 'off'">discharge blocked</span>
          <span class="pill" :class="s.force ? 'warn' : 'off'">forced charge</span>
          <span class="pill" :class="s.low ? 'bad' : 'off'">LOW</span></td></tr>
      </table>
    </div>
  </div>

  <div class="cols">
    <div class="card">
      <h3>batmonitor {{ d.bm && d.bm.version }}</h3>
      <table class="kv" v-if="d.bm">
        <tr><td>Season / source</td><td>{{ d.bm.season }} ({{ d.bm.season_source }}) · SRC {{ d.bm.src }}</td></tr>
        <tr><td>Forecast</td><td>{{ d.bm.method }} · kt {{ n(d.bm.kt, 2) }} · PV model {{ n(d.bm.pv_model_w, 0) }} W · rest {{ n(d.bm.pv_rest_kwh, 1) }} kWh</td></tr>
        <tr><td>D1 cell (bm_d1_id {{ d.bm.d1_id }})</td><td><span class="pill on" v-for="t in d.bm.d1 || []" :key="t">{{ t }}</span></td></tr>
        <tr><td>Flags</td><td>
          <span class="pill" :class="d.bm.ladesperre ? 'warn' : 'off'">charge block</span>
          <span class="pill" :class="d.bm.bat1_first ? 'on' : 'off'">Bat1 first</span>
          <span class="pill" :class="d.bm.wp_max_w >= 0 ? 'warn' : 'off'">WP max {{ d.bm.wp_max_w >= 0 ? d.bm.wp_max_w + ' W' : '–' }}</span>
          <span class="pill" :class="d.bm.balance_lead ? 'warn' : 'off'">lead {{ d.bm.balance_lead || '–' }}</span></td></tr>
        <tr><td>state.json age</td><td :class="d.bm_age_s > 30 ? 'bad' : ''">{{ d.bm_age_s }} s</td></tr>
      </table>
    </div>
    <div class="card">
      <h3>Front panel LEDs (master L1)</h3>
      <div class="leds">
        <span v-for="l in d.leds || []" :key="l.name" class="led" :class="'l' + (l.v === null ? 'x' : l.v)">{{ l.name }}</span>
      </div>
      <div class="s">on · blinking · off</div>
    </div>
  </div>

  <h3>Last 6 h</h3>
  <div class="cols">
    <div class="card"><div class="k">SoC A (blue) / B (orange), 0–100 %</div>
      <svg viewBox="0 0 360 100" preserveAspectRatio="none" class="spark">
        <polyline :points="line('soc_a', 0, 100)" class="c1"/><polyline :points="line('soc_b', 0, 100)" class="c2"/>
      </svg></div>
    <div class="card"><div class="k">Grid (blue) / batteries DC (orange), W · {{ range.lo }} … {{ range.hi }}</div>
      <svg viewBox="0 0 360 100" preserveAspectRatio="none" class="spark">
        <line x1="0" x2="360" :y1="zero" :y2="zero" class="z"/>
        <polyline :points="line('grid', range.lo, range.hi)" class="c1"/><polyline :points="line('bat', range.lo, range.hi)" class="c2"/>
      </svg></div>
  </div>
  <div class="s">updated {{ new Date(d.ts || 0).toLocaleTimeString() }} · source: local Venus MQTT + /data/batmonitor/state.json</div>
</div>
</template>

<script>
export default {
  computed: {
    d () { return (this.msg && this.msg.payload) || {} },
    stacks () { return [this.d.a, this.d.b].filter(Boolean) },
    range () {
      const h = this.d.hist || []
      let lo = 0, hi = 0
      h.forEach(p => { ['grid', 'bat'].forEach(k => { if (p[k] !== null) { lo = Math.min(lo, p[k]); hi = Math.max(hi, p[k]) } }) })
      if (hi - lo < 1000) { hi += 500; lo -= 500 }
      return { lo: Math.round(lo), hi: Math.round(hi) }
    },
    zero () { return 100 - (0 - this.range.lo) / (this.range.hi - this.range.lo) * 100 }
  },
  methods: {
    n (x, d) { return (x === null || x === undefined || isNaN(x)) ? '–' : Number(x).toFixed(d) },
    kw (w) { return (w === null || w === undefined) ? '–' : (w / 1000).toFixed(2) + ' kW' },
    line (k, lo, hi) {
      const h = this.d.hist || []
      const off = 360 - h.length
      return h.map((p, i) => p[k] === null ? '' : (off + i) + ',' + (100 - (p[k] - lo) / (hi - lo) * 100).toFixed(1)).join(' ')
    }
  }
}
</script>

<style>
.vd { font-size: 14px; width: 100%; }
.vd h3 { font-size: 15px; margin: 14px 0 6px; }
.vd .hdr { display: flex; flex-wrap: wrap; gap: 6px 16px; align-items: center; }
.vd .tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; margin-top: 10px; }
.vd .tile, .vd .card { border: 1px solid rgba(128,128,128,.35); border-radius: 6px; padding: 8px 10px; }
.vd .tile .k, .vd .k { font-size: 12px; opacity: .7; }
.vd .tile .v { font-size: 22px; font-weight: 600; }
.vd .s { font-size: 12px; opacity: .6; margin-top: 4px; }
.vd .cols { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 10px; margin-top: 10px; }
.vd table { border-collapse: collapse; width: 100%; }
.vd th, .vd td { text-align: left; padding: 3px 6px; border-top: 1px solid rgba(128,128,128,.2); font-variant-numeric: tabular-nums; }
.vd th { font-size: 12px; opacity: .7; font-weight: 500; }
.vd .kv td:first-child { opacity: .7; white-space: nowrap; width: 40%; }
.vd .mono { font-family: ui-monospace, monospace; font-size: 12px; }
.vd .pill { display: inline-block; font-size: 11px; padding: 1px 7px; border-radius: 9px; margin: 1px 3px 1px 0; }
.vd .pill.ok, .vd .pill.on { background: #1d7a3a22; color: #1d7a3a; }
.vd .pill.warn { background: #9a5b0022; color: #b36b00; }
.vd .pill.bad, .vd .bad { color: #c0302a; }
.vd .pill.bad { background: #c0302a22; }
.vd .pill.off { opacity: .35; border: 1px solid rgba(128,128,128,.4); }
.vd .bar { position: relative; height: 22px; border-radius: 5px; background: rgba(128,128,128,.2); overflow: hidden; margin-bottom: 6px; }
.vd .bar div { height: 100%; background: #2f8f4e; }
.vd .bar span { position: absolute; left: 8px; top: 2px; font-weight: 600; }
.vd .leds { display: flex; flex-wrap: wrap; gap: 6px; }
.vd .led { font-size: 12px; padding: 3px 8px; border-radius: 4px; border: 1px solid rgba(128,128,128,.4); opacity: .45; }
.vd .led.l1 { background: #2f8f4e; color: #fff; opacity: 1; }
.vd .led.l2, .vd .led.l3 { background: #2f8f4e; color: #fff; opacity: 1; animation: vdblink 1s steps(2) infinite; }
@keyframes vdblink { 50% { opacity: .3 } }
.vd .spark { width: 100%; height: 110px; }
.vd .spark polyline { fill: none; stroke-width: 1.5; vector-effect: non-scaling-stroke; }
.vd .spark .c1 { stroke: #2a6fdb; } .vd .spark .c2 { stroke: #e07b1f; }
.vd .spark .z { stroke: rgba(128,128,128,.5); stroke-dasharray: 3 3; vector-effect: non-scaling-stroke; }
</style>
"""


def node(**kw):
	kw.setdefault('z', TAB)
	return kw


def main():
	nodes = [
		{'id': TAB, 'type': 'tab', 'label': 'Venus dashboard', 'disabled': False,
		 'info': 'Generated by victron venus-addons/nodered-dashboard/gen_flows.py - edit there, not here.'},
		{'id': 'vdash_broker', 'type': 'mqtt-broker', 'name': 'Venus local', 'broker': '127.0.0.1', 'port': '1883',
		 'clientid': 'nodered-vdash', 'autoConnect': True, 'usetls': False, 'protocolVersion': '4', 'keepalive': '60',
		 'cleansession': True, 'autoUnsubscribe': True, 'birthTopic': '', 'closeTopic': '', 'willTopic': ''},
		{'id': 'vdash_base', 'type': 'ui-base', 'name': 'Venus', 'path': '/dashboard', 'includeClientData': True,
		 'acceptsClientConfig': ['ui-notification', 'ui-control'], 'showPathInSidebar': False, 'showPageTitle': True,
		 'navigationStyle': 'none', 'titleBarStyle': 'default'},
		{'id': 'vdash_theme', 'type': 'ui-theme', 'name': 'Venus', 'colors': {'surface': '#1f5fa8', 'primary': '#1f5fa8',
		 'bgPage': '#f4f4f2', 'groupBg': '#ffffff', 'groupOutline': '#dddcd6'},
		 'sizes': {'density': 'default', 'pagePadding': '12px', 'groupGap': '12px', 'groupBorderRadius': '6px', 'widgetGap': '12px'}},
		{'id': 'vdash_page', 'type': 'ui-page', 'name': 'Venus Lenggries', 'ui': 'vdash_base', 'path': '/venus', 'icon': 'home',
		 'layout': 'grid', 'theme': 'vdash_theme', 'order': 1, 'className': '', 'visible': 'true', 'disabled': 'false'},
		{'id': 'vdash_group', 'type': 'ui-group', 'name': 'Live', 'page': 'vdash_page', 'width': '12', 'height': '1',
		 'order': 1, 'showTitle': False, 'className': '', 'visible': 'true', 'disabled': 'false', 'groupType': 'default'},

		node(id='vdash_ka_full', type='inject', name='keepalive: full republish', props=[{'p': 'payload'}],
			 repeat='', crontab='', once=True, onceDelay='3', topic='', payload='', payloadType='str',
			 x=170, y=60, wires=[['vdash_ka_out']]),
		node(id='vdash_ka', type='inject', name='keepalive 30 s', props=[{'p': 'payload'}], repeat='30', crontab='',
			 once=False, onceDelay='', topic='', payload='{"keepalive-options":["suppress-republish"]}', payloadType='str',
			 x=150, y=100, wires=[['vdash_ka_out']]),
		node(id='vdash_ka_out', type='mqtt out', name='', topic=f'R/{PORTAL}/keepalive', qos='0', retain='false',
			 broker='vdash_broker', x=420, y=80, wires=[]),
	]
	for i, t in enumerate(TOPICS):
		nodes.append(node(id=f'vdash_in{i}', type='mqtt in', name='', topic=t, qos='0', datatype='utf8',
						  broker='vdash_broker', nl=False, rap=True, rh=0, inputs=0, x=170, y=160 + 40 * i,
						  wires=[['vdash_store']]))
	nodes += [
		node(id='vdash_store', type='function', name='store dbus values', func=STORE.strip(), outputs=1,
			 timeout=0, noerr=0, initialize='', finalize='', libs=[], x=470, y=220, wires=[[]]),
		node(id='vdash_bm_tick', type='inject', name='every 5 s', props=[{'p': 'payload'}], repeat='5', crontab='',
			 once=True, onceDelay='2', topic='', payload='', payloadType='date', x=140, y=400, wires=[['vdash_bm_file']]),
		node(id='vdash_bm_file', type='file in', name='batmonitor state.json', filename='/data/batmonitor/state.json',
			 filenameType='str', format='utf8', chunk=False, sendError=False, encoding='none', allProps=False,
			 x=340, y=400, wires=[['vdash_bm_json']]),
		node(id='vdash_bm_json', type='json', name='', property='payload', action='obj', pretty=False,
			 x=530, y=400, wires=[['vdash_bm_store']]),
		node(id='vdash_bm_store', type='function', name='store batmonitor', func=STORE_BM.strip(), outputs=1,
			 timeout=0, noerr=0, initialize='', finalize='', libs=[], x=700, y=400, wires=[[]]),
		node(id='vdash_tick', type='inject', name='every 2 s', props=[{'p': 'payload'}], repeat='2', crontab='',
			 once=True, onceDelay='4', topic='', payload='', payloadType='date', x=140, y=480, wires=[['vdash_snap']]),
		node(id='vdash_snap', type='function', name='snapshot + history', func=SNAPSHOT.strip(), outputs=1,
			 timeout=0, noerr=0, initialize='', finalize='', libs=[], x=340, y=480, wires=[['vdash_ui']]),
		node(id='vdash_ui', type='ui-template', group='vdash_group', page='', ui='', name='Venus live', order=1,
			 width='0', height='0', head='', format=TEMPLATE, storeOutMessages=True, passthru=False,
			 resendOnRefresh=True, templateScope='local', className='', x=560, y=480, wires=[[]]),
	]
	json.dump(nodes, sys.stdout, indent=1, ensure_ascii=False)


if __name__ == '__main__':
	main()
