#!/bin/sh
# Run a VE Configure tool under box64/wine. ttyUSB0 (Zenner M-Bus) and ttyUSB1 (EBox console) are
# replaced by /dev/null in a private mount namespace, so a COM port scan cannot disturb them.
# usage: run.sh VEBusQuickConfigure.exe [args]
EXE="$1"; shift
sudo unshare -m sh -c "mount --bind /dev/null /dev/ttyUSB0; mount --bind /dev/null /dev/ttyUSB1; \
  exec sudo -u pi env DISPLAY=:1 WINEPREFIX=/home/pi/.wine-vec WINEDEBUG=-all PATH=/home/pi/wine/bin:/usr/bin:/bin \
  box64 wine64 \"C:\\\\Program Files (x86)\\\\VE Configure tools\\\\$EXE\" $*"
