#!/bin/bash -e
# Venus OS subsystem: host unit/udev/nspawn files from host/ and the prepared rootfs
# (host/prepare_venus_rootfs.sh) in files/venus-rootfs.tar.zst.
TARBALL="files/venus-rootfs.tar.zst"
if [ ! -f "${TARBALL}" ]; then
	echo "03-venus: ${TARBALL} missing, installing the host side only"
	TARBALL=""
fi
files/host/install_venus.sh "${ROOTFS_DIR}" ${TARBALL:+"${TARBALL}"}
