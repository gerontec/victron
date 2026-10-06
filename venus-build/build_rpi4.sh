#!/bin/bash
# Build Venus OS v3.80 large image for raspberrypi4 inside the venus-build:24.04 container.
# Private layer is absent: closed packages (mk2-dbus, vecan-dbus, hub4control, ...) come later from the official feed.
set -e
cd /home/gh/venus
export MACHINE=raspberrypi4
make build/conf/bblayers.conf
. ./sources/openembedded-core/oe-init-build-env build sources/bitbake
bitbake -k venus-image-large
