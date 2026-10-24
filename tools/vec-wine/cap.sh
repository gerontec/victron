#!/bin/sh
# Capture all VEConfig tabs, ESS summary and virtual panel of the open unit into /tmp/cap/<name>_*.xwd
export DISPLAY=:1; N=$1; O=/tmp/cap; mkdir -p $O; cd ~/vec
W=$(xdotool search --onlyvisible --name '^Warning$' | tail -1); [ -n "$W" ] && xdotool windowactivate --sync $W key Return; sleep 3
xdotool mousemove 452 509 click 1; sleep 60                      # Get settings (incl. assistants)
W=$(xdotool search --onlyvisible --name '^Warning$' | tail -1); [ -n "$W" ] && { sh dlgshot.sh; cp /tmp/dlg.xwd $O/${N}_getwarn.xwd; xdotool windowactivate --sync $W key Return; sleep 3; }
for t in general:533 grid:580 inverter:624 charger:672 vswitch:735 advanced:860 assistants:800; do
  xdotool mousemove ${t#*:} 229 click 1; sleep 3; xwd -root -silent > $O/${N}_${t%:*}.xwd; done
xdotool mousemove 574 597 click 1; sleep 5; sh dlgshot.sh; cp /tmp/dlg.xwd $O/${N}_summary.xwd
W=$(xdotool search --onlyvisible --name '^Information$' | tail -1); [ -n "$W" ] && xdotool windowactivate --sync $W key Return; sleep 2
xdotool mousemove 457 608 click 1; sleep 5
W=$(xdotool search --onlyvisible --name '^Warning$' | tail -1); [ -n "$W" ] && xdotool windowactivate --sync $W key Return; sleep 8
sh dlgshot.sh; cp /tmp/dlg.xwd $O/${N}_panel.xwd
xdotool mousemove 853 278 click 1; sleep 2; xdotool key Down Return; sleep 3
sh ess.sh close
