#!/usr/bin/env python3
"""
batmonitor.py - battery protection and charge / discharge control for a three-phase MultiPlus system whose phases sit
on separate battery banks (Stack1 MUST on L1, Stack2 Pytes on L2 + L3). Python port of c/batmonitor.c 0.51-c.

This file is the shell: it reads the inputs (D-Bus of Venus, MQTT on .218), calls bm_logic.bm_step once per cycle
and writes the outputs (ESS setpoints per phase, state file, log, PI shadow CSV, DO4 pulse, peak model). The rules
themselves are in bm_logic.py (a 1:1 port of c/bm_logic.c, proven byte-identical by `make py-compare`); the MQTT
payload checks in bm_parse.py. The C version is the one that runs; this port is its readable reference and the
fallback the service starts with BATMONITOR_PYTHON=1.

Environment (service/run exports /data/batmonitor/env):
  BATMONITOR_LIVE=1            writes the setpoints; without: dry run (log + state file only)
  BATMONITOR_LADESPERRE=0      switches the summer charge block off
  BATMONITOR_PI=1              arms the PI prototype (else it only logs to the PI shadow CSV)
  BATMONITOR_FORECAST=0        ignores batmonitor/forecast
  BATMONITOR_<PARAM>=value     overrides a parameter (bm_logic.BM_PARAMS, logged at start as "params")
  BATMONITOR_MQTT_HOST         default 192.168.178.218
  BATMONITOR_STATE_FILE        default /data/batmonitor/state.json (another path for a dry test run)
  BATMONITOR_PI_SHADOW_FILE    default /data/batmonitor/pi_shadow.csv (Python only: keeps a test run apart)
"""
import json
import os
import signal
import sys
import threading
import time

import dbus
import paho.mqtt.client as mqtt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bm_logic as L  # noqa: E402
import bm_parse  # noqa: E402

VERSION = "0.51-py"
INVERTER_TOPIC = "inverter/power_grid_exchange/json"
R290_TOPIC = "r290/heatpump/all"
AUSSEN_TOPIC = "aussen/temp"
WP_TOPIC = "em0/power"                     # SDM72D heat pump W (sdm72d.py on .218, once a minute)
SEASON_TOPIC = "batmonitor/season"         # season.py on .218, hourly
FORECAST_TOPIC = "batmonitor/forecast"     # forecast.py on .218, hourly
PEAK_MODEL_TOPIC = "batmonitor/peak_model" # out, retained, every PEAK_MODEL_SECONDS: for r290_boost.py
PEAK_MODEL_SECONDS = 60.0
DO4_LAST_TOPIC = "batmonitor/do4_last"     # out, retained: {"ts", "pcc_w"} of the last DO4 pulse (r290_boost, hantech)
DO4_TOPIC = "pv_relay/DO4"                 # out, not retained: "1" for DO4_PULSE_S, then "0" (FoxESS shedding)
TZ_BERLIN = "CET-1CEST,M3.5.0,M10.5.0/3"   # Europe/Berlin without a zoneinfo file
SETPOINT_MAX_AGE = 90.0
LOG_SECONDS = 60.0
ETA_WINDOW = 300.0
ETA_MIN_W = 50.0
PI_SHADOW_MAX = 8000000                    # bytes, then renamed to .1
BUSITEM = "com.victronenergy.BusItem"


def mono():
    return time.monotonic()


def log(*a):
    print("INFO " + " ".join(str(x) for x in a), flush=True)


def loge(*a):
    print("ERROR " + " ".join(str(x) for x in a), flush=True)


def c_round(x):
    """C round(): half away from zero, as the C version writes the state file"""
    return L.lround(x) if x == x else 0


