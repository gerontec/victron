#!/bin/bash -e
# The first user gets passwordless sudo, as on the Raspberry Pi OS images before trixie.
F="${ROOTFS_DIR}/etc/sudoers.d/010_${FIRST_USER_NAME}-nopasswd"
echo "${FIRST_USER_NAME} ALL=(ALL) NOPASSWD: ALL" > "${F}"
chmod 440 "${F}"
