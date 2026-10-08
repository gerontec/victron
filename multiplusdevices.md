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

## Charge and discharge strategy (batmonitor 0.10, venus-addons/batmonitor)

ESS runs in external control (Hub4Mode 3): batmonitor sets `/Hub4/L1..L3/AcPowerSetpoint` per phase
(+ = charging from AC-in, - = feeding out of AC-in). It calculates every 60 s and re-sends every 10 s.
Inputs: the Sofar values every 4 s (sofar_fast.py on 192.168.178.218: PCC, Bat1, SOC1, PV), the SoC of both
BMS in Venus (Stack1 CAN, Stack2 ebox reader) and the measured AC-in power of the three units.

Charging (PV surplus)

- surplus = Sofar PCC + Sofar Bat1 (discharge counts fully, charge with factor 0.5) + own measured AC-in
  charging. The Sofar holds its PCC at 0 with its own battery, so a Bat1 discharge is no surplus.
- charge = 1.01 x (surplus - 200 W), at most 4000 W AC per phase (the 70 A charger of each MultiPlus;
  measured maximum 11.1 kW DC at 205 A for all three).
- order: Stack1 (L2) first up to its maximum, the rest to Stack2 (L1 + L3, half each). Stack1 has one
  70 A charger (300 Ah in ~4.3 h), Stack2 two (140 A, ~2.1 h, 0.47 C); Stack2 is charged gentler.
- a full stack (100 %) drops out, its share goes to the other one.

Discharging (feeding the house)

- AC-out1 loads (UPS sub-distribution): each phase carries its own load from its own stack
  (L2 from Stack1, L1 + L3 from Stack2); batmonitor cannot move these.
- feed-in over AC-in: the soyo law of the former Waveshare ESP32, power values x2 for the ~1000 W base load:
  import > 100 W: w = min(1.01 x import + 936 W at night, 1800 W); dead band: 936 W at night, 20 W by day;
  Oct-Apr with the R290 heat pump running at most 1000 W; no feed-in at PV surplus > 200 W or while a stack
  is being charged; Sofar data older than 3 min -> 0. Shared equally by the phases whose stack may discharge.

SoC balancing (keep the stacks within +-3 %)

- one stack more than 3 % ahead: it alone feeds the house, and the other one is charged first;
  back to normal when the difference is below 1 %.

Protection, per stack

- SoC < 5 %: no discharge until 7 %.
- SoC < 3 %: forced charge from the grid, 500 W per phase, until 5 % (any time of day).
- BMS service missing: its phases get 0 W. The Pytes limits (CVL 56.6 V, cell taper 3.60 V) still come from
  dbus-ebox-battery via DVCC; the voltage cut-off of the units (37.2 V) is only the last resort.

Logged every minute in wagodb.pv_victron, columns bm_* (setpoints, rule per phase, balance lead, protection
and forced charge flags, surplus), from /data/batmonitor/state.json via readers/victron2db.py.

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
