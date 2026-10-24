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
| `xwd2png.py` | convert an xwd screenshot to PNG with the Python standard library (no ImageMagick/PIL on the Pi), optional crop `x y w h` |
| `wiz.sh` | single steps inside the open ESS wizard (`start`, `next`, `set X Y VALUE`, `tab VALUE`, `shot NAME`), one PNG per step in `/tmp/pytes49/`; sends nothing |
| `cap600.sh` | battery capacity change on all three units (`pre`/`dry`/`run`/`finish`/`status`), used for 600 Ah on 2026-10-23 |
| `cap.sh` | capture all VEConfig tabs, ESS summary and virtual panel of the open unit to `/tmp/cap/` |
| `../vebus-backup.sh` | read the complete VE.Bus configuration (incl. assistants) through Venus' MK3 tunnel with mk2vsc to `~/vebus-backup/<name>.rvms` |
| `nightly-config.sh` | cron 03:30: `vebus-backup.sh` + `rvms2db.py` into `wagodb.dim_config`; skips while Wine holds the MK3 (no vebus service) |
| `mk3-remote.sh` | lend the MK3 to a PC over USB/IP and take it back |
| `env.sh` | DISPLAY/WINEPREFIX/PATH for manual use |
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

## Changing one setting on one unit (AES off on L2, 2026-10-21)

The firmware update to 568 (2026-10-07) left phase L2 (HQ2606Y2Y4U) with AES enabled while L1/L3 had it
off; a three-phase system needs identical settings. Done step by step, a screenshot before every click:

```sh
cd ~/vec
sh vebus-backup.sh pre_aes_20261021                        # backup through Venus first
setsid nohup sh ess.sh release qc l2 get inverter >/tmp/ess_run.log 2>&1 </dev/null &   # ~6 min, wait for it
python3 xwd2png.py /tmp/scr.xwd /tmp/scr.png               # check: title 'phase L2' + serial, AES ticked
sh ess.sh aestoggle sendmod close restore
sudo nsenter -t $(sudo machinectl show venus -p Leader --value) -a sh -c "svc -u /service/mk2-dbus.ttyUSB*"
sh vebus-backup.sh post_aes_20261021                       # flags0 bit 8 (DisableAES) now set on all three
python3 ~/python/rvms2db.py /data/vebus-backup/post_aes_20261021.rvms   # into wagodb.dim_config
```

- Start the long steps detached (`setsid nohup`): a dropped or cancelled ssh session otherwise leaves a
  half-run step behind, and a second `qc` then starts a second Quick Configure over the first one.
- `l1`/`l2`/`l3` end with a Return that cancels the assistant read; never send from that state (assistants
  could be overwritten), run `get` first.
- VEConfig's command line shows the opened unit: `/N01` selects entry 01 of the `/P` list of target ids
  (`DE331F7A` = HQ2606Y2Y4U); the window title shows the serial as well.
- After any mk2vsc or VEConfig access mk2-dbus lists the units one by one again, Venus needs up to about
  a minute until all three `Devices/<n>/SerialNumber` are back.

The coordinates in `ess.sh` are absolute on the 1280x800 Xvfb screen with the 1260x780 Wine virtual
desktop; they hold as long as the window sizes do not change.

## Pytes values in the ESS assistant (2026-10-24)

A "Low battery" alarm at 17 % SoC came from the ESS dynamic cut-off (52/50/49.2/48 V): with 600 Ah,
26 A is only 0.04C, the interpolated cut-off is about 51.7 V, and L1 sagged to 51.16 V. Changed to the
Pytes recommendation for the E-BOX48100 (Victron compatibility PDF "VE Configure setting with Pytes 51.2V
batteries"): sustain 51.00 V, dynamic cut-off 49.00 V at 0.005C/0.25C/0.7C/2C, restart offset 1.20 V
(unchanged), capacity 600 Ah. "PV inverters on AC out" set to Yes (15000 W inverter and panel power,
frequencies 50.20/52.70/53.00 Hz default) before the PV inverter is wired; in grid mode it has no effect.

```sh
cd ~/vec
sh vebus-backup.sh pre_pytes49_20261024
setsid nohup sh ess.sh release qc l1 get assist >/tmp/ess_run.log 2>&1 </dev/null &
sh wiz.sh start; sh wiz.sh next; sh wiz.sh next; sh wiz.sh next     # Welcome, Battery system, Capacity
sh wiz.sh set 133 228 51.00                                         # Sustain
sh wiz.sh next; sh wiz.sh set 123 266 49.00; sh wiz.sh tab 49.00; sh wiz.sh tab 49.00; sh wiz.sh tab 49.00
sh wiz.sh next; sh wiz.sh next                                      # Restart offset, PV inverters (Yes: radio 46,162)
# PV pages on the master only: restart PV voltage, frequencies, total solar power (fields 222,153 / 222,174)
sh wiz.sh next; sh wiz.sh next                                      # VEConfig settings summary, finish
sh ess.sh sendthis
sh ess.sh close l2 get assist                                       # L2/L3: Sustain + PV only, then sendthis
```

- Look at every PNG before the next click. The wizard window does not always open at the same place:
  `wiz.sh next` clicks ">>" relative to the window (364, height-34), not at absolute coordinates.
- On L2/L3 the wizard has no cut-off, restart or PV detail pages; PV "Yes" goes straight to the summary.
- `sendall` never carries assistants (VEConfig says so); every unit needs its own `sendthis`.
- Each `sendthis` resets the VE.Bus; right after it a slave can show 53 Hz output for a few seconds.
- `rvms2db.py` decodes the ESS constants from the assistant program in the backup (setting ids 999-1014,
  see readers/rvms2db.py) so the values show up in `dim_config`.