class Batmonitor:
    def __init__(self):
        self.live = os.environ.get("BATMONITOR_LIVE") == "1"
        self.cfg = L.Cfg()
        self.cfg.ladesperre = int(os.environ.get("BATMONITOR_LADESPERRE", "1") == "1")
        self.cfg.pi_armed = int(os.environ.get("BATMONITOR_PI") == "1")
        self.cfg.forecast = int(os.environ.get("BATMONITOR_FORECAST", "1") == "1")
        self.state_file = os.environ.get("BATMONITOR_STATE_FILE") or "/data/batmonitor/state.json"
        self.pi_shadow_file = os.environ.get("BATMONITOR_PI_SHADOW_FILE") or "/data/batmonitor/pi_shadow.csv"
        self.host = os.environ.get("BATMONITOR_MQTT_HOST") or "192.168.178.218"
        self.st = L.State()
        self.out = L.Out()                 # result of the last bm_step
        self.in_last = L.In()              # its input (BMS limits, AC-out for the state file)
        self.fc_out = L.FcOut()
        self.full_reached = [0] * L.NBANK  # first cycle with SoC 100, 0 = below 100
        self.hist = [[] for _ in range(L.NBANK)]   # (t, BMS power) over ETA_WINDOW per stack
        self.setpoint_time = 0.0
        self.hub4mode_set = False
        self.do4_off_at = 0.0
        self.last_rules = None
        self.last_log = 0.0
        self.last_peak_model = 0.0
        self.last_inv_err = 0.0
        self.stop = False
        # MQTT inputs, written by the paho thread under mq_lock
        self.mq_lock = threading.Lock()
        self.mq = dict(have_pcc=0, have_pv=0, pcc=0.0, pv=0.0, bat1=0.0, pcc_avg5=0.0, bat1_avg5=0.0, inv_time=0.0,
                       have_soc_bat1=0, soc_bat1=0.0, r290_hz=0, r290_time=0.0, aussen=0.0, aussen_time=0.0,
                       wp=0.0, wp_time=0.0, season=0, season_ts=0.0, fc_target=0.0, fc_ts=0.0, fc_corr=0.0,
                       fc_slots=[])
        self.bus = dbus.SystemBus()
        self.mqttc = None

    # ---- parameters ----------------------------------------------------------------------------------------
    def load_params(self):
        """BATMONITOR_<NAME> overrides the default; out of range or not a number -> default stays"""
        parts = []
        for i, p in enumerate(L.BM_PARAMS):
            v = os.environ.get("BATMONITOR_" + p.name)
            was_set = False
            if v:
                try:
                    x = float(v)
                    ok = self.cfg.set_param(i, x) == 0
                except ValueError:
                    ok = False
                if ok:
                    was_set = True
                else:
                    loge("BATMONITOR_%s=%s ignored (allowed %g..%g %s), default %g"
                         % (p.name, v, p.min, p.max, p.unit, p.default))
            parts.append("%s=%g%s" % (p.name, getattr(self.cfg, p.field), "*" if was_set else ""))
        log("params (* = from env): " + " ".join(parts))

    # ---- D-Bus ---------------------------------------------------------------------------------------------
    def get(self, service, path):
        """GetValue of a Victron BusItem as float, None = missing service / path, empty array or no number"""
        if not service:
            return None
        try:
            v = self.bus.get_object(service, path, introspect=False).GetValue(dbus_interface=BUSITEM, timeout=2.0)
        except dbus.exceptions.DBusException:
            return None
        if isinstance(v, (dbus.String, dbus.Array, dbus.Dictionary, dbus.Struct, str, list, dict)):
            return None                     # empty array = invalid, strings are no numbers
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def set_int(self, service, path, value):
        if not self.live or not service:
            return
        try:
            self.bus.get_object(service, path, introspect=False).SetValue(
                dbus.Int32(value, variant_level=1), dbus_interface=BUSITEM, timeout=2.0)
        except dbus.exceptions.DBusException as e:
            loge("SetValue %s %s: %s" % (service, path, e))

    def vebus(self):
        """first com.victronenergy.vebus.* on the bus, None if none"""
        try:
            names = self.bus.list_names()
        except dbus.exceptions.DBusException:
            return None
        for n in names:
            if str(n).startswith("com.victronenergy.vebus."):
                return str(n)
        return None

    # ---- MQTT (paho thread) --------------------------------------------------------------------------------
    def on_connect(self, client, userdata, flags, rc, properties=None):
        log("MQTT connected (%s)" % rc)
        for t in (INVERTER_TOPIC, R290_TOPIC, AUSSEN_TOPIC, WP_TOPIC, SEASON_TOPIC, FORECAST_TOPIC):
            client.subscribe(t, 0)

    def on_message(self, client, userdata, msg):
        text = msg.payload[:2047].decode("utf-8", "replace")
        topic = msg.topic
        if topic == WP_TOPIC:
            v = bm_parse.parse_number(text, -1000.0, 30000.0)
            if v is not None:
                with self.mq_lock:
                    self.mq.update(wp=v, wp_time=mono())
            return
        if topic == AUSSEN_TOPIC:
            v = bm_parse.parse_number(text, -40.0, 55.0)
            if v is not None:
                with self.mq_lock:
                    self.mq.update(aussen=v, aussen_time=mono())
            return
        # a sample counts only with valid numbers; a rejected one does not refresh the timestamp
        if topic == INVERTER_TOPIC:
            s = bm_parse.parse_inverter(text)
            if s is None:
                if mono() - self.last_inv_err > 300.0:
                    loge("inverter sample rejected (PCC / Power_Bat1 missing or invalid): %.200s" % text)
                    self.last_inv_err = mono()
                return
            with self.mq_lock:
                self.mq.update(s, have_pcc=1, inv_time=mono())
            return
        if topic == R290_TOPIC:
            hz = bm_parse.parse_r290(text)
            if hz is not None:
                with self.mq_lock:
                    self.mq.update(r290_hz=hz, r290_time=mono())
            return
        try:
            d = json.loads(text)
        except ValueError:
            d = None
        if not isinstance(d, dict):
            loge("bad payload on %s" % topic)
            return
        if topic == FORECAST_TOPIC:
            slots = []
            for sl in d.get("kt_slots") or []:
                if (len(slots) < L.BM_FC_SLOTS and isinstance(sl, list) and len(sl) >= 2
                        and _number(sl[0]) is not None and _number(sl[1]) is not None):
                    slots.append((int(sl[0]), float(sl[1])))
            with self.mq_lock:
                self.mq.update(fc_target=_number(d.get("target_soc"), -1), fc_ts=_number(d.get("ts"), 0),
                               fc_corr=_number(d.get("correction"), 0), fc_slots=slots)
            log("forecast: %s" % text)
        elif topic == SEASON_TOPIC:
            mode = d.get("mode")
            season = {"summer": L.BM_SUMMER, "winter": L.BM_WINTER, "transition": L.BM_TRANSITION}.get(mode, 0)
            with self.mq_lock:
                self.mq.update(season=season, season_ts=_number(d.get("ts"), 0))
            log("season: %s (%s)" % (mode if season else "?", text))

    def mqtt_start(self):
        try:
            c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)          # paho 2.x
        except AttributeError:
            c = mqtt.Client()                                          # paho 1.x
        # random client id: Pi and NCR fallback share the broker, a fixed id would make them kick each other off
        c.on_connect = self.on_connect
        c.on_message = self.on_message
        c.reconnect_delay_set(2, 30)
        try:
            c.connect_async(self.host, 1883, 60)
        except OSError as e:
            loge("MQTT connect to %s failed, retrying in the loop: %s" % (self.host, e))
        c.loop_start()
        self.mqttc = c

    def publish(self, topic, payload, qos, retain):
        if self.mqttc is None:
            return False
        return self.mqttc.publish(topic, payload, qos, retain).rc == mqtt.MQTT_ERR_SUCCESS

    def do4_publish(self, v):
        """DO4 pulse edge (D5): QoS 1, not retained, so a reconnecting relay never sees an old "1" """
        if not self.publish(DO4_TOPIC, v, 1, False):
            loge("DO4=%s publish failed" % v)

    def publish_peak_model(self, now):
        """the charge block's clear-sky peak model, retained, once a minute (for r290_boost.py)"""
        if self.last_peak_model > 0 and now - self.last_peak_model < PEAK_MODEL_SECONDS:
            return
        o, st = self.out, self.st
        payload = ('{"ts":%d,"peak_h":%d,"win_end_h":%d,"ratio":%.3f,"ratio_now":%.3f,"dc_expected_w":%.0f,'
                   '"noon_h":%.2f,"peak_today":%d,"cap_active":%d,"ladesperre_active":%d,"version":"%s"}'
                   % (int(time.time()), o.ls.peak_h, o.ls.win_end_h, o.ls.ratio, o.ls.ratio_now, o.ls.dc, o.ls.noon_h,
                      st.peak_today, o.ls.cap, o.ls.active, VERSION))
        if self.publish(PEAK_MODEL_TOPIC, payload, 1, True):
            self.last_peak_model = now

    # ---- forecast and outputs ------------------------------------------------------------------------------
    def sample_power(self):
        now = mono()
        for b, bank in enumerate(L.BM_BANKS):
            pw = self.get(bank.service, "/Dc/0/Power")
            if pw is not None:
                self.hist[b].append((now, pw))
            self.hist[b] = [(t, p) for t, p in self.hist[b] if now - t <= ETA_WINDOW][-128:]

    def avg_power(self, b):
        h = self.hist[b]
        return (len(h) > 0, sum(p for _, p in h) / len(h) if h else 0.0)

    def full_forecast(self, b):
        """average charge power over ETA_WINDOW, then the weather forecast (by day) or, at night, the time 100 % SoC is
        reached at that power. Returns (has_avg, avg, full_at unix or 0)"""
        has, avg = self.avg_power(b)
        if self.full_reached[b]:
            return has, avg, self.full_reached[b]
        if self.fc_out.soc_sunset[b] >= 0:
            return has, avg, self.fc_out.full_at[b]
        st = self.st
        if has and st.soc_ok[b] and avg >= ETA_MIN_W and st.soc[b] < 100:
            return has, avg, int(time.time() + (100 - st.soc[b]) / 100 * L.BM_BANKS[b].capacity_wh / avg * 3600)
        return has, avg, 0

    def run_full_forecast(self, inp):
        fi = L.FcIn()
        fi.t = inp.t
        fi.have_pv = int(inp.have_pv and inp.now - inp.inv_time < 180.0)
        fi.pv_sofar = inp.pv
        fi.lead = self.st.lead
        for b in range(L.NBANK):
            fi.volt_ok[b], fi.volt[b] = inp.volt_ok[b], inp.volt[b]
            fi.soc_ok[b], fi.soc[b] = self.st.soc_ok[b], self.st.soc[b]
            fi.has_avg[b], fi.avg[b] = self.avg_power(b)
        with self.mq_lock:
            if self.mq["fc_ts"] > 0 and inp.t - int(self.mq["fc_ts"]) < 3 * 3600:     # as FC_MAX_AGE
                fi.corr = self.mq["fc_corr"]
                fi.slots = list(self.mq["fc_slots"])
        self.fc_out = L.bm_full_forecast(self.cfg, fi)
        for b in range(L.NBANK):
            if not self.st.soc_ok[b]:
                continue
            if self.st.soc[b] < 100:
                self.full_reached[b] = 0
            elif not self.full_reached[b]:
                self.full_reached[b] = inp.t

    def write_state(self):
        o, st, fc, il = self.out, self.st, self.fc_out, self.in_last
        forecast = {}
        for b, bank in enumerate(L.BM_BANKS):
            has, avg, full = self.full_forecast(b)
            forecast[bank.name] = {
                "avg5_w": c_round(avg) if has else None,
                "full_at": full or None,
                "soc_sunset": c_round(fc.soc_sunset[b] * 10) / 10 if fc.soc_sunset[b] >= 0 else None,
            }
        forecast["method"] = ((("weather" if fc.weather else "months")
                               if fc.soc_sunset[0] >= 0 or fc.soc_sunset[1] >= 0 else "linear"))
        forecast["anchor"] = c_round(fc.anchor * 100) / 100
        forecast["kt_now"] = c_round(fc.kt_now * 1000) / 1000
        forecast["pv_model_w"] = c_round(fc.pv_now_w)
        forecast["pv_rest_kwh"] = c_round(fc.rest_kwh * 10) / 10
        js = {
            "ts": int(time.time()),
            "version": VERSION,
            "sp": {L.BM_PH[p]: o.sp[p] for p in range(L.NPH)},
            "rule": {L.BM_PH[p]: o.why[p][:48] for p in range(L.NPH)},
            "balance_lead": L.BM_BANKS[st.lead].name if st.lead >= 0 else None,
            "surplus_w": c_round(o.surplus),
            "prot": {bank.name: st.prot[b] for b, bank in enumerate(L.BM_BANKS)},
            "force": {bank.name: st.force[b] for b, bank in enumerate(L.BM_BANKS)},
            "forecast": forecast,
            "charge_cap_w": {L.BM_PH[p]: c_round(o.chg_cap[p]) for p in range(L.NPH)},
            "ac_out_w": {L.BM_PH[p]: (c_round(il.ac_out[p]) if il.ac_out_ok[p] else None) for p in range(L.NPH)},
            "discharge_cap_w": {L.BM_PH[p]: c_round(o.dis_cap[p]) for p in range(L.NPH)},
            "bms_limits": {bank.name: ({"ccl_a": il.ccl[b], "dcl_a": il.dcl[b]} if il.lim_ok[b]
                                       else {"ccl_a": None, "dcl_a": None})
                           for b, bank in enumerate(L.BM_BANKS)},
            "ladesperre": {"active": o.ls.active, "cap_active": o.ls.cap, "dc_expected_w": c_round(o.ls.dc),
                           "ratio": c_round(o.ls.ratio * 1000) / 1000, "ratio_now": c_round(o.ls.ratio_now * 1000) / 1000,
                           "peak_h": o.ls.peak_h, "win_end_h": o.ls.win_end_h, "noon_h": c_round(o.ls.noon_h * 100) / 100,
                           "peak_today": st.peak_today},
            "season": "winter" if o.season == L.BM_WINTER else "transition" if o.season == L.BM_TRANSITION else "summer",
            "season_source": "measured" if o.season_measured else "months",
            "forecast_rule": {"active": o.fc_active, "bad": o.fc_bad, "target_soc": o.fc_target,
                              "min_soc": o.fc_min_soc},
            "bat1_first": o.bat1_first,
            "chain": {"wp": o.wp_why, "src": o.src_why},
            "pi": {"armed": self.cfg.pi_armed, "y_w": c_round(o.pi.y), "e_w": c_round(o.pi.e),
                   "applied_w": c_round(o.pi.applied), "u_w": c_round(o.pi.u),
                   "sp": {L.BM_PH[p]: o.pi.sp[p] for p in range(L.NPH)}},
        }
        tmp = self.state_file + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(js, f, separators=(",", ":"))
            os.replace(tmp, self.state_file)
        except OSError:
            loge("state file: write failed")

    def pi_shadow_log(self, pcc, bat1, pv):
        o = self.out
        try:
            with open(self.pi_shadow_file, "a") as f:
                if f.tell() == 0:
                    f.write("ts,pcc_w,bat1_w,pv_w,y_w,e_w,applied_w,pi_u_w,pi_min_w,pi_max_w,"
                            "sp_l1,sp_l2,sp_l3,pi_l1,pi_l2,pi_l3,armed,rule_l1,rule_l2,rule_l3\n")
                f.write("%d,%.0f,%.0f,%.0f,%.0f,%.0f,%.0f,%.0f,%.0f,%.0f,%d,%d,%d,%d,%d,%d,%d,%s,%s,%s\n" % (
                    int(time.time()), pcc, bat1, pv, o.pi.y, o.pi.e, o.pi.applied, o.pi.u, o.pi.u_min, o.pi.u_max,
                    o.sp[0], o.sp[1], o.sp[2], o.pi.sp[0], o.pi.sp[1], o.pi.sp[2], self.cfg.pi_armed,
                    o.why[0], o.why[1], o.why[2]))
                size = f.tell()
            if size > PI_SHADOW_MAX:
                os.replace(self.pi_shadow_file, self.pi_shadow_file + ".1")
        except OSError:
            pass

    # ---- one cycle -----------------------------------------------------------------------------------------
    def calc(self):
        """collect the inputs, bm_step, then log, PI shadow line and state file"""
        i = L.In()
        i.now = mono()
        i.t = int(time.time())
        with self.mq_lock:
            m = self.mq
            i.have_pcc, i.have_pv = m["have_pcc"], m["have_pv"]
            i.pcc, i.pv, i.bat1 = m["pcc"], m["pv"], m["bat1"]
            i.have_soc_bat1, i.soc_bat1 = m["have_soc_bat1"], m["soc_bat1"]
            i.pcc_avg5, i.bat1_avg5, i.inv_time = m["pcc_avg5"], m["bat1_avg5"], m["inv_time"]
            i.r290_hz, i.r290_time = m["r290_hz"], m["r290_time"]
            i.aussen, i.aussen_time = m["aussen"], m["aussen_time"]
            i.wp, i.wp_time = m["wp"], m["wp_time"]
            i.season, i.season_ts = m["season"], int(m["season_ts"])
            i.fc_target = m["fc_target"] if m["fc_ts"] > 0 else -1
            i.fc_ts = int(m["fc_ts"])
            if m["fc_ts"] > 0 and i.t - int(m["fc_ts"]) < 3 * 3600:    # the slots for the peak window, as FC_MAX_AGE
                i.fc_corr = m["fc_corr"]
                i.fc_slots = list(m["fc_slots"])
        vb = self.vebus()
        i.grid_ok = int(self.get(vb, "/Ac/ActiveIn/Connected") == 1.0)
        for p in range(L.NPH):
            v = self.get(vb, "/Ac/ActiveIn/%s/P" % L.BM_PH[p])
            i.ac_ok[p], i.ac_in[p] = int(v is not None), v or 0.0
            v = self.get(vb, "/Ac/Out/%s/P" % L.BM_PH[p])
            i.ac_out_ok[p], i.ac_out[p] = int(v is not None), v or 0.0
        for b, bank in enumerate(L.BM_BANKS):
            s = bank.service
            soc = self.get(s, "/Soc") if self.get(s, "/Connected") == 1.0 else None
            i.bms_ok[b], i.soc[b] = int(soc is not None), soc or 0.0
            pw = self.get(s, "/Dc/0/Power")
            i.power_ok[b], i.power[b] = int(pw is not None), pw or 0.0
            u = self.get(s, "/Dc/0/Voltage")
            i.volt_ok[b], i.volt[b] = int(u is not None), u or 0.0
            ccl, dcl = self.get(s, "/Info/MaxChargeCurrent"), None
            if ccl is not None and ccl >= 0:
                dcl = self.get(s, "/Info/MaxDischargeCurrent")
            i.lim_ok[b] = int(ccl is not None and ccl >= 0 and dcl is not None and dcl >= 0)
            i.ccl[b], i.dcl[b] = ccl or 0.0, dcl or 0.0

        o = self.out = L.bm_step(self.cfg, i, self.st)
        self.in_last = i
        self.run_full_forecast(i)

        if o.do4_pulse:
            if o.do4_acout:
                log("DO4 pulse (curtail WR2): grid offline, PV feeds %.0f W into one AC-out > %.0f W for %.0f s%s" % (
                    o.acout_feed_w, self.cfg.acout_feed_max_w, self.cfg.acout_feed_s,
                    "" if self.live else " (dry run, not sent)"))
            else:
                log("DO4 pulse (curtail WR2): PCC %.0f W > 20 kW for %.0f s%s" % (
                    i.pcc, i.now - self.st.do4_over_since, "" if self.live else " (dry run, not sent)"))
            if self.live:
                self.do4_publish("1")
                self.do4_off_at = mono() + L.DO4_PULSE_S
                self.publish(DO4_LAST_TOPIC, '{"ts":%d,"pcc_w":%.0f}' % (int(time.time()), i.pcc), 1, True)
        if o.lead_event > 0:
            log("SoC balance: %s ahead by %.1f %%" % (L.BM_BANKS[self.st.lead].name, o.lead_diff))
        elif o.lead_event < 0:
            log("SoC balance: back within 1.0 %")
        self.pi_shadow_log(i.pcc if i.have_pcc else 0, i.bat1, i.pv if i.have_pv else 0)
        rules = ";".join(o.why)
        if rules != self.last_rules or i.now - self.last_log >= LOG_SECONDS:
            eta = []
            for b, bank in enumerate(L.BM_BANKS):
                _, _, full = self.full_forecast(b)
                eta.append("%s full %s" % (bank.name, time.strftime("%H:%M", time.localtime(full)) if full else "-"))
            line = "  ".join("%s %+d W (%s)" % (L.BM_PH[p], o.sp[p], o.why[p]) for p in range(L.NPH))
            log("%spcc %s W wp %.0f W bat1 %.0f W pv %s W -> %s | %s | PI%s u %+.0f W" % (
                "" if self.live else "DRY ", "%.0f" % i.pcc if i.have_pcc else "None", o.wp_eff, i.bat1,
                "%.0f" % i.pv if i.have_pv else "None", line, "  ".join(eta),
                "" if self.cfg.pi_armed else " shadow", o.pi.u))
            self.last_rules = rules
            self.last_log = i.now
        self.setpoint_time = i.now
        self.write_state()
        self.publish_peak_model(i.now)

    def send_setpoints(self):
        self.sample_power()
        vb = self.vebus()
        if not vb:
            return
        sp = list(self.st.setpoints)
        if mono() - self.setpoint_time > SETPOINT_MAX_AGE:
            sp = [0] * L.NPH                # ESP TX watchdog
        if not self.hub4mode_set:
            self.set_int("com.victronenergy.settings", "/Settings/CGwacs/Hub4Mode", 3)
            self.hub4mode_set = self.live
        for p in range(L.NPH):
            self.set_int(vb, "/Hub4/%s/MaxFeedInPower" % L.BM_PH[p], (-sp[p] if sp[p] < 0 else 0) + 100)
            self.set_int(vb, "/Hub4/%s/AcPowerSetpoint" % L.BM_PH[p], sp[p])

    def stop_control(self):
        if self.do4_off_at > 0:             # never leave the relay on
            self.do4_publish("0")
            self.do4_off_at = 0.0
        vb = self.vebus()
        if vb:
            for p in range(L.NPH):
                self.set_int(vb, "/Hub4/%s/AcPowerSetpoint" % L.BM_PH[p], 0)
        self.set_int("com.victronenergy.settings", "/Settings/CGwacs/Hub4Mode", 1)
        log("stopped%s" % (", back to Hub4Mode 1" if self.live else ""))

    def run(self):
        log("batmonitor %s %s, cycle %d s, banks STACK1_MUST=L1, STACK2_PYTES=L2+L3, charge block %s, PI %s" % (
            VERSION, "LIVE" if self.live else "DRY RUN", L.CYCLE_SECONDS, "on" if self.cfg.ladesperre else "off",
            "ARMED" if self.cfg.pi_armed else "shadow"))
        self.load_params()
        self.mqtt_start()
        signal.signal(signal.SIGTERM, self.on_signal)
        signal.signal(signal.SIGINT, self.on_signal)
        nxt = mono() + L.CYCLE_SECONDS      # first cycle once the retained MQTT data is in
        while not self.stop:
            if self.do4_off_at > 0 and mono() >= self.do4_off_at:     # end of the DO4 pulse, between two cycles
                self.do4_publish("0")
                self.do4_off_at = 0.0
            wait = nxt - mono()
            if wait > 0:                    # only the cycle deadline starts calc(); a pulse end only wakes earlier
                if self.do4_off_at > 0:
                    wait = min(wait, max(0.001, self.do4_off_at - mono()))
                time.sleep(wait)
                continue
            self.calc()
            self.send_setpoints()
            nxt += L.CYCLE_SECONDS
            if nxt < mono():
                nxt = mono() + L.CYCLE_SECONDS
        self.stop_control()
        if self.mqttc is not None:
            self.mqttc.loop_stop()

    def on_signal(self, sig, frame):
        self.stop = True


def _number(x, dflt=None):
    """a finite JSON number as float, else dflt (forecast target_soc -1 / ts 0 mean "none / stale")"""
    if isinstance(x, bool) or not isinstance(x, (int, float)) or x != x or x in (float("inf"), float("-inf")):
        return dflt
    return float(x)


def main():
    os.environ["TZ"] = TZ_BERLIN
    time.tzset()
    Batmonitor().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
