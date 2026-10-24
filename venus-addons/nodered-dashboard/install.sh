#!/bin/sh
# Install the Venus dashboard flow into Node-RED on Venus (/data, survives firmware updates).
# Usage on the Venus host: sudo sh install.sh            (runs gen_flows.py, merges, restarts Node-RED)
# Needs @flowfuse/node-red-dashboard in /data/home/nodered/.node-red (npm install, see README.md).
# Merge: nodes with ids vdash_* are replaced, all other nodes of flows.json are kept.
set -e
HERE=$(dirname "$(readlink -f "$0")")
NR=/data/home/nodered/.node-red
python3 "$HERE/gen_flows.py" > /tmp/vdash_flows.json
cp "$NR/flows.json" "$NR/flows.json.bak-$(date +%Y%m%d%H%M%S)"
python3 - "$NR/flows.json" /tmp/vdash_flows.json <<'PY'
import json, sys
old = json.load(open(sys.argv[1]))
new = json.load(open(sys.argv[2]))
keep = [n for n in old if not str(n.get('id', '')).startswith('vdash_')]
json.dump(keep + new, open(sys.argv[1], 'w'), indent=1, ensure_ascii=False)
print(f'flows.json: {len(keep)} kept, {len(new)} vdash nodes')
PY
chown "$(stat -c %u:%g "$NR")" "$NR/flows.json"
L=$(machinectl show venus -p Leader --value)
nsenter -t "$L" -a svc -t /service/node-red-venus
echo "Node-RED restarted; page: https://<gx>:1881/dashboard/venus"
