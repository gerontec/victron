#!/bin/bash -e
# Install a NetworkManager WiFi profile from files/wifi.env (WIFI_SSID, WIFI_PSK - empty for an open network; not in git).
ENV_FILE="files/wifi.env"
if [ ! -f "${ENV_FILE}" ]; then
	echo "01-wifi: ${ENV_FILE} missing, no WiFi profile installed"
	exit 0
fi
. "${ENV_FILE}"
CONN="${ROOTFS_DIR}/etc/NetworkManager/system-connections/wifi-${WIFI_SSID}.nmconnection"
# open network when WIFI_PSK is empty
SECURITY=""
if [ -n "${WIFI_PSK}" ]; then
	SECURITY=$(printf "[wifi-security]\nkey-mgmt=wpa-psk\npsk=%s\n" "${WIFI_PSK}")
fi
install -d -m 700 "${ROOTFS_DIR}/etc/NetworkManager/system-connections"
cat > "${CONN}" <<EOC
[connection]
id=wifi-${WIFI_SSID}
type=wifi
autoconnect=true

[wifi]
mode=infrastructure
ssid=${WIFI_SSID}

${SECURITY}
[ipv4]
method=auto

[ipv6]
method=auto
addr-gen-mode=default
EOC
chmod 600 "${CONN}"
