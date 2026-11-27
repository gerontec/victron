#!/usr/bin/env python3
"""rule_code_dim.py - write the bit layout of pv_victron.bm_l1..3_code into wagodb.dim_bm_rule_bit (0.84-py).

The table holds only the layout of the running batmonitor version (user 2026-11-27: "alle alten zustaende sind
unwichtig, immer nur aktuelle version"): every run replaces all rows with RULE_FIELDS of the bm_logic.py next to
this script. Run it on every deploy that changes RULE_FIELDS (from the dell, like the deploy itself).

Decode in SQL:  value of a field = (code >> bit_lo) & ((1 << bits) - 1), its word = token of that (field, value).
Example, minutes per rule of phase L1 on one day:
    SELECT bm_l1_code, COUNT(*) FROM pv_victron WHERE ts >= '2026-12-01' AND ts < '2026-12-02' GROUP BY 1;

    tools/rule_code_dim.py            # write
    tools/rule_code_dim.py --dry-run  # only print the rows
Credentials: ~/.my.cnf [wagodb], host VICTRON_DB_HOST (default 192.168.178.218, where pv_victron lives).
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
import bm_logic as L  # noqa: E402

DB_HOST = os.environ.get("VICTRON_DB_HOST", "192.168.178.218")
DDL = """CREATE TABLE IF NOT EXISTS dim_bm_rule_bit (
  field VARCHAR(16) NOT NULL COMMENT 'field of the rule bit field (mode, dis_rule, src, split, ...)',
  value TINYINT UNSIGNED NOT NULL COMMENT 'value index inside the field, 0 = absent',
  bit_lo TINYINT UNSIGNED NOT NULL COMMENT 'first bit of the field',
  bits TINYINT UNSIGNED NOT NULL COMMENT 'width of the field',
  mask INT UNSIGNED NOT NULL COMMENT 'value << bit_lo: the bits this (field, value) sets',
  token VARCHAR(24) NOT NULL COMMENT 'word in the rule text (without measured values)',
  logic_version VARCHAR(16) NOT NULL COMMENT 'batmonitor version the layout belongs to',
  written_at DATETIME NOT NULL DEFAULT current_timestamp(),
  PRIMARY KEY (field, value)
) COMMENT='bit layout of pv_victron.bm_l1..3_code, running batmonitor version only (rule_code_dim.py)'"""


def version():
    with open(os.path.join(HERE, "..", "batmonitor.py")) as f:
        for line in f:
            if line.startswith("VERSION = "):
                return line.split('"')[1]
    return "?"


def rows():
    ver = version()
    out = []
    for name, lo, bits, vals in L.RULE_FIELDS:
        for v, tok in enumerate(vals):
            if v:
                out.append((name, v, lo, bits, v << lo, tok, ver))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="only print the rows")
    ap.add_argument("--db-host", default=DB_HOST)
    a = ap.parse_args()
    r = rows()
    for x in r:
        print("%-9s %2d  bits %2d..%2d  mask %10d  %s" % (x[0], x[1], x[2], x[2] + x[3] - 1, x[4], x[5]))
    if a.dry_run:
        return
    import pymysql
    con = pymysql.connect(read_default_file=os.path.expanduser("~/.my.cnf"), read_default_group="wagodb",
                          host=a.db_host, database="wagodb", connect_timeout=5)
    with con.cursor() as cur:
        cur.execute(DDL)
        cur.execute("DELETE FROM dim_bm_rule_bit")
        cur.executemany("INSERT INTO dim_bm_rule_bit (field, value, bit_lo, bits, mask, token, logic_version) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s)", r)
    con.commit()
    con.close()
    print("%d rows written to dim_bm_rule_bit (%s) for batmonitor %s" % (len(r), a.db_host, r[0][6]))


if __name__ == "__main__":
    main()
