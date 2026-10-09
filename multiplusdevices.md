# MultiPlus-II devices on the VE.Bus

## Current state (2026-10-08, verified)

Wall, left to right (serial of the middle unit checked on its type plate 2026-10-08):

| Unit on the wall | Venus dbus | Phase | Serial number | Unique number | Battery (DC) |
|---|---|---|---|---|---|
| 1 (left) | `Devices/1` | L2 | HQ2606Y2Y4U | 0x7A1F33DE | Stack2 = Pytes EBox, 300 Ah |
| 2 (middle) | `Devices/0` | L1 (VE.Bus master) | HQ2606P4NCH | 0x7A1F3388 | Stack1 = MUST, 300 Ah, CAN BMS |
| 3 (right) | `Devices/2` | L3 | HQ2605CWUWH | 0x7A3D8892 | Stack2 = Pytes EBox, 300 Ah |

- MK3-USB (serial HQ1722KX766) is plugged into unit 1 (left, L2); the VE.Bus chain runs unit 1 - unit 2 - unit 3.
- Firmware 568 on all three units, ESS assistant on all three, grid code VDE-AR-N 4105, DVCC on
  (active BMS: `battery.ebox`, instance 513).
- AC-in of the three units comes from a motorised 3-phase ATS; AC-out1 (UPS output) feeds the house
  sub-distribution busbar, one phase per unit.
- VE.Bus battery monitor off on all units (2026-10-08); SoC comes only from the BMS (Stack1 CAN, Stack2 ebox reader).
- ESS Hub4Mode 3 (external control by batmonitor; it sets 1 again when it stops), BatteryLife state 3.
- AC input current limit 35 A (L1 read back from the device; L2/L3 not read back yet).
- Per-unit settings: private repo gerontec/victron-backup, `dev0.conf` .. `dev2.conf`.

## Charge and discharge strategy (batmonitor 0.23-c, venus-addons/batmonitor)

ESS runs in external control (Hub4Mode 3): batmonitor sets `/Hub4/L1..L3/AcPowerSetpoint` per phase
(+ = charging from AC-in, - = feeding out of AC-in) and `MaxFeedInPower`, every 5 s. It is a C program
(`c/batmonitor.c`, about 3 MB RSS); the arithmetic is the pure function `bm_step` in `c/bm_logic.c`, tested with a
plant model (`make -C venus-addons/batmonitor/c test`, 59 checks). `batmonitor.py` is the fallback
(`BATMONITOR_PYTHON=1`, 60 s, fixed parameters). Live on the Pi 4 (.119); the NCR (venusncr) runs the same binary
as fallback and stays idle without MK3/BMS/adapters.

Inputs (MQTT on 192.168.178.218 and Venus D-Bus)

- Sofar every 4 s (sofar_fast.py): PCC, Bat1, SOC1, PV, plus 5 min averages.
- Heat pump power (SDM72D `em0/power`, once a minute), season (`batmonitor/season`, hourly), R290 compressor
  frequency, outdoor temperature.
- SoC and DC power of both BMS (Stack1 `battery.socketcan_can0`, Stack2 `battery.ebox`), measured AC-in of the units.

Meters and tariffs

- Z1 = grid connection, counts everything incl. the heat pump (cheap heat pump share); Z2 = house without the heat
  pump (expensive). The heat pump hangs between Z2 and Z1. The Sofar PCC CT sits at Z1, so the Sofar sees the
  heat pump as house load (measured: heat pump 4.1 kW at night -> Sofar load +3.3 kW, Bat1 -1.7 kW).
- Z2 = PCC + heat pump power. The Sofar keeps covering the heat pump from its Bat1 (up to 2.5 kW) because of its own
  PCC regulation; batmonitor cannot change that.

Season (readers/season.py on .218, from measured energy)

- ratio = 7-day sold energy (inverter_data, PCC > 0) / 7-day heat pump energy (sdm72d); a band is left only 0.1
  beyond its threshold:
  - summer, ratio > 2: the heat pump may and must run 100 % from the batteries; discharge on the Z1 PCC.
  - transition, 1 .. 2: the heat pump gets at most 1900 W from the batteries, the rest from the Z1 grid.
  - winter, ratio < 1: discharge on Z2, the heat pump takes only the cheap Z1 grid power, the batteries serve the
    expensive house power; no night floor while the heat pump runs.
- Missing or older than 2 days: months (May-Sep summer, Oct-Apr winter). 2026-10-08: transition (1.83).

Charging (PV surplus, on the Z1 PCC in every season)

- surplus = PCC + Sofar Bat1 (discharge counts fully, charge with factor 0.5) + own charging - own discharge.
- entry at surplus > 200 W, then charging holds down to 20 W; setpoint = own charging + 1.01 x (PCC + Bat1 term),
  i.e. the PCC is driven to 0 W (before 0.18 it settled at +200 W export).
- at most 4900 W AC per phase; the 70 A charger of each unit takes ~4.2 kW (measured maximum 11.1 kW DC at 205 A
  for all three); order: Stack1 (L1, middle unit) up to 4200 W, the rest to Stack2 (L2 + L3, half each); above all
  charger capacities the rest goes to all phases alike. Stack1 has one 70 A charger (300 Ah in ~4.3 h), Stack2 two
  (140 A, ~2.1 h, 0.47 C). A full stack (100 %) drops out.
- charging stays on Z1 also in winter: on Z2 the Sofar would refill the charging from its Bat1 while the heat
  pump runs.
