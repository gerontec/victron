#!/bin/bash
# Install the Venus subsystem into a Debian host root: unit, nspawn settings, udev rule, start/stop helpers,
# and (optionally) the rootfs tarball from prepare_venus_rootfs.sh into /var/lib/machines/venus.
# Works on a running host (TARGET=/) and on an image chroot (pi-gen, mmdebstrap).
# Usage: sudo ./install_venus.sh <target root> [venus-rootfs.tar.zst]
set -euo pipefail
T="${1:?usage: install_venus.sh <target root> [venus-rootfs.tar.zst]}"
TARBALL="${2:-}"
F=$(dirname "$(readlink -f "$0")")/files
T="${T%/}"

install -D -m 755 "$F/venus-start" "$T/usr/local/sbin/venus-start"
install -D -m 755 "$F/venus-stop" "$T/usr/local/sbin/venus-stop"
install -D -m 644 "$F/venus.service" "$T/etc/systemd/system/venus.service"
install -D -m 644 "$F/venus.nspawn" "$T/etc/systemd/nspawn/venus.nspawn"
install -D -m 644 "$F/90-venus-serial-starter.rules" "$T/etc/udev/rules.d/90-venus-serial-starter.rules"
install -d -m 755 "$T/data"
install -d -m 700 "$T/var/lib/machines"
install -d "$T/etc/systemd/system/multi-user.target.wants"
ln -sf /etc/systemd/system/venus.service "$T/etc/systemd/system/multi-user.target.wants/venus.service"

if [ -n "$TARBALL" ]; then
	M="$T/var/lib/machines/venus"
	rm -rf "$M"
	install -d -m 755 "$M"
	if command -v zstd >/dev/null; then
		zstd -dc "$TARBALL" | tar -C "$M" --numeric-owner --xattrs --acls -xpf -
	else
		# pi-gen's build container has no zstd, but bsdtar (libarchive-tools) reads it
		bsdtar -C "$M" --numeric-owner --xattrs --acls -xpf "$TARBALL"
	fi
	# /data is bound from the host: seed it with the skeleton Venus expects (conf, db, log, ...)
	cp -an "$M/data/." "$T/data/"
fi
echo "Venus subsystem installed into ${T:-/}"
