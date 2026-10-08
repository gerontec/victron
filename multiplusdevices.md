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

Everything below is the history of 2026-10-07 (first three-phase setup, firmware 556); its mapping table
was superseded by the reconfiguration that evening.

Three MultiPlus-II 48/5000/70-50 (product id 9763) form one three-phase VE.Bus
system. Venus OS reaches it through the MK3-USB (serial HQ1722KX766, host
`/dev/ttyUSB2`) as dbus service `com.victronenergy.vebus.ttyUSB2`, device
instance 290. All three run firmware 556 (`FirmwareVersion` 1366 = 0x556).

## Mapping

| Label on unit | VE.Bus short address | Unique number | Venus dbus | Serial number | Phase |
|---|---|---|---|---|---|
| **1** | 2 | 0x7A1F33DE | `Devices/2` | HQ2606Y2Y4U | **L3** |
| (unlabelled) | 1 | 0x7A1F3388 | `Devices/1` | HQ2606P4NCH | L2 (presumed) |
| (unlabelled) | 0 | 0x7A3D8892 | `Devices/0` | HQ2605CWUWH | L1 (presumed, master) |

How unit 1 was identified (2026-10-07, after the VE.Bus system configuration):
only unit 1 had grid on its AC-in. Venus then reported AC-in voltage only on
L3 (`Ac/ActiveIn/L3/V` = 240.4 V, L1/L2 = 0 V), and only `Devices/2` had
`ExtendStatus/AcIn1Available` = 1. So unit 1 is configured as the **L3** unit.

Assumptions still to verify:

- `Devices/N` equals VE.Bus short address N (same order in mk2-dbus and
  `mk2vsc -l`). Confirm by flashing the front-panel LEDs of one unit (below)
  and reading the serial on its type plate.
- L1/L2 for short addresses 0/1 follow the usual layout (master on L1). Label
  them the same way as unit 1: power only one unit's AC-in and see which
  `Ac/ActiveIn/Lx/V` comes up.

## DC side: two battery stacks

| Label on unit | Position | Battery | BMS in Venus | Charge voltage limit (CVL) |
|---|---|---|---|---|
| 2 | middle | stack A | CAN BMS, `battery.socketcan_can0`, 300 Ah | 57.6 V (3.60 V/cell) |
| 1 and 3 | outer | stack B | EBox via ebox_mqtt, `battery.ebox` | 56.6 V (3.54 V/cell) |

Until 2026-10-07 the two stacks shared one DC bus (after the charge stop at
13:31 the full EBox discharged into the CAN stack: EBox -7 A, CAN +3 A).
That DC link has since been removed; stacks A and B are now separate.

Unit 2's VE.Bus short address / `Devices/N` is not mapped yet (unit 1 is
`Devices/2`, see above); identify it with the LED flash.

Idea: unit 2 charges stack A to its somewhat higher voltage while units 1
and 3 keep stack B at the lower EBox limit.

Limits of that idea (Victron behaviour, not yet tested here):

- The charger settings (absorption/float voltage) of a VE.Bus system are one
  system setting; VE.Bus System Configurator writes the same charger values
  to all units, and Victron requires identical settings across a parallel or
  three-phase system.
- With DVCC on, Venus sends one CVL/CCL to the whole VE.Bus system, taken
  from the single active battery service (`ActiveBatteryService`). There is
  no per-unit charge voltage. Currently active: `com.victronenergy.battery/513`
  = EBox (stack B); the CAN BMS is instance 512. DVCC is off (2026-10-07).
- Victron's manuals for parallel/three-phase systems assume one common battery
  bank. With split stacks, load on one phase is carried only by that phase's
  stack; there is no energy exchange between phases on the DC side.

So a separate, higher charge voltage for unit 2 alone is not possible inside
one VE.Bus system. Options: run stack A at the EBox limit (56.6 V; on 2026-10-07 it
reached 99 % at about 56 V before the charge stop), or charge stack A to 57.6 V from a separate
charger outside the VE.Bus system.

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
# flash the LEDs of one unit (off again with -F off)
$V /opt/victronenergy/mk2vsc/mk2vsc -F on -W 0x7A1F33DE \
  -s dbus://com.victronenergy.vebus.ttyUSB2/Interfaces/Mk2/Tunnel
```

The mk2vsc address needs the `/Interfaces/Mk2/Tunnel` suffix; the bare
service name fails with `invalid dbus path`.

## Configuration backup

Read with `mk2vsc -r` (read only, Remote VEConfigure) right after the
three-phase configuration on 2026-10-07:

```sh
$V mkdir -p /data/vebus-backup
$V /opt/victronenergy/mk2vsc/mk2vsc --cache /tmp/ -r \
  -f /data/vebus-backup/vebus_3ph_20261007 \
  -s dbus://com.victronenergy.vebus.ttyUSB2/Interfaces/Mk2/Tunnel
```

| File | Where | Content |
|---|---|---|
| `vebus_3ph_20261007.rvms` (5519 B, md5 7e71db0185fcaacbfc9e90a4f6e895d3) | Venus `/data/vebus-backup/`, Pi `~/vebus-backup/`, gh-hpi7 `~/python/victron-backup/` | VE.Bus system configuration of all three units; open with VE.Bus System Configurator |
| `venus_settings_20261007.xml` | Pi `~/vebus-backup/`, gh-hpi7 `~/python/victron-backup/` | Venus localsettings (`/data/conf/settings.xml`) |

The first `-r` call right after `-l` can fail with
`veDbusAddRemoteService failed`; a retry a few seconds later works.

Firmware updates (e.g. to 563) only together for all three units: a VE.Bus
system needs identical firmware on every unit.
