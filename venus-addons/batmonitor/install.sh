#!/bin/sh
# Install batmonitor into a Venus /data (survives firmware updates) and start it from /data/rc.local.
# Usage inside Venus: sh install.sh            From the host: sudo sh install.sh /data
# The C version is built with the gcc of the Venus image (inside Venus only); without a binary service/run starts
# the Python port (batmonitor.py + bm_logic.py + bm_parse.py, byte-identical logic: make -C c py-compare).
set -e
DATA="${1:-/data}"
HERE=$(dirname "$(readlink -f "$0")")
D="$DATA/batmonitor"
mkdir -p "$D/service/log" "$D/c"
cp "$HERE/batmonitor.py" "$HERE/bm_logic.py" "$HERE/bm_parse.py" "$D/"
cp -r "$HERE/c/batmonitor.c" "$HERE/c/bm_logic.c" "$HERE/c/bm_logic.h" "$HERE/c/bm_parse.c" "$HERE/c/bm_parse.h" \
	"$HERE/c/Makefile" "$HERE/c/include" "$HERE/c/tests" "$D/c/"
cp "$HERE/service/run" "$D/service/run"
cp "$HERE/service/log/run" "$D/service/log/run"
chmod 755 "$D/batmonitor.py" "$D/service/run" "$D/service/log/run"
if [ -d /opt/victronenergy ] && command -v cc >/dev/null; then
	make -C "$D/c" -B || echo "C build failed: service/run falls back to batmonitor.py (or copy a binary built on the Pi)"
else
	echo "not inside Venus: build later in Venus with: make -C /data/batmonitor/c"
fi
RC="$DATA/rc.local"
[ -f "$RC" ] || printf '#!/bin/sh\n' > "$RC"
chmod 755 "$RC"
LINE='ln -sfn /data/batmonitor/service /service/batmonitor'
grep -qxF "$LINE" "$RC" || echo "$LINE" >> "$RC"
echo "installed in $D; starts at the next Venus boot, or now with: $LINE"
