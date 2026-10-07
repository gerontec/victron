#!/bin/sh
# Fixed ESS feed-in from the battery via the MultiPlus AC-in until a given time, then back to normal ESS.
# Runs on the Pi host; Venus is the nspawn container "venus". Needs the ESS assistant (/Hub4/AssistantId = 5).
# usage: feedin.sh <watts_per_phase> <HH:MM end>     e.g. feedin.sh 333 08:15
W=${1:?watts per phase}; END=${2:?end time HH:MM}
L=$(sudo machinectl show venus -p Leader --value)
V() { sudo nsenter -t "$L" -a dbus -y "$@" >/dev/null; }
VB=com.victronenergy.vebus.ttyUSB2
end=$(date -d "$END" +%s); [ "$end" -le "$(date +%s)" ] && end=$(date -d "tomorrow $END" +%s)
echo "$(date +%T) feed-in ${W} W per phase until $(date -d @$end '+%F %T')"
trap 'for p in L1 L2 L3; do V $VB /Hub4/$p/AcPowerSetpoint SetValue 0; done; V com.victronenergy.settings /Settings/CGwacs/Hub4Mode SetValue 1; echo "$(date +%T) stopped, back to Hub4Mode 1"; exit' INT TERM
while [ "$(date +%s)" -lt "$end" ]; do
  V com.victronenergy.settings /Settings/CGwacs/Hub4Mode SetValue 3    # external control, re-asserted every cycle
  for p in L1 L2 L3; do
    V $VB /Hub4/$p/MaxFeedInPower SetValue $((W + 100))
    V $VB /Hub4/$p/AcPowerSetpoint SetValue -- -$W                    # negative = power out of AC-in
  done
  sleep 10 & wait $!                                                # wait: a TERM is handled at once, not after the sleep
done
kill -TERM $$
