#!/bin/sh
# One-time setup on the Pi (Debian trixie arm64): run Victron VE Configure tools under box64 + Wine,
# headless on Xvfb :1 (VNC on localhost:5901), with only the MK3 visible as COM3.
# Usage on the Pi: sh setup.sh /path/to/VECSetup_B.exe
set -e
INST="${1:?path to VECSetup_B.exe}"
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends \
  box64 xvfb x11vnc openbox xcompmgr xdotool x11-apps xterm \
  libfreetype6 libfontconfig1 fonts-dejavu-core libx11-6 libxext6 libxrender1 libxrandr2 libxi6 \
  libxcursor1 libxcomposite1 libxinerama1 libxfixes3 libgnutls30t64 libdbus-1-3 libgl1
mkdir -p ~/vec ~/wine-dl
if [ ! -x ~/wine/bin/wine64 ]; then
  curl -sSL -o ~/wine-dl/wine.tar.xz https://github.com/Kron4ek/Wine-Builds/releases/download/10.0/wine-10.0-amd64-wow64.tar.xz
  mkdir -p ~/wine && tar -xJf ~/wine-dl/wine.tar.xz -C ~/wine --strip-components=1 && rm ~/wine-dl/wine.tar.xz
fi
cat > ~/vec/env.sh <<'E2'
export DISPLAY=:1
export WINEPREFIX=$HOME/.wine-vec
export WINEDEBUG=-all
export PATH=$HOME/wine/bin:$PATH
E2
. ~/vec/env.sh
sh "$(dirname "$0")/xstart.sh"
box64 wine64 wineboot -i >/dev/null 2>&1 &
# the Mono dialog blocks wineboot: cancel it, VE tools do not need .NET
for i in $(seq 1 60); do W=$(xdotool search --name "Wine-Mono" 2>/dev/null | head -1); [ -n "$W" ] && break; sleep 5; done
[ -n "$W" ] && xdotool windowactivate --sync "$W" key Escape
wait
box64 wine64 "$INST" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART >/dev/null 2>&1
mkdir -p "$WINEPREFIX/drive_c/ProgramData/VE Configure tools/Assistants"
cp "$WINEPREFIX/drive_c/users/Public/VE Configure tools/Assistants/"*.vfp "$WINEPREFIX/drive_c/ProgramData/VE Configure tools/Assistants/"
# virtual desktop (popups render) and COM3 = MK3
printf 'Windows Registry Editor Version 5.00\r\n\r\n[HKEY_CURRENT_USER\\Software\\Wine\\Explorer]\r\n"Desktop"="Default"\r\n\r\n[HKEY_CURRENT_USER\\Software\\Wine\\Explorer\\Desktops]\r\n"Default"="1260x780"\r\n\r\n[HKEY_LOCAL_MACHINE\\Software\\Wine\\Ports]\r\n"COM3"="/dev/ttyUSB30"\r\n' > ~/vec/setup.reg
box64 wine64 regedit /S 'Z:\home\pi\vec\setup.reg'
box64 wineserver -k
ln -sfn /dev/ttyUSB30 "$WINEPREFIX/dosdevices/com3"
cp "$(dirname "$0")/run.sh" ~/vec/run.sh; chmod +x ~/vec/run.sh
echo "setup done"
