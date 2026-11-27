"""
test_rule_code.py - 0.84-py: the rule text of every phase is fixed by its bit field (pv_victron.bm_l1..3_code).

Random cycles through bm_step (the inputs of bm_selftest.py, four seasons, several seeds, PI armed and not) collect
every rule text the running logic produces. For each: no unknown token (bit 31), the decoded text equals the text
without measured values (same words), and one code never stands for two texts (and the other way round).
usage: python3 tests/test_rule_code.py [cycles per seed, default 4000]
"""
import os
import random
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
import bm_logic as L  # noqa: E402
import bm_selftest as S  # noqa: E402

fails = 0


def check(cond, what):
    global fails
    print(("ok   " if cond else "FAIL ") + what)
    if not cond:
        fails += 1


def normal(why):
    """the text without measured values, as a sorted word list"""
    words = []
    for w in why.split():
        if w.startswith("WPMAX("):
            words.append("WPMAX(0)" if float(w[6:-1]) == 0 else "WPMAX(>0)")
        elif re.match(r"^WP\+\d+$", w):
            words.append("WP+")
        else:
            words.append(w.split("(", 1)[0])
    return sorted(words)          # a discharge rule word (PROP, IDLE, ...) names the mode DIS, as in the code


# ---- fixed examples (texts of the live log 2026-11-07 .. 2026-11-27) ---------------------------------------------
EXAMPLES = {
    "FORCE(2.3%) LOW": "FORCE LOW",
    "PROT(2.3%)": "PROT",
    "CHARGE SRC:Z2 1PH": "CHARGE SRC:Z2 1PH",
    "CHARGE SRC:Z2 STBY": "CHARGE SRC:Z2 STBY",
    "CHARGE SRC:DAY EQ FF CAP": "CHARGE SRC:DAY EQ FF CAP",
    "FORCE(10.0%) LOW CCL": "FORCE LOW CCL",
    "CHARGING(1381W)": "CHARGING",
    "LADESPERRE(13h)": "LADESPERRE",
    "PROP SRC:DAY EQ": "PROP SRC:DAY EQ",
    "WPPEAK SRC:Z2 1PH": "WPPEAK SRC:Z2 1PH",
    "HOUSE SRC:Z2 EQ WPMAX(100) WP+40": "HOUSE SRC:Z2 EQ WPMAX(>0) WP+",
    "IDLE SRC:STK SPILL WPMAX(0) DCL": "IDLE SRC:STK SPILL WPMAX(0) DCL",
    "BMS_MISSING": "BMS_MISSING",
    "": "",
}
for text, want in EXAMPLES.items():
    c = L.rule_code(text)
    check(L.rule_code_text(c) == want and c < 1 << 31, "%-36r -> %8d -> %r" % (text, c, want))
check(L.rule_code("PROP SRC:DAY EQ BOGUS") >> 31 == 1, "unknown token sets bit 31")
check(L.rule_code("CHARGE PROT") >> 31 == 1, "two modes in one text set bit 31")
used = sum(f[2] for f in L.RULE_FIELDS if f[0] != "unparsed")
check(used == 22 and all(len(f[3]) <= 1 << f[2] for f in L.RULE_FIELDS), "layout: 22 bits used, every field fits")
spans = sorted((f[1], f[1] + f[2]) for f in L.RULE_FIELDS)
check(all(a[1] <= b[0] for a, b in zip(spans, spans[1:])) and spans[-1][1] <= 32, "fields do not overlap, <= 32 bit")

# ---- every text the logic produces in random cycles -------------------------------------------------------------
cycles = int(sys.argv[1]) if len(sys.argv) > 1 else 4000
texts, by_code, by_text, bad = {}, {}, {}, []
for pi_armed in (0, 1):
    cfg = S.site_cfg(L)
    cfg.pi_armed = pi_armed
    for seed in (1, 2, 3):
        for run_no, month in enumerate((1, 4, 7, 10)):
            r = random.Random(seed * 100 + run_no + 1000 * pi_armed)
            t0 = int(time.mktime((2026, month, 15, 0, 0, 0, 0, 0, -1)))
            st, now, sp = L.State(), 1000.0, [0] * L.NPH
            for _ in range(cycles // 4):
                now += r.choice((5, 5, 5, 30, 300))
                o = L.bm_step(cfg, S.make_in(L, r, now, t0 + int(now), sp), st)
                sp = list(o.sp)
                for p in range(L.NPH):
                    w, c = o.why[p], o.code[p]
                    texts[w] = texts.get(w, 0) + 1
                    if c != L.rule_code(w) or c >> 31 or sorted(L.rule_code_text(c).split()) != normal(w):
                        bad.append((w, c, L.rule_code_text(c)))
                    n = " ".join(normal(w))
                    by_code.setdefault(c, set()).add(n)
                    by_text.setdefault(n, set()).add(c)
check(not bad, "%d distinct texts: every one encodes without unknown token and decodes to its words%s" % (
    len(texts), "" if not bad else ": %s" % bad[:5]))
check(all(len(v) == 1 for v in by_code.values()), "one code = one text (%d codes)" % len(by_code))
check(all(len(v) == 1 for v in by_text.values()), "one text = one code")
modes = {L.rule_code_fields(c).get("mode", 0) for c in by_code}
print("     modes seen: %s" % " ".join(sorted(L.RULE_FIELDS[0][3][m] or "-" for m in modes)))
print("     largest code %d (%d bits)" % (max(by_code), max(by_code).bit_length()))


# ---- 0.85-py: batmonitor logs an unknown token once per text (needs python3-dbus / paho stubs) -------------------
try:
    import types
    sys.modules.setdefault("dbus", types.ModuleType("dbus"))
    pm = types.ModuleType("paho")
    pm.mqtt = types.ModuleType("paho.mqtt")
    pm.mqtt.client = types.ModuleType("paho.mqtt.client")
    sys.modules.setdefault("paho", pm)
    sys.modules.setdefault("paho.mqtt", pm.mqtt)
    sys.modules.setdefault("paho.mqtt.client", pm.mqtt.client)
    import batmonitor as B  # noqa: E402
except Exception as e:                      # noqa: BLE001
    print("skip batmonitor.check_rule_code: %s" % e)
    B = None
if B is not None:
    logs = []
    B.loge = lambda s: logs.append(s)
    bm = B.Batmonitor.__new__(B.Batmonitor)
    bm.unparsed_seen = set()
    o = L.Out()
    o.why = ["PROT(2.3%)", "PROP SRC:DAY EQ NEWWORD", "PROP SRC:DAY EQ NEWWORD"]
    o.code = [L.rule_code(w) for w in o.why]
    bm.check_rule_code(o)
    bm.check_rule_code(o)
    check(len(logs) == 1 and "NEWWORD" in logs[0] and "L2" in logs[0], "batmonitor: one ERROR line per unknown text")
    o.why[0] = "PROT(2.3%) OTHER"
    o.code = [L.rule_code(w) for w in o.why]
    bm.check_rule_code(o)
    check(len(logs) == 2 and "OTHER" in logs[1], "batmonitor: a second unknown text gets its own line")

print("%d failed" % fails)
sys.exit(1 if fails else 0)
