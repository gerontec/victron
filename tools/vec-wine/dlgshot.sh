#!/bin/sh
# Capture the newest visible Wine dialog on :1 (Xvfb does not composite some Wine windows into the root image).
# Prints "<id> <title> <x>,<y> <w>x<h>" and writes /tmp/dlg.xwd
export DISPLAY=:1
best=""
for w in $(xdotool search --onlyvisible --name "" 2>/dev/null); do
  g=$(xdotool getwindowgeometry "$w" 2>/dev/null | sed -n 's/.*Geometry: //p'); W=${g%x*}; H=${g#*x}
  [ "${W:-0}" -ge 150 ] && [ "${H:-0}" -ge 60 ] || continue
  case "$g" in 1280x800|1262x800|1260x779|636x500) continue;; esac
  best=$w
done
[ -z "$best" ] && { echo none; exit 1; }
p=$(xdotool getwindowgeometry "$best" | sed -n 's/.*Position: \([0-9,]*\).*/\1/p')
echo "$best [$(xdotool getwindowname "$best")] $p $(xdotool getwindowgeometry "$best" | sed -n 's/.*Geometry: //p')"
xwd -id "$best" -silent > /tmp/dlg.xwd
