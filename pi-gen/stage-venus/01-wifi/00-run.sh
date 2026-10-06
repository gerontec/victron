#!/bin/bash -e
# Install NetworkManager WiFi profiles from files/wifi.env (not in git):
#   WIFI_SSID, WIFI_PSK     main network (PSK empty = open network)
#   WIFI2_SSID, WIFI2_PSK   optional fallback, lower autoconnect priority
ENV_FILE="files/wifi.env"
if [ ! -f "${ENV_FILE}" ]; then
	echo "01-wifi: ${ENV_FILE} missing, no WiFi profile installed"
	exit 0
fi
. "${ENV_FILE}"

wifi_profile() {
	local ssid="$1" psk="$2" prio="$3"
	local conn="${ROOTFS_DIR}/etc/NetworkManager/system-connections/wifi-${ssid}.nmconnection"
	local security=""
	if [ -n "${psk}" ]; then
		security=$(printf "[wifi-security]\nkey-mgmt=wpa-psk\npsk=%s\n" "${psk}")
	fi
	cat > "${conn}" <<EOC
[connection]
id=wifi-${ssid}
type=wifi
autoconnect=true
autoconnect-priority=${prio}

[wifi]
mode=infrastructure
ssid=${ssid}

${security}
[ipv4]
method=auto

[ipv6]
method=auto
addr-gen-mode=default
EOC
	chmod 600 "${conn}"
}

install -d -m 700 "${ROOTFS_DIR}/etc/NetworkManager/system-connections"
wifi_profile "${WIFI_SSID}" "${WIFI_PSK}" 10
if [ -n "${WIFI2_SSID:-}" ]; then
	wifi_profile "${WIFI2_SSID}" "${WIFI2_PSK:-}" 0
fi
