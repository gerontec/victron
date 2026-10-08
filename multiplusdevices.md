# MultiPlus-II devices on the VE.Bus

## Current state (2026-10-08, verified)

| Venus dbus | Phase | Position | Serial number | Unique number | Battery (DC) |
|---|---|---|---|---|---|
| `Devices/0` | L1 (VE.Bus master) | outer | HQ2606P4NCH | 0x7A1F3388 | Stack2 = Pytes EBox, 300 Ah |
| `Devices/1` | L2 | middle, on the wall | HQ2606Y2Y4U | 0x7A1F33DE | Stack1 = MUST, 300 Ah, CAN BMS |
| `Devices/2` | L3 | outer | HQ2605CWUWH | 0x7A3D8892 | Stack2 = Pytes EBox, 300 Ah |

- MK3-USB (serial HQ1722KX766) is plugged into the L1 unit; L2 and L3 hang on the VE.Bus chain L1 - L2 - L3.
- Firmware 568 on all three units, ESS assistant on all three, grid code VDE-AR-N 4105, DVCC on
  (active BMS: `battery.ebox`, instance 513).
- AC-in of the three units comes from a motorised 3-phase ATS; AC-out1 (UPS output) feeds the house
  sub-distribution busbar, one phase per unit.
- VE.Bus battery monitor off on all units (2026-10-08); SoC comes only from the BMS (Stack1 CAN, Stack2 ebox reader).
- AC input current limit 35 A (L1 read back from the device; L2/L3 not read back yet).
- Per-unit settings: private repo gerontec/victron-backup, `dev0.conf` .. `dev2.conf`.

## Reading it from Venus

```sh
L=$(sudo machinectl show venus -p Leader --value)
V="sudo nsenter -t $L -a"
# devices, serials, AC-in per phase
$V dbus -y com.victronenergy.vebus.ttyUSB2 / GetValue | tr ',' '\n' \
  | grep -E 'NumberOfMultis|SerialNumber|AcIn1Available|Ac/ActiveIn/L./V'
# short addresses and unique numbers
$V /opt/victronenergy/mk2vsc/mk2vsc -l \
  -s dbus://com.victronenergy.vebus.ttyUSB2/Interfaces/Mk2/Tunnel
# flash the LEDs of the L2 unit (off again with -F off)
$V /opt/victronenergy/mk2vsc/mk2vsc -F on -W 0x7A1F33DE \
  -s dbus://com.victronenergy.vebus.ttyUSB2/Interfaces/Mk2/Tunnel
```

The mk2vsc address needs the `/Interfaces/Mk2/Tunnel` suffix; the bare service name fails with
`invalid dbus path`. Full configuration backup and restore: tools/vebus-backup.sh, private repo
gerontec/victron-backup (`config/restore.sh`).
