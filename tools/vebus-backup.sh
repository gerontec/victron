#!/bin/sh
# Read the complete VE.Bus configuration (all units, incl. assistants) through Venus' MK3 tunnel with
# Victron's mk2vsc and copy it to ~/vebus-backup on the Pi host.  usage: vebus-backup.sh <name>
N=${1:?name}; L=$(sudo machinectl show venus -p Leader --value); V() { sudo nsenter -t "$L" -a "$@"; }
T=dbus://$(V dbus -y | grep -m1 "^com.victronenergy.vebus\.")/Interfaces/Mk2/Tunnel; F=/data/vebus-backup/$N
V mkdir -p /data/vebus-backup; mkdir -p ~/vebus-backup
for i in 1 2 3; do                      # the first read right after another mk2vsc call can fail
  V /opt/victronenergy/mk2vsc/mk2vsc --cache /tmp/ -r -f "$F" -s "$T" >/tmp/mk2vsc_r.log 2>&1
  for e in rvms rvsc; do V test -s "$F.$e" && { V cat "$F.$e" > ~/vebus-backup/"$N.$e"; X=$e; }; done
  [ -n "$X" ] && break; sleep 5
done
[ -z "$X" ] && { echo "read failed"; cat /tmp/mk2vsc_r.log; exit 1; }
ls -la ~/vebus-backup/"$N.$X"
V /opt/victronenergy/mk2vsc/mk2vsc -L -f "$F.$X" 2>&1 | grep -E "Firmware|Unique|device"
echo "assistant sections: $(strings -n 5 ~/vebus-backup/"$N.$X" | grep -c AssistantInfo)"
