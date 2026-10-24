#!/bin/sh
# cap600.sh - ESS assistant battery capacity 10 Ah -> 600 Ah (2 x 300 Ah = 30 kWh) on L1, L2, L3 (todo.md).
# Builds on ess.sh (same 1280x800 Xvfb coordinates). Every step is logged to ~/vec/RESUME.md.
#   cap600.sh pre                backup pre_cap600_<date> + dim_config, precheck (VE.Bus error 0)
#   cap600.sh dry l1|l2|l3       open unit, walk the existing ESS assistant page by page, screenshot each page to
#                                /tmp/cap600/<unit>_dry_<n>.png, cancel the wizard - nothing is sent
#   cap600.sh run l1|l2|l3       same walk, capacity field -> 600, finish wizard, send assistant to this unit only
#   cap600.sh finish             close VEConfig, MK3 back to Venus (serial-starter + mk2-dbus), backup post_cap600, dim_config
#   cap600.sh status             VE.Bus state / error / mode
# Never changes Hub4Mode or VE.Bus /Mode. Stop on anything unexpected and run "finish".
cd "$HOME/vec" || exit 1
export DISPLAY=:1
O=/tmp/cap600; mkdir -p $O
D=$(date +%Y%m%d)
log() { echo "$(date +%T) $*"; echo "- $(date '+%F %T') cap600: $*" >> "$HOME/vec/RESUME.md"; }
V() { L=$(sudo machinectl show venus -p Leader --value); sudo nsenter -t "$L" -a "$@"; }
vb() { V dbus -y 2>/dev/null | grep -m1 "^com.victronenergy.vebus\."; }
g() { V dbus -y "$(vb)" "$1" GetValue 2>/dev/null | sed 's/value = //'; }
click() { xdotool mousemove "$1" "$2" click "${3:-1}"; sleep "${4:-4}"; }
NEXT_X=754; NEXT_Y=562                        # ">>" in the assistant wizard (ess.sh esswiz)
shotdlg() { # newest Wine dialog, else whole screen -> $O/$1.png
  if sh dlgshot.sh >/dev/null 2>&1; then python3 xwd2png.py /tmp/dlg.xwd "$O/$1.png"
  else xwd -root -silent > /tmp/scr.xwd; python3 xwd2png.py /tmp/scr.xwd "$O/$1.png"; fi; }

open_unit() { # $1 = l1|l2|l3 ; Quick Configure is started once (release qc), later units reuse it after "close"
  if ! pgrep -f VEBusQuickConfigure >/dev/null; then sh ess.sh release qc >>/tmp/ess_run.log 2>&1; fi
  pgrep -f VEConfig.exe >/dev/null && sh ess.sh close >>/tmp/ess_run.log 2>&1
  sh ess.sh "$1" get assist >>/tmp/ess_run.log 2>&1
  xwd -root -silent > /tmp/scr.xwd; python3 xwd2png.py /tmp/scr.xwd "$O/${1}_${2}_assist.png" 380 150 900 590
}

walk() { # $1 unit, $2 dry|run ; ESS is the only (selected) assistant -> "Start assistant"
  click 588 562 1 15; shotdlg "${1}_${2}_00"
  n=1
  while [ $n -le 9 ]; do
    if [ $n -eq 3 ] && [ "$2" = run ]; then   # shown page = _02 "Battery capacity" (seen in dry l1 2026-10-22)
      W=$(xdotool search --onlyvisible --name "^ESS \(Energy Storage System\)$" | tail -1)
      xdotool mousemove --window "$W" 232 145 click --repeat 3 1; sleep 1
      xdotool key End BackSpace BackSpace BackSpace BackSpace BackSpace BackSpace; xdotool type 600; sleep 2
      shotdlg "${1}_${2}_$(printf %02d $n)_cap600"
    fi
    if [ "$2" = dry ] && [ $n -eq 8 ]; then break; fi   # dry: stop before the summary/finish page
    click $NEXT_X $NEXT_Y 1 4; shotdlg "${1}_${2}_$(printf %02d $n)"
    n=$((n+1))
  done
}

case "$1" in
pre)
  [ "$(g /VebusError)" = 0 ] || { log "pre: VE.Bus error $(g /VebusError) - abort"; exit 1; }
  sudo pkill -f 'VE Configure tools' 2>/dev/null; sleep 2
  sh vebus-backup.sh "pre_cap600_$D" && python3 "$HOME/python/rvms2db.py" "$HOME/vebus-backup/pre_cap600_$D.rvms"
  log "pre: backup pre_cap600_$D, dim_config geladen, State=$(g /State) Err=$(g /VebusError) Mode=$(g /Mode)" ;;
dry|run)
  U=${2:?l1|l2|l3}; case "$U" in l1|l2|l3) ;; *) echo "unit l1|l2|l3"; exit 1;; esac
  log "$1 $U: start"
  open_unit "$U" "$1"
  walk "$U" "$1"
  if [ "$1" = dry ]; then
    xdotool key Escape; sleep 3; W=$(xdotool search --onlyvisible --name '^Confirm$' | tail -1)
    [ -n "$W" ] && xdotool windowactivate --sync "$W" key y; sleep 3; shotdlg "${U}_dry_cancel"
    log "dry $U: Assistent abgebrochen, nichts gesendet. Screenshots $O/${U}_dry_*.png"
  else
    W=$(xdotool search --onlyvisible --name '^Information$' | tail -1); [ -n "$W" ] && xdotool windowactivate --sync "$W" key Return; sleep 4
    click 574 597 1 6; shotdlg "${U}_run_summary"        # ESS summary: must show 600 Ah
    W=$(xdotool search --onlyvisible --name '^Information$' | tail -1); [ -n "$W" ] && xdotool windowactivate --sync "$W" key Return; sleep 2
    sh ess.sh sendthis >>/tmp/ess_run.log 2>&1; shotdlg "${U}_run_sent"
    log "run $U: Kapazitaet 600 Ah gesetzt und an dieses Geraet gesendet (Screenshots $O/${U}_run_*.png)"
  fi ;;
finish)
  pgrep -f VEConfig.exe >/dev/null && sh ess.sh close >>/tmp/ess_run.log 2>&1
  sh ess.sh restore >>/tmp/ess_run.log 2>&1; sudo pkill -f 'VE Configure tools' 2>/dev/null
  sleep 10; V sh -c 'svc -u /service/mk2-dbus.ttyUSB*'; sleep 40
  log "finish: MK3 an Venus, State=$(g /State) Err=$(g /VebusError) Mode=$(g /Mode) In=$(g /Ac/ActiveIn/Connected)"
  [ "$2" = nobackup ] || { sh vebus-backup.sh "post_cap600_$D" && python3 "$HOME/python/rvms2db.py" "$HOME/vebus-backup/post_cap600_$D.rvms"; } ;;
status)
  echo "vebus=$(vb) State=$(g /State) Err=$(g /VebusError) Mode=$(g /Mode) In=$(g /Ac/ActiveIn/Connected)"
  printf "Hub4Mode="; V dbus -y com.victronenergy.settings /Settings/CGwacs/Hub4Mode GetValue ;;
*) sed -n '2,12p' "$0"; exit 1 ;;
esac
