"""
test_parse.py: bm_parse.py with real payloads, the same cases as c/tests/test_parse.c plus the Python json traps
(true is an int, NaN / Infinity literals). usage: python3 tests/test_parse.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from bm_parse import parse_inverter, parse_number, parse_r290  # noqa: E402

checks = fails = 0


def check(cond, msg):
    global checks, fails
    checks += 1
    if not cond:
        fails += 1
        print("  FAIL", msg)


FULL = ('"ActivePower_PCC_Total":-1.25,"ActivePower_PCC_Total_avg5":-1.5,"Power_Bat1":0.4,'
        '"Power_Bat1_avg5":0.3,"Power_PV1":2.0,"Power_PV2":1.5,"SOC_Bat1":37')

s = parse_inverter("{" + FULL + "}")
check(s is not None, "full message accepted")
check(s["pcc"] == -1250 and s["pcc_avg5"] == -1500 and s["bat1"] == 400 and s["bat1_avg5"] == 300, "kW -> W")
check(s["have_pv"] and s["pv"] == 3500 and s["have_soc_bat1"] and s["soc_bat1"] == 37, "PV and SOC_Bat1")

check(parse_inverter('{"Power_Bat1":0.4,"Power_PV1":2,"Power_PV2":1}') is None, "PCC missing: rejected")
check(parse_inverter('{"ActivePower_PCC_Total":null,"Power_Bat1":0.4}') is None, "PCC null: rejected")
check(parse_inverter('{"ActivePower_PCC_Total":"-1.2","Power_Bat1":0.4}') is None, "PCC string: rejected")
check(parse_inverter('{"ActivePower_PCC_Total":1e999,"Power_Bat1":0.4}') is None, "PCC infinite: rejected")
check(parse_inverter('{"ActivePower_PCC_Total":250,"Power_Bat1":0.4}') is None, "PCC 250 kW: rejected")
check(parse_inverter('{"ActivePower_PCC_Total":-1.2}') is None, "Power_Bat1 missing: rejected")
check(parse_inverter('{"ActivePower_PCC_Total":-1.2,"Power_Bat1":null}') is None, "Power_Bat1 null: rejected")
check(parse_inverter("[1,2]") is None and parse_inverter("not json") is None and parse_inverter("") is None,
      "no object: rejected")
# Python json only
check(parse_inverter('{"ActivePower_PCC_Total":true,"Power_Bat1":0.4}') is None, "PCC true: rejected (bool is int)")
check(parse_inverter('{"ActivePower_PCC_Total":NaN,"Power_Bat1":0.4}') is None, "PCC NaN literal: rejected")
check(parse_inverter('{"ActivePower_PCC_Total":-1.2,"Power_Bat1":Infinity}') is None, "Bat1 Infinity: rejected")

s = parse_inverter('{"ActivePower_PCC_Total":-1.2,"Power_Bat1":0.4,"Power_PV1":2}')
check(s is not None and not s["have_pv"], "PV2 missing: sample usable, but no PV")
check(s["pcc_avg5"] == -1200 and s["bat1_avg5"] == 400, "5 min means missing: the instant values")
check(not s["have_soc_bat1"] and s["soc_bat1"] < 0, "SOC_Bat1 missing: none")
s = parse_inverter('{"ActivePower_PCC_Total":0,"Power_Bat1":0,"SOC_Bat1":140}')
check(s is not None and s["pcc"] == 0 and not s["have_soc_bat1"], "a real 0 kW is valid; SOC 140 % is none")

check(parse_r290('{"comp_freq_actual":48}') == 48, "R290 48 Hz")
check(parse_r290('{"other":1}') is None, "comp_freq_actual missing: rejected")
check(parse_r290('{"comp_freq_actual":null}') is None, "null: rejected")

check(parse_number("1234.5", -1000, 30000) == 1234.5, "plain number")
check(parse_number(" 812 \n", -1000, 30000) == 812, "whitespace around")
check(parse_number("123xyz", -1000, 30000) is None, "trailing text: rejected")
check(parse_number("", -1000, 30000) is None and parse_number("abc", -1000, 30000) is None, "no number")
check(parse_number("nan", -1000, 30000) is None and parse_number("inf", -1000, 30000) is None, "nan / inf")
check(parse_number("40000", -1000, 30000) is None, "out of range")

print("%d checks, %d failed" % (checks, fails))
sys.exit(fails != 0)
