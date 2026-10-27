#!/usr/bin/env python3
"""Export the batmonitor parameter definitions and the two rule tables as JSON for the web editor.

    tools/export_params.py > ../../web/bm_params_def.json

Source: bm_logic.py (the Python port, byte-identical logic with the C port: make -C c py-compare). The editor
(web/bm_params.php) shows the parameters as a form and the source / mode matrices read-only; the Pi validates every
published version again against its own deployed bm_logic.py (host/bm_params_pull.py), so this file only drives
the form, it is not the authority for the limits."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import bm_logic as L  # noqa: E402
from batmonitor import VERSION  # noqa: E402

ACTION = ['ZERO', 'FORCE', 'CHARGE', 'DISCHARGE']
WHY = ['NONE', 'NAME', 'SOC', 'PEAK_H', 'POWER', 'CHAIN']

out = {
    'version': VERSION,
    'params': [{'name': p.name, 'default': p.default, 'min': p.min, 'max': p.max, 'unit': p.unit, 'help': p.help}
               for p in L.BM_PARAMS],
    'src_rule': [dict(src=L.BM_SRC_NAME[k], b1_signed=r.b1_signed, trickle=r.trickle, idle_w=r.idle_w,
                      prop_hold=r.prop_hold) for k, r in sorted(L.BM_SRC_RULE.items())],
    'mode_rule': [dict(mode=L.BM_MODE_NAME[k], action=ACTION[r.action], capped=r.capped, why=WHY[r.why])
                  for k, r in sorted(L.BM_MODE_RULE.items())],
}
json.dump(out, sys.stdout, indent=1)
print()
