#!/bin/bash
# Build an x86_64 Debian trixie live system (netboot via iPXE + live-boot fetch=) that hosts the
# armv7 Venus OS rootfs in systemd-nspawn, emulated by qemu-user binfmt.
# Usage: sudo [VENUS_ROOTFS=venus-rootfs.tar.zst] ./build_ncr.sh <authorized_keys file> [out dir]
# Output: <out>/vmlinuz, <out>/initrd.img, <out>/filesystem.squashfs
set -euo pipefail

KEYS="${1:?usage: build_ncr.sh <authorized_keys> [out dir]}"
OUT="${2:-/var/www/html/netboot/venus}"
ROOT="${ROOT:-/srv/netboot/venus-ncr}"
HOSTNAME_NEW="${HOSTNAME_NEW:-venusncr}"
USER_NEW="${USER_NEW:-gh}"

PKGS="linux-image-amd64,live-boot,live-boot-initramfs-tools,busybox,systemd-sysv,systemd-container,systemd-resolved,\
qemu-user,qemu-user-binfmt,network-manager,openssh-server,sudo,locales,ca-certificates,curl,wget,can-utils,usbutils,\
iproute2,iputils-ping,less,nano,rsync,zstd,xz-utils,efibootmgr,ethtool,pciutils,mosquitto-clients"

echo "== bootstrap ${ROOT}"
rm -rf "${ROOT}"
mmdebstrap --variant=important --components="main contrib non-free-firmware" --include="${PKGS}" \
	trixie "${ROOT}" http://deb.debian.org/debian

echo "== configure"
echo "${HOSTNAME_NEW}" > "${ROOT}/etc/hostname"
printf '127.0.0.1\tlocalhost\n127.0.1.1\t%s\n::1\tlocalhost ip6-localhost ip6-loopback\n' "${HOSTNAME_NEW}" > "${ROOT}/etc/hosts"
sed -i 's/^# *de_DE.UTF-8/de_DE.UTF-8/' "${ROOT}/etc/locale.gen"
chroot "${ROOT}" locale-gen >/dev/null
echo 'LANG=de_DE.UTF-8' > "${ROOT}/etc/default/locale"
ln -sf /usr/share/zoneinfo/Europe/Berlin "${ROOT}/etc/localtime"
: > "${ROOT}/etc/fstab"   # never mount the local disk by accident (fsck on the real system)

# user with key-only ssh and passwordless sudo
chroot "${ROOT}" useradd -m -s /bin/bash -G sudo,dialout,systemd-journal "${USER_NEW}"
chroot "${ROOT}" passwd -l "${USER_NEW}" >/dev/null
install -d -m 700 -o 1000 -g 1000 "${ROOT}/home/${USER_NEW}/.ssh"
install -m 600 -o 1000 -g 1000 "${KEYS}" "${ROOT}/home/${USER_NEW}/.ssh/authorized_keys"
echo "${USER_NEW} ALL=(ALL) NOPASSWD:ALL" > "${ROOT}/etc/sudoers.d/010-${USER_NEW}-nopasswd"
chmod 440 "${ROOT}/etc/sudoers.d/010-${USER_NEW}-nopasswd"
printf 'PasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitRootLogin no\n' > "${ROOT}/etc/ssh/sshd_config.d/10-keys-only.conf"
rm -f "${ROOT}"/etc/ssh/ssh_host_*   # regenerated on first boot of every live boot

cat > "${ROOT}/etc/systemd/system/ssh-hostkeys.service" <<'EOF'
[Unit]
Description=Generate ssh host keys on the live system
Before=ssh.service
ConditionPathExists=!/etc/ssh/ssh_host_ed25519_key

[Service]
Type=oneshot
ExecStart=/usr/bin/ssh-keygen -A

[Install]
WantedBy=multi-user.target
EOF
chroot "${ROOT}" systemctl enable ssh-hostkeys.service ssh.service NetworkManager.service systemd-resolved.service >/dev/null

# serial console on ttyS0 at a fixed 115200 (agetty --keep-baud otherwise falls back to 9600)
install -d "${ROOT}/etc/systemd/system/serial-getty@ttyS0.service.d"
printf '[Service]\nExecStart=\nExecStart=-/sbin/agetty -o "-p -- \\\\u" 115200 %%I $TERM\n' \
	> "${ROOT}/etc/systemd/system/serial-getty@ttyS0.service.d/10-baud.conf"
chroot "${ROOT}" systemctl enable serial-getty@ttyS0.service >/dev/null

# NetworkManager: DHCP on eth0 (net.ifnames=0 on the kernel line); live-boot must not hand eth0 to ifupdown
cat > "${ROOT}/etc/NetworkManager/system-connections/eth0.nmconnection" <<'EOF'
[connection]
id=eth0
type=ethernet
interface-name=eth0
autoconnect=true

[ipv4]
method=auto

[ipv6]
method=auto
addr-gen-mode=eui64
EOF
chmod 600 "${ROOT}/etc/NetworkManager/system-connections/eth0.nmconnection"
printf '[main]\nno-auto-default=*\n[device]\nmatch-device=interface-name:eth0\nkeep-configuration=no\n' \
	> "${ROOT}/etc/NetworkManager/conf.d/10-eth0.conf"

# persistent Venus /data: any partition labelled VENUSDATA, otherwise tmpfs
install -d "${ROOT}/data"
cat > "${ROOT}/etc/systemd/system/data.mount" <<'EOF'
[Unit]
Description=Venus OS persistent data (partition labelled VENUSDATA)
ConditionPathExists=/dev/disk/by-label/VENUSDATA

[Mount]
What=/dev/disk/by-label/VENUSDATA
Where=/data
Type=ext4
Options=defaults,noatime,nofail

[Install]
WantedBy=local-fs.target
EOF
chroot "${ROOT}" systemctl enable data.mount >/dev/null

# Venus subsystem: unit, nspawn settings, udev rule; rootfs from host/prepare_venus_rootfs.sh if given
"$(dirname "$(readlink -f "$0")")/../host/install_venus.sh" "${ROOT}" ${VENUS_ROOTFS:+"${VENUS_ROOTFS}"}

echo "== initramfs + squashfs"
chroot "${ROOT}" update-initramfs -u -k all >/dev/null
chroot "${ROOT}" apt-get clean
install -d "${OUT}"
cp -L "${ROOT}/vmlinuz" "${OUT}/vmlinuz"
cp -L "${ROOT}/initrd.img" "${OUT}/initrd.img"
rm -f "${OUT}/filesystem.squashfs"
# no excludes: a fresh bootstrap has empty /proc /sys /dev /run, and emptying /dev breaks the boot
mksquashfs "${ROOT}" "${OUT}/filesystem.squashfs" -comp zstd -b 131072 -noappend >/dev/null
chmod 644 "${OUT}"/*
ls -la "${OUT}"
