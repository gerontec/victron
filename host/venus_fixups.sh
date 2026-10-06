#!/bin/sh
# Adapt a Venus OS rootfs to run as a systemd-nspawn container. Idempotent.
# Usage: venus_fixups.sh <venus rootfs dir>
set -eu
R="${1:?usage: venus_fixups.sh <venus rootfs dir>}"
HERE=$(dirname "$(readlink -f "$0")")
[ -d "$R/opt/victronenergy" ] || { echo "not a Venus rootfs: $R" >&2; exit 1; }

# nspawn refuses to boot a tree without os-release
version=$(head -n1 "$R/opt/victronenergy/version")
cat > "$R/usr/lib/os-release" <<EOF
NAME="Venus OS"
ID=venus
VERSION_ID=${version}
PRETTY_NAME="Venus OS ${version} (nspawn)"
EOF

# the host's udevd owns /run/udev (bound read-only); a second udevd fails and stalls the boot
[ -e "$R/etc/rcS.d/S04udev" ] && mv "$R/etc/rcS.d/S04udev" "$R/etc/rcS.d/K04udev.disabled-host-udev"

# sysvinit runs rc with the nspawn console pty as controlling tty; when rc 5 ends, the backgrounded
# svscanboot gets SIGHUP and all Venus services die. setsid detaches it.
sed -i 's|^\tsvscanboot &|\tsetsid svscanboot \&|' "$R/etc/init.d/svscanboot.sh"

# without udevd nobody fills /dev/serial-starter
install -m 755 "$HERE/files/serial-starter-devs.sh" "$R/etc/init.d/serial-starter-devs.sh"
ln -sf ../init.d/serial-starter-devs.sh "$R/etc/rcS.d/S21serial-starter-devs.sh"

# no tty1/ttyS0 in the container: the gettys only respawn "too fast"
sed -i -e 's|^1:12345:respawn:/sbin/getty|#&|' -e 's|^S0:12345:respawn:/sbin/getty|#&|' "$R/etc/inittab"

echo "fixups applied to $R (${version})"
