# VE Configure tools on the Pi (box64 + Wine), scripted ESS

Victron's VE.Bus configuration tools (VE.Bus Quick Configure, VEConfig) are Windows-only GUI programs.
These scripts run them headless on the Raspberry Pi 4 that hosts Venus OS, talk to the MultiPlus units
through the MK3 that Venus normally uses, and drive the GUI with `xdotool`, so ESS can be put into a
three-phase system without moving the MK3 to a Windows PC.

| File | Purpose |
|---|---|
| `setup.sh` | one-time install: box64, Xvfb/openbox/xcompmgr/x11vnc, Wine 10.0 wow64 (Kron4ek), VE Configure tools (`VECSetup_B.exe`, silent), assistant packs, Wine virtual desktop, COM3 = `/dev/ttyUSB30` (fixed MK3 name, host/99-victron-mk3.rules) |
| `xstart.sh` | start the display stack on `:1` (VNC on localhost:5901) |
| `run.sh` | start a VE tool; `ttyUSB0`/`ttyUSB1` (M-Bus, EBox console) are bind-mounted to `/dev/null` so a COM scan cannot disturb them |
| `ess.sh` | the recorded GUI steps (see below) |
| `dlgshot.sh` | capture the newest Wine dialog to `/tmp/dlg.xwd` (some dialogs are not composited into the root image) |
| `../feedin.sh` | fixed feed-in per phase via `/Hub4/Lx/AcPowerSetpoint` (Hub4Mode 3) until a given time |

## Roll-out on a new three-phase system (3 units, same model and firmware)

```sh
sh setup.sh ~/VECSetup_B.exe                 # once per Pi
cd ~/vec
sh ess.sh release qc l1 vsoff gridde assist addess esswiz sendall
SAVE_NAME=L1 sh ess.sh save sendthis close
SAVE_NAME=L2 sh ess.sh l2 assist addess esswiz save sendthis close
SAVE_NAME=L3 sh ess.sh l3 assist addess esswiz save sendthis close
sh ess.sh restore                            # MK3 back to Venus
sudo nsenter -t $(sudo machinectl show venus -p Leader --value) -a sh -c "svc -u /service/mk2-dbus.ttyUSB*"
```

Then check in Venus that `/Hub4/AssistantId` = 5 on `com.victronenergy.vebus.ttyUSB*`.

Prerequisites and lessons from the first run (2026-10-07):

- The units must already form the three-phase system (Quick Configure shows "System is correctly
  configured!" with phase L1/L2/L3). `qc` assumes "Change settings of an existing VE.Bus system";
  setting up a new system ("Setup a VE.Bus system") is not scripted yet.
- Assistants are disabled while the Virtual Switch is used: `vsoff` selects "Do not use VS".
- The first selection of a grid code needs no password ("Once sent to the device, a password will be
  required to change or disable"); `gridde` picks Germany VDE-AR-N 4105:2018-11, internal NS protection.
- `sendall` (Send where: all devices) sends VS off, grid code and charger settings to all three, but
  never assistants; ESS only goes out with `sendthis` (this device), once per unit.
- The ESS wizard answers: LiFePo4 with other type BMS, 900 Ah, change battery type as suggested,
  sustain 50.00 V, dynamic cut-off and restart offset default, no PV inverters on AC out.
- Without `xcompmgr` Wine draws popups and dialogs black; popup menus stay black even with it, so
  menu entries are chosen by keyboard (order taken from the program: context menu = Toggle flash LEDs,
  VEConfigure Multi; Add assistant = ... Solar / Self-consumption > ESS (Energy Storage System) (0190)).
- Never close VEConfig with Alt+F4 (Wine "Warten auf Programm" dialog); `close` uses File > Exit.
- Over ssh, `pkill -f`/`pgrep -f` with the script name also match the ssh command line itself.

The coordinates in `ess.sh` are absolute on the 1280x800 Xvfb screen with the 1260x780 Wine virtual
desktop; they hold as long as the window sizes do not change.
