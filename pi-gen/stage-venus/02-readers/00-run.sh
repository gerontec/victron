#!/bin/bash -e
# ebox + Zenner readers 1:1 from the old Pi: files/src holds python/, sofar/ and 99-usb-serial.rules (site config, not in git).
SRC="files/src"
if [ ! -f "${SRC}/python/zenner2db.py" ]; then
	echo "02-readers: ${SRC} missing, readers not installed"
	exit 0
fi
rm -rf "${ROOTFS_DIR}/tmp/readers"
install -d "${ROOTFS_DIR}/tmp/readers"
cp -a "${SRC}/." "${ROOTFS_DIR}/tmp/readers/"
install -m 755 files/install_readers.sh "${ROOTFS_DIR}/tmp/readers/install_readers.sh"
[ -f files/victron2db.py ] && install -m 755 files/victron2db.py "${ROOTFS_DIR}/tmp/readers/victron2db.py"
on_chroot <<EOC
bash /tmp/readers/install_readers.sh /tmp/readers
rm -rf /tmp/readers
EOC
