#!/bin/sh
# Install batmonitor into a Venus /data (survives firmware updates) and start it from /data/rc.local.
# Usage inside Venus: sh install.sh            From the host: sudo sh install.sh /data
set -e
DATA="${1:-/data}"
HERE=$(dirname "$(readlink -f "$0")")
D="$DATA/batmonitor"
mkdir -p "$D/service/log"
cp "$HERE/batmonitor.py" "$D/"
cp "$HERE/service/run" "$D/service/run"
cp "$HERE/service/log/run" "$D/service/log/run"
chmod 755 "$D/batmonitor.py" "$D/service/run" "$D/service/log/run"
RC="$DATA/rc.local"
[ -f "$RC" ] || printf '#!/bin/sh\n' > "$RC"
chmod 755 "$RC"
LINE='ln -sfn /data/batmonitor/service /service/batmonitor'
grep -qxF "$LINE" "$RC" || echo "$LINE" >> "$RC"
echo "installed in $D; starts at the next Venus boot, or now with: $LINE"
