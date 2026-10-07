#!/bin/bash
# Write the commissioned configuration of the Pi 4 host and the Venus OS subsystem, idempotent.
# Run on the Pi from a checkout of this repo, after install_venus.sh:
#   sudo host/venus-settings.sh apply    host: CAN HAT overlay, CAN profile, dbus-ebox-battery, victron2db;
#                                        Venus: settings from host/venus-settings.conf
#   sudo host/venus-settings.sh save [path ...]
#                                        read the paths of venus-settings.conf (plus the given extra paths)
#                                        from the running Venus and rewrite the file, then commit it
#   sudo host/venus-settings.sh show     current values of venus-settings.conf in Venus
set -eu
HERE=$(dirname "$(readlink -f "$0")")
REPO=$(dirname "$HERE")
CONF="$HERE/venus-settings.conf"
BOOTCFG=/boot/firmware/config.txt
# Waveshare RS485 CAN HAT: MCP2515 with a 12 MHz crystal (8 MHz gives a wrong bitrate: only error frames)
CAN_OVERLAY='dtoverlay=mcp2515-can0,oscillator=12000000,interrupt=25,spimaxfrequency=1000000'
CAN_PROFILE=bms         # Speicher A: CAN-bus BMS LV, 500 kbit/s (see can.md)
RUN_USER="${SUDO_USER:-pi}"
RUN_HOME=$(getent passwd "$RUN_USER" | cut -d: -f6)
V2DB_DIR="$RUN_HOME/python"
V2DB_CRON="* * * * * /usr/bin/python3 $V2DB_DIR/victron2db.py >/tmp/victron2db.log 2>&1"

[ "$(id -u)" -eq 0 ] || exec sudo "$0" "$@"

venus() {
	leader=$(machinectl show venus -p Leader --value 2>/dev/null) || { echo "Venus container not running" >&2; return 1; }
	nsenter -t "$leader" -a "$@"
}

settings_paths() {
	grep -vE '^\s*(#|$)' "$CONF" | awk '{print $1}'
}

get() {
	venus dbus -y com.victronenergy.settings "$1" GetValue 2>/dev/null | tail -1
}

# dbus tool output -> conf value: 'text' -> text, number -> %number
to_conf() {
	case "$1" in
		\'*\') v="${1#\'}"; echo "${v%\'}" ;;
		*) echo "%$1" ;;
	esac
}

apply_bootcfg() {
	if grep -qxF "$CAN_OVERLAY" "$BOOTCFG"; then
		echo "config.txt: CAN overlay ok"
		return
	fi
	cp "$BOOTCFG" "$BOOTCFG.bak-$(date +%Y%m%d%H%M%S)"
	if grep -q '^dtoverlay=mcp2515-can0' "$BOOTCFG"; then
		sed -i "s|^dtoverlay=mcp2515-can0.*|$CAN_OVERLAY|" "$BOOTCFG"
	else
		printf '\n[all]\n# MCP2515 CAN controller (Waveshare RS485 CAN HAT): 12 MHz crystal, interrupt GPIO25\ndtparam=spi=on\n%s\n' \
			"$CAN_OVERLAY" >> "$BOOTCFG"
	fi
	echo "config.txt: CAN overlay written, REBOOT NEEDED, then run apply again"
	REBOOT=1
}

apply_can() {
	if ! ip link show can0 >/dev/null 2>&1; then
		echo "can0 missing: CAN profile skipped"
		return
	fi
	/usr/local/sbin/venus-can-profile "$CAN_PROFILE" can0
}

apply_ebox() {
	sh "$REPO/venus-addons/dbus-ebox-battery/install.sh" /data
	venus sh -c '[ -e /service/dbus-ebox-battery ] || ln -sfn /data/dbus-ebox-battery/service /service/dbus-ebox-battery; svc -t /service/dbus-ebox-battery' \
		&& echo "dbus-ebox-battery restarted"
}

apply_victron2db() {
	install -D -o "$RUN_USER" -g "$RUN_USER" -m 755 "$REPO/readers/victron2db.py" "$V2DB_DIR/victron2db.py"
	if [ -f "$RUN_HOME/.victron2db.cnf" ]; then
		# add the Speicher A columns to an existing wagodb.pv_victron (ADD COLUMN IF NOT EXISTS)
		sudo -u "$RUN_USER" python3 - "$RUN_HOME/.victron2db.cnf" "$REPO/sql/pv_victron_speicher_a.sql" <<'EOF'
import sys, pymysql
conn = pymysql.connect(read_default_file=sys.argv[1], connect_timeout=10)
with conn.cursor() as cur:
	cur.execute(open(sys.argv[2]).read())
conn.commit()
print('pv_victron: Speicher A columns ok')
EOF
	else
		echo "$RUN_HOME/.victron2db.cnf missing: DB not touched, victron2db cannot write"
	fi
	if crontab -u "$RUN_USER" -l 2>/dev/null | grep -qF "$V2DB_DIR/victron2db.py"; then
		echo "victron2db: cron ok"
	else
		(crontab -u "$RUN_USER" -l 2>/dev/null; echo "$V2DB_CRON") | crontab -u "$RUN_USER" -
		echo "victron2db: cron added"
	fi
}

apply_venus_settings() {
	grep -vE '^\s*(#|$)' "$CONF" | while read -r path value; do
		venus dbus -y com.victronenergy.settings "$path" SetValue "$value" >/dev/null
		echo "venus: $path = $(get "$path")"
	done
}

show() {
	for p in $(settings_paths); do
		echo "$p = $(get "$p")"
	done
}

save() {
	tmp=$(mktemp)
	paths=$( { settings_paths; for p in "$@"; do echo "$p"; done; } | awk '!seen[$0]++')
	# keep comments and order, replace the values; extra paths go to the end
	while IFS= read -r line; do
		case "$line" in
			''|\#*) echo "$line" ;;
			*) p=${line%% *}; echo "$p $(to_conf "$(get "$p")")" ;;
		esac
	done < "$CONF" > "$tmp"
	for p in $paths; do
		grep -q "^$p " "$tmp" || echo "$p $(to_conf "$(get "$p")")" >> "$tmp"
	done
	cat "$tmp" > "$CONF"
	rm -f "$tmp"
	echo "saved to $CONF:"
	show
}

REBOOT=0
case "${1:-}" in
	apply)
		apply_bootcfg
		if [ "$REBOOT" -eq 0 ]; then
			apply_can
			apply_ebox
			apply_victron2db
			apply_venus_settings
		fi
		;;
	save) shift; save "$@" ;;
	show) show ;;
	*) echo "usage: $0 apply | save [path ...] | show" >&2; exit 2 ;;
esac
