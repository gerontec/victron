#!/usr/bin/env python3
# pv_victron: per-phase AC-in frequency, NA-Schutz log (newest entry per unit) and VE.Bus network quality (2026-10-20)
import os
import pymysql

ADD = []
for ph in (1, 2, 3):
    ADD += [
        ("ac_in_l%d_f_hz" % ph, "decimal(5,2) NULL COMMENT 'AC-in L%d frequency'" % ph),
        ("na_l%d_flags" % ph, "smallint unsigned NULL COMMENT 'NA-Schutz unit L%d newest log entry flags, "
                              "dim_vebus_ip_flag: (flags & code) <> 0'" % ph),
        ("na_l%d_at" % ph, "datetime NULL COMMENT 'NA-Schutz unit L%d newest log entry time'" % ph),
        ("vebus_nq_l%d" % ph, "smallint unsigned NULL COMMENT 'VE.Bus network quality counter unit L%d'" % ph),
    ]
conn = pymysql.connect(read_default_file=os.path.expanduser("~/.victron2db.cnf"))
with conn.cursor() as cur:
    cur.execute("SHOW COLUMNS FROM pv_victron")
    have = {r[0] for r in cur.fetchall()}
    todo = [(c, d) for c, d in ADD if c not in have]
    if todo:
        cur.execute("ALTER TABLE pv_victron " + ", ".join("ADD COLUMN %s %s" % cd for cd in todo))
    print("added:", [c for c, _ in todo] or "nothing")
conn.commit()
