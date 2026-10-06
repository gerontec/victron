#!/bin/sh
# Overwrite the boot device of a RUNNING Linux machine with an image and reboot into it.
# Runs on the target as root. The image (.img.xz) must already be in RAM (/dev/shm).
# Everything needed after the disk is gone (loader, libc, liblzma, dash, xz, dd) is copied to RAM first:
# once dd overwrites the root filesystem, binaries paged out from the old disk would crash.
# Before writing, sysrq-u remounts every filesystem read-only, so no dirty page of the old system is
# written back over the new image afterwards. If anything fails, the machine needs physical access.
# Usage: sudo sh remote_flash.sh /dev/shm/image.img.xz /dev/mmcblk0
set -eu
IMG="${1:?usage: remote_flash.sh <image.img.xz in /dev/shm> <device>}"
DEV="${2:?usage: remote_flash.sh <image.img.xz in /dev/shm> <device>}"
D=/dev/shm/flash
case "$IMG" in /dev/shm/*) ;; *) echo "image must be in /dev/shm" >&2; exit 1 ;; esac
[ -b "$DEV" ] || { echo "no block device $DEV" >&2; exit 1; }
xz -t "$IMG"

mkdir -p "$D"
LD=$(readlink -f /lib/ld-linux-aarch64.so.1 2>/dev/null || readlink -f /lib64/ld-linux-x86-64.so.2)
for b in dash xz dd; do
	cp "$(command -v $b)" "$D/"
	ldd "$(command -v $b)" | awk '$3 ~ /^\// {print $3}' | while read -r l; do cp -L "$l" "$D/"; done
done
cp -L "$LD" "$D/ld.so"

cat > "$D/run.sh" <<EOF
L="$D/ld.so --library-path $D"
echo u > /proc/sysrq-trigger
sleep 2
\$L $D/xz -dc "$IMG" | \$L $D/dd of="$DEV" bs=4M conv=fsync 2>$D/dd.log
echo "dd rc=\$?" >> $D/dd.log
sleep 1
echo b > /proc/sysrq-trigger
EOF
echo "flashing $IMG to $DEV, the machine reboots when done"
# detached from ssh: the session dies with the old system
nohup "$D/ld.so" --library-path "$D" "$D/dash" "$D/run.sh" >/dev/null 2>&1 &
