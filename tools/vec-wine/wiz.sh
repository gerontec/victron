#!/bin/sh
# wiz.sh - single steps inside the open ESS assistant wizard of VEConfig (Pi display :1), one screenshot per step,
# so every page can be checked before the next click (Pytes LiFePO4 values, 2026-10-24). Sends nothing.
#   wiz.sh start              Assistants tab: "Start assistant" (ESS must be the only/selected assistant)
#   wiz.sh next               ">>" on the current page
#   wiz.sh set X Y VALUE      click the edit field at X,Y relative to the ESS window, replace its text with VALUE
#   wiz.sh tab VALUE          Tab to the next edit field and replace its text with VALUE
#   wiz.sh shot NAME          screenshot of the newest dialog to /tmp/pytes49/NAME.png
# Every step writes /tmp/pytes49/<step>_<time>.png and logs to ~/vec/RESUME.md.
cd "$HOME/vec" || exit 1
export DISPLAY=:1
O=/tmp/pytes49; mkdir -p $O
log() { echo "$(date +%T) $*"; echo "- $(date '+%F %T') pytes49: $*" >> "$HOME/vec/RESUME.md"; }
shotdlg() {
  if sh dlgshot.sh >/dev/null 2>&1; then python3 xwd2png.py /tmp/dlg.xwd "$O/$1.png"
  else xwd -root -silent > /tmp/scr.xwd; python3 xwd2png.py /tmp/scr.xwd "$O/$1.png"; fi
  echo "$O/$1.png"; }
win() { xdotool search --onlyvisible --name "^ESS \(Energy Storage System\)$" | tail -1; }
replace() { xdotool key End; for i in 1 2 3 4 5 6 7 8; do xdotool key BackSpace; done; xdotool type --delay 80 "$1"; sleep 1; }
T=$(date +%H%M%S)
case "$1" in
start) xdotool mousemove 588 562 click 1; sleep 15; shotdlg "start_$T"; log "wizard started" ;;
next)  W=$(win); [ -n "$W" ] || { echo "no ESS window"; shotdlg "nowin_$T"; exit 1; }
       H=$(xdotool getwindowgeometry --shell "$W" | sed -n "s/^HEIGHT=//p")
       xdotool mousemove --window "$W" 364 $((H-34)) click 1; sleep 4; shotdlg "next_$T" ;;
set)   W=$(win); [ -n "$W" ] || { echo "no ESS window"; exit 1; }
       xdotool mousemove --window "$W" "$2" "$3" click 1; sleep 1; replace "$4"; shotdlg "set_${4}_$T"; log "set field $2,$3 = $4" ;;
tab)   W=$(win); [ -n "$W" ] || { echo "no ESS window"; exit 1; }
       xdotool key Tab; sleep 1; replace "$2"; shotdlg "tab_${2}_$T"; log "tab field = $2" ;;
shot)  shotdlg "${2:-shot}_$T" ;;
*)     sed -n '2,9p' "$0"; exit 1 ;;
esac
