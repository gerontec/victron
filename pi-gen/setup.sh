#!/bin/bash
# Copy stage-venus into a pi-gen checkout and skip the plain Lite export of stage2.
set -e
PIGEN="${1:?usage: setup.sh /path/to/pi-gen}"
HERE="$(cd "$(dirname "$0")" && pwd)"
cp -r "${HERE}/stage-venus" "${PIGEN}/"
install -m 755 "${HERE}/../readers/install_readers.sh" "${PIGEN}/stage-venus/02-readers/files/install_readers.sh"
rm -rf "${PIGEN}/stage-venus/03-venus/files/host"
cp -r "${HERE}/../host" "${PIGEN}/stage-venus/03-venus/files/host"
touch "${PIGEN}/stage2/SKIP_IMAGES"
grep -qx "stage-venus/01-wifi/files/wifi.env" "${PIGEN}/.gitignore" 2>/dev/null || echo "stage-venus/01-wifi/files/wifi.env" >> "${PIGEN}/.gitignore"
echo "stage-venus installed in ${PIGEN}; now create ${PIGEN}/config from config.example"
echo "and put the output of host/prepare_venus_rootfs.sh at ${PIGEN}/stage-venus/03-venus/files/venus-rootfs.tar.zst"
