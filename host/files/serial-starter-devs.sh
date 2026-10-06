#!/bin/sh
# nspawn subsystem: the container runs no udevd and the host only passes Victron ttys in.
# Create the /dev/serial-starter entries Venus' own udev rules would make; serial-starter removes
# each entry once it has taken the device over. Properties (VE_SERVICE) come from the host udev db.
[ "$1" = start ] || exit 0
mkdir -p /dev/serial-starter
for d in /dev/ttyUSB* /dev/ttyACM*; do
	[ -c "$d" ] || continue
	ln -sf "../${d#/dev/}" "/dev/serial-starter/${d#/dev/}"
done
