#!/bin/sh
# Lend the MK3-USB to a PC over the network (USB/IP) for VictronConnect / VEConfigure, and take it back.
#   mk3-remote.sh on    Venus releases the MK3 (serial-starter, mk2-dbus down), the MK3 is bound to usbip-host
#   mk3-remote.sh off   unbind, the kernel's ftdi_sio takes it back, Venus restarts its VE.Bus service
#   mk3-remote.sh status
# While "on", Venus has no VE.Bus connection: no DVCC/ESS control of the MultiPlus units.
# PC side: Linux "usbip attach -r <pi> -b <busid>", Windows usbip-win2, VirtualBox "usbdevsource add".
set -e
L=$(sudo machinectl show venus -p Leader --value)
V() { sudo nsenter -t "$L" -a "$@"; }
BUSID=$(for d in /sys/bus/usb/devices/*; do
  [ "$(cat "$d/idVendor" 2>/dev/null)" = 0403 ] && [ "$(cat "$d/idProduct" 2>/dev/null)" = 6015 ] &&
  [ "$(cat "$d/manufacturer" 2>/dev/null)" = VictronEnergy ] && basename "$d"; done | head -1)
[ -z "$BUSID" ] && { echo "MK3 not found"; exit 1; }
case "$1" in
on)
  V svc -d /service/serial-starter; V sh -c 'svc -d /service/mk2-dbus.tty*'; sleep 3
  sudo usbip bind -b "$BUSID"
  echo "MK3 exported as busid $BUSID on $(hostname -I | cut -d' ' -f1):3240" ;;
off)
  sudo usbip unbind -b "$BUSID" || true; sleep 3
  V svc -u /service/serial-starter; V sh -c 'svc -u /service/mk2-dbus.tty*' || true
  echo "MK3 back to Venus" ;;
status)
  echo "busid $BUSID"; sudo usbip list -l | grep -A1 "$BUSID" ;;
*) echo "usage: $0 on|off|status"; exit 1 ;;
esac