- charge block May-Aug: on forecast 20 kW days (clear-sky model, NOAA sun position, fitted strings, tree horizon,
  outdoor temperature) PV charging waits for the peak window (PCC > 20 kW, peak hour or solar noon); measured
  clouds release it for the day. The rest of the year every watt of surplus charges at once.

Discharging (feeding the house)

- AC-out1 loads (UPS sub-distribution): each phase carries its own load from its own stack
  (L1 from Stack1, L2 + L3 from Stack2); batmonitor cannot move these.
- feed-in over AC-in on the season's point (winter Z2, transition PCC + heat pump above 1900 W, summer PCC):
  entry at 100 W import, then setpoint = own discharge - Sofar Bat1 charge - 1.01 x import, held down to 20 W
  (before 0.18 it fell back to idle once covered and toggled). The own force charging of a stack is no house load,
  it comes from the grid.
- idle: 20 W by day, 936 W at night (PV < 100 W; not in winter while the heat pump runs).
- at most 10 kW in total and 3800 W per phase (0.34-c; 4 kW is the MultiPlus-II 48/5000 limit, not its best
  operating point).
- equal power per stack (0.26-c): Stack1 feeds only L1, Stack2 feeds L2 + L3, so L1 gets 50 %, L2 and L3 25 % each
  (Unit 2 twice Unit 1 / Unit 3). Above 7.6 kW L1 stays at 3800 W and the rest goes to L2/L3; the faster drain of
  Stack2 is caught up later by the SoC balancing.
- no feed-in while a stack is being charged or at PV surplus; Sofar data older than 3 min -> 0;
  setpoints older than 90 s -> 0.
- heat pump meter silent (> 150 s) in winter/transition with the R290 running: at most 1000 W (old soyo cap).

SoC balancing (keep the stacks within +-3 %)

- one stack more than 3 % ahead: it alone feeds the house (up to 3800 W per phase, the rest spills to the other
  stack's phases) and the other one is charged first; back to normal when the difference is below 1 %.

Protection, per stack

- SoC < 5 %: no discharge until 7 %.
- SoC < 3 %: forced charge from the grid, 500 W per phase, until 5 % (any time of day).
- BMS service missing: its phases get 0 W. The Pytes limits (CVL 56.6 V, cell taper 3.60 V) still come from
  dbus-ebox-battery via DVCC; the voltage cut-off of the units (37.2 V) is only the last resort.

Full-charge forecast, per stack

- full at = now + (100 % - SoC) x 15.36 kWh (300 Ah x 51.2 V) / 5 min average DC charge power from its BMS
  (sampled every 5 s); none below 50 W or at 100 %. Valid for the current power only.

Parameters (`/data/batmonitor/env`, `BATMONITOR_<NAME>=value`, defaults in `BM_PARAMS`, logged at start)

| Parameter | Default | Meaning |
|---|---|---|
| `W_MAX` | 10000 W | total discharge |
| `DISCHARGE_MAX_PHASE` | 3800 W | discharge per phase (since 0.34-c; 4000 W is the MP2 limit, not its best operating point) |
| `CHARGE_MAX_PHASE` / `CHARGER_CAP_PHASE` | 4900 / 4200 W | PV charge per phase / real charger capacity |
| `WP_BAT_MAX_TRANSITION` | 1900 W | heat pump share from the batteries in the transition |
| `WP_CAP` | 1000 W | discharge cap when the heat pump meter is silent |
| `SOC_MIN` / `SOC_MIN_RELEASE` | 5 / 7 % | discharge protection |
| `SOC_FORCE` / `SOC_FORCE_RELEASE` / `FORCE_CHARGE_W` | 3 / 5 % / 500 W | grid force charge |
| `SOC_BALANCE_ON` / `SOC_BALANCE_OFF` | 3 / 1 % | balancing |
| `SOYO_TARGET` | 0 W | PCC target |
| `B_NIGHT` | 936 W | night base discharge |

Switches: `BATMONITOR_LIVE=1` (set on the Pi), `BATMONITOR_LADESPERRE=0`, `BATMONITOR_PI=1`.

PI prototype (not armed)

- velocity-form PI on PCC + Bat1 -> 0 W runs in shadow mode; every cycle one line in
  `/data/batmonitor/pi_shadow.csv` (what soyo sent, what the PI would send); `tools/compare.py` replays it in closed
  loop on the recorded data. `BATMONITOR_PI=1` would arm it.

Logging

- `/data/batmonitor/state.json` every cycle: setpoints, rule per phase, balance lead, protection/force flags,
  surplus, forecast, season and its source, charge block, PI; wagodb.pv_victron columns bm_* every minute via
  readers/victron2db.py.
- log (multilog 4 x 1 MB) on rule change or once a minute; pi_shadow.csv 2 x 8 MB; at most 20 MB together.

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
# flash the LEDs of unit 1 (left, L2) (off again with -F off)
$V /opt/victronenergy/mk2vsc/mk2vsc -F on -W 0x7A1F33DE \
  -s dbus://com.victronenergy.vebus.ttyUSB2/Interfaces/Mk2/Tunnel
```

The mk2vsc address needs the `/Interfaces/Mk2/Tunnel` suffix; the bare service name fails with
`invalid dbus path`. Full configuration backup and restore: tools/vebus-backup.sh, private repo
gerontec/victron-backup (`config/restore.sh`).
