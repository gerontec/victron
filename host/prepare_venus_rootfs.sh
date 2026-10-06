#!/bin/bash
# Turn the ext4 image of the Venus build into a container rootfs tarball:
# copy the tree, apply the nspawn fixups and install the closed Victron packages from the official feed.
# The feed only carries the current release, so the image must be built from the same tag (e.g. v3.81).
# Needs root; on an x86 build host also qemu-user-binfmt (opkg is an armv7 binary).
# Usage: sudo ./prepare_venus_rootfs.sh <venus-image-large-raspberrypi4-*.ext4[.gz]> <out.tar.zst>
set -euo pipefail
IMG="${1:?usage: prepare_venus_rootfs.sh <image.ext4[.gz]> <out.tar.zst>}"
OUT="${2:?usage: prepare_venus_rootfs.sh <image.ext4[.gz]> <out.tar.zst>}"
FEED="${FEED:-https://updates.victronenergy.com/feeds/venus/release/packages/scarthgap}"
HERE=$(dirname "$(readlink -f "$0")")

# closed packages of the official large image that have no public recipe
CLOSED="serial-starter mk2-dbus mk2vsc hub4control vecan-dbus vrmlogger vebus-system-config vebus-updater
service-advertiser dbus-modbustcp dbus-mqtt-integrations dbus-parallel-bms dbus-eebus dbus-canopen-motordrive
dbus-fzsonick-48tl dbus-adc dbus-ble-sensors dbus-cgwacs dbus-fronius dbus-motordrive dbus-rv-c dbus-valence
gps-dbus can-bus-bms can-bus-bms-hv vedirect-interface gui start-gui-v1 mqtt-rpc venus-eeprom
venus-opportunity-loads vesmart-server velib-tools support-keys dup vup xupc xupd xupt prodtest"

W=$(mktemp -d /var/tmp/venus-rootfs.XXXXXX)
cleanup() {
	umount "$W/rootfs/proc" 2>/dev/null || true
	umount "$W/rootfs/var/volatile" 2>/dev/null || true
	umount "$W/mnt" 2>/dev/null || true
	rm -rf "$W"
}
trap cleanup EXIT

echo "== copy rootfs from ${IMG}"
if [[ "$IMG" == *.gz ]]; then
	gzip -dc "$IMG" > "$W/image.ext4"
	IMG="$W/image.ext4"
fi
mkdir "$W/mnt" "$W/rootfs"
mount -o loop,ro "$IMG" "$W/mnt"
tar -C "$W/mnt" --numeric-owner --xattrs --acls -cpf - . | tar -C "$W/rootfs" --numeric-owner --xattrs --acls -xpf -
umount "$W/mnt"
rm -f "$W/image.ext4"

echo "== fixups"
"$HERE/venus_fixups.sh" "$W/rootfs"

echo "== closed packages from ${FEED}"
R="$W/rootfs"
# v3.81 ships venus-feed-configs (etc/opkg/venus.conf -> release feed); older images have no feed at all
if ! chroot "$R" /bin/sh -c 'cat /etc/opkg/*.conf 2>/dev/null' | grep -q '^src'; then
	printf 'src/gz %s %s/%s\n' all "$FEED" all cortexa7hf-neon-vfpv4 "$FEED" cortexa7hf-neon-vfpv4 \
		raspberrypi4 "$FEED" raspberrypi4 > "$R/etc/opkg/venus-feeds.conf"
fi
cp -a "$R/etc/resolv.conf" "$W/resolv.conf.orig" 2>/dev/null || true
rm -f "$R/etc/resolv.conf"
cp /etc/resolv.conf "$R/etc/resolv.conf"
mount -t proc proc "$R/proc"
# /tmp -> /var/tmp -> /var/volatile/tmp only exists at runtime
mount -t tmpfs tmpfs "$R/var/volatile"
mkdir -p "$R/var/volatile/tmp" "$R/var/volatile/log"
# shellcheck disable=SC2086
chroot "$R" /bin/sh -c "opkg update && opkg install $(echo $CLOSED)"
missing=""
for p in $CLOSED; do
	chroot "$R" opkg status "$p" | grep -q 'install ok installed' || missing="$missing $p"
done
umount "$R/var/volatile"
umount "$R/proc"
rm -f "$R/etc/resolv.conf"
[ -e "$W/resolv.conf.orig" ] || [ -L "$W/resolv.conf.orig" ] && cp -a "$W/resolv.conf.orig" "$R/etc/resolv.conf"
rm -rf "$R/usr/lib/opkg/lists"/*
[ -z "$missing" ] || { echo "missing closed packages:$missing" >&2; exit 1; }

echo "== pack ${OUT}"
tar -C "$R" --numeric-owner --xattrs --acls -cpf - . | zstd -T0 -q -o "$OUT" -f
ls -la "$OUT"
