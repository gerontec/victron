#!/bin/sh
# Scripted VE.Bus Quick Configure / VEConfig session on the Pi display :1 (see setup.sh, run.sh).
# Coordinates are absolute on the 1280x800 Xvfb screen with the Wine virtual desktop (1260x780).
# Usage on the Pi: sh ess.sh <step> ...   e.g.  sh ess.sh release qc l1 vsoff gridde
export DISPLAY=:1
V() { L=$(sudo machinectl show venus -p Leader --value); sudo nsenter -t "$L" -a "$@"; }
shot() { xwd -root -silent > /tmp/scr.xwd; }          # copy /tmp/scr.xwd off the Pi to look at it
click() { xdotool mousemove "$1" "$2" click "${3:-1}"; sleep "${4:-4}"; }
NEXT_X=743; NEXT_Y=543                                    # "Next" in Quick Configure

dlg() { xdotool search --onlyvisible --name "^$1\$" | tail -1; }   # newest visible dialog with this title
ok() { W=$(dlg "$1"); [ -n "$W" ] && xdotool windowactivate --sync "$W" key "${2:-Return}" 2>/dev/null; sleep "${3:-4}"; }
for step in "$@"; do echo "$(date +%T) step $step"; case "$step" in
release)  # hand the MK3 from Venus to Wine
  V svc -d /service/serial-starter; V svc -d /service/mk2-dbus.ttyUSB2; sleep 3 ;;
restore)  # give the MK3 back to Venus
  pkill -f 'VE Configure tools' ; sleep 2; V svc -u /service/serial-starter ;;
qc)       # Quick Configure: Welcome -> Change settings (default) -> Com 3 (remembered) -> Switch all ON -> overview
  sh "$(dirname "$0")/xstart.sh"
  setsid nohup "$HOME/vec/run.sh" VEBusQuickConfigure.exe >/tmp/vqc.log 2>&1 </dev/null &
  sleep 50
  click $NEXT_X $NEXT_Y 1 6; click $NEXT_X $NEXT_Y 1 6     # Welcome, Choose action
  click $NEXT_X $NEXT_Y 1 8; click $NEXT_X $NEXT_Y 1 60    # Select comport (Com 3), Switch all ON -> reads system
  ;;
l1|l2|l3) # right-click phase image -> 2nd menu item "VEConfigure Multi" (1st is Toggle flash LEDs)
  case "$step" in l1) X=518;; l2) X=628;; l3) X=738;; esac
  click $X 370 3 3; xdotool key Down key Down key Return; sleep 45
  xdotool key Return; sleep 50                             # hidden first-start dialog, then device read
  ;;
vsoff)    # Virtual switch tab -> Usage: "Do not use VS" (assistants are disabled while VS is used)
  click 735 229; click 540 314 ;;
gridde)   # Grid tab -> Germany VDE-AR-N 4105:2018-11 internal NS protection (first selection: no password)
  click 580 229; click 720 299; click 600 332             # open list, pick "Other" -> Information box
  W=$(xdotool search --onlyvisible --name '^Information$' | head -1); [ -n "$W" ] && xdotool windowactivate --sync "$W" key Return
  click 720 299 1 3; xdotool key g; sleep 3                # 'g' jumps to Germany: VDE-AR-N 4105 internal
  ;;
assist)   # Assistants tab
  click 800 229 ;;
addess)   # Add assistant -> popup "Solar / Self-consumption" (5th) -> "ESS (Energy Storage System) (0190)" (1st)
  click 591 308 1 6; xdotool key Down Down Down Down Down Right; sleep 3; xdotool key Return; sleep 15 ;;
esswiz)   # Start assistant; screen origin of the wizard dialog is x=389, ">>" is always at (754,562)
  click 588 562 1 15
  click 754 562                                            # Welcome
  click 434 457 1 3; click 754 563                         # Battery system: LiFePo4 with other type BMS
  click 621 476 1 2; xdotool key End BackSpace BackSpace BackSpace BackSpace BackSpace BackSpace type 900; sleep 2
  click 754 562                                            # Battery capacity 900 Ah (300 CAN + 600 EBox)
  click 754 561                                            # change battery type as suggested (default)
  click 754 562                                            # sustain voltage 50.00 V (default)
  click 754 562                                            # dynamic cut-off (defaults)
  click 754 562                                            # restart offset 1.20 V (default)
  click 754 562                                            # PV inverters on AC out: No (default)
  click 754 562                                            # VEConfig settings summary
  W=$(xdotool search --onlyvisible --name '^Information$' | tail -1); [ -n "$W" ] && xdotool windowactivate --sync "$W" key Return
  sleep 4 ;;
save)     # File -> Save settings (Ctrl+S) to Z:\home\pi\vebus-backup\ess_<SAVE_NAME>.vsc (absolute: the dialog remembers its folder)
  xdotool key ctrl+s; sleep 5; xdotool type --delay 40 "Z:\\home\\pi\\vebus-backup\\ess_${SAVE_NAME:-unit}.vsc"; sleep 1; xdotool key Return; sleep 6
  ok Error Return 2                                        # "Error creating file!" would otherwise block the next steps
  ls -la "$HOME/vebus-backup/ess_${SAVE_NAME:-unit}.vsc" ;;
sendall)  # Send settings: modified settings -> all devices (VS off, grid code, charger; NOT assistants), no reset yet
  click 458 557 1 6; click 521 419 1 2; click 720 519 1 45
  ok Warning Return 45                                     # "assistant setup will not be sent ... only 'this device'"
  ok Warning Return 50                                     # "grid code related settings will be sent to other devices"
  ok Confirm n 4 ;;                                        # "needs to be reset ... reset now?" -> No (reset once at the end)
sendthis) # Send settings: modified settings -> this device (assistants only go out this way)
  click 458 557 1 6; click 521 398 1 2; click 720 519 1 45
  ok Confirm y 50                                          # "send the assistant setup to the device?" -> Yes (unit switches off)
  ok Information Return 4 ;;                               # "Assistant setup succesfully written to target."
close)    # close VEConfig: File menu -> Exit (last entry). Never Alt+F4: Wine then shows "Warten auf Programm".
  click 329 200 1 2; xdotool key Up Return; sleep 6 ;;
*) echo "unknown step $step"; exit 1 ;;
esac; done
shot
