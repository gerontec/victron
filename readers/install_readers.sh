#!/bin/bash
# Install the ebox (battery console) and Zenner (M-Bus heat meter) readers 1:1 as on the old Pi:
# user pi, /home/pi/python + /home/pi/sofar, cron every 30 s (ebox) / 60 s (zenner), udev names ttyUSB23/ttyUSB24.
# The scripts carry site config (DB, broker) and are not part of this repo; pass the directory that holds them.
# Usage (as root on the target, or inside a chroot): install_readers.sh <src dir with python/ and sofar/> [udev rules file]
set -euo pipefail

SRC="${1:?usage: install_readers.sh <src dir> [udev rules file]}"
RULES="${2:-${SRC}/99-usb-serial.rules}"

for f in python/ebox_mqtt.py python/pv_ebox2.py python/zenner2db.py sofar/ebox.c; do
	[ -f "${SRC}/${f}" ] || { echo "missing ${SRC}/${f}"; exit 1; }
done

export DEBIAN_FRONTEND=noninteractive
apt-get install -y -q python3-serial python3-paho-mqtt python3-pymysql python3-pip gcc libc6-dev cron

id pi >/dev/null 2>&1 || useradd -m -s /bin/bash pi
usermod -aG dialout pi
install -d -o pi -g pi /home/pi/python /home/pi/sofar
install -m 755 -o pi -g pi "${SRC}"/python/ebox_mqtt.py "${SRC}"/python/pv_ebox2.py "${SRC}"/python/zenner2db.py /home/pi/python/
install -m 644 -o pi -g pi "${SRC}"/sofar/ebox.c /home/pi/sofar/
[ -f "${SRC}/sofar/Makefile" ] && install -m 644 -o pi -g pi "${SRC}"/sofar/Makefile /home/pi/sofar/

# same flags as the Makefile target "ebox"; built natively for this architecture
gcc -Wall -Wextra -O2 -std=c11 -o /home/pi/sofar/ebox /home/pi/sofar/ebox.c
chown pi:pi /home/pi/sofar/ebox

# pyMeterBus is not packaged in Debian
sudo -u pi -H pip3 install --user --break-system-packages --quiet pyMeterBus

# fixed device names: Prolific 067b:2303 -> ttyUSB23 (ebox console), 067b:23a3 -> ttyUSB24 (M-Bus)
if [ -f "${RULES}" ]; then
	install -m 644 "${RULES}" /etc/udev/rules.d/99-usb-serial.rules
else
	cat > /etc/udev/rules.d/99-usb-serial.rules <<'EOF'
SUBSYSTEM=="tty", ATTRS{idVendor}=="067b", ATTRS{idProduct}=="2303", SYMLINK+="ttyUSB23",GROUP="dialout",MODE="0666"
SUBSYSTEM=="tty", ATTRS{idVendor}=="067b", ATTRS{idProduct}=="23a3", SYMLINK+="ttyUSB24",GROUP="dialout",MODE="0666"
EOF
fi
udevadm control --reload-rules 2>/dev/null && udevadm trigger --subsystem-match=tty 2>/dev/null || true

# cron lines exactly as on the old Pi
crontab -u pi - <<'EOF'
* * * * * /home/pi/python/zenner2db.py >/tmp/zenner2db.txt
* * * * * /usr/bin/python3 /home/pi/python/ebox_mqtt.py >/tmp/ebox_mqtt.log 2>&1
* * * * * sleep 30 && /usr/bin/python3 /home/pi/python/ebox_mqtt.py >>/tmp/ebox_mqtt.log 2>&1
EOF
systemctl enable cron >/dev/null 2>&1 || true
echo "readers installed for $(uname -m)"
