#!/bin/sh
# Install the settings web page into a Venus /data (survives firmware updates); served by the Venus nginx at /settings/.
# Usage inside Venus: sh install.sh            From the host: sudo sh install.sh /data
# /data/rc.local recreates the link /var/www/venus/settings -> /data/www-settings at every Venus boot.
set -e
DATA="${1:-/data}"
HERE=$(dirname "$(readlink -f "$0")")
D="$DATA/www-settings"
mkdir -p "$D"
cp "$HERE/index.php" "$HERE/dbus_settings.py" "$HERE/settings_help.json" "$D/"
chmod 644 "$D/settings_help.json"
chmod 644 "$D/index.php"
chmod 755 "$D" "$D/dbus_settings.py"
RC="$DATA/rc.local"
[ -f "$RC" ] || printf '#!/bin/sh\n' > "$RC"
chmod 755 "$RC"
LINE='ln -sfn /data/www-settings /var/www/venus/settings'
grep -qxF "$LINE" "$RC" || echo "$LINE" >> "$RC"
echo "installed in $D; link now (inside Venus): $LINE"
