# batmonitor – field observations

Dated observations from the running system in Lenggries (3× MultiPlus-II 48/5000, one 600 Ah bank: MUST + Pytes
E-Box 48100R parallel, Venus OS v3.81 in nspawn on the Pi .123). Measured values only; data from `wagodb.pv_victron`
(one row per minute, `readers/victron2db.py`) unless stated otherwise. Newest first.

## 2026-11-10 – first discharge to the bottom with the Pytes cell limits: SOC_MIN trips first

### Setup that morning

| Layer | Limit | Pack equivalent (16S) | Source |
|---|---|---|---|
| batmonitor PROT (cell) | weakest cell < 2.94 V while SoC < 30 %, release 3.00 V | ≈ 47 V / 48 V | param version 6, live 07:33 |
| batmonitor LOW (cell) | weakest cell < 2.94 V, release 3.00 V | ≈ 47 V / 48 V | param version 6 |
| batmonitor PROT (SoC) | SOC_MIN 8 %, release 10 % | – | `bm_tables.h` |
| Venus ESS | MinimumSocLimit 8 % | – | `/Settings/CGwacs/BatteryLife/MinimumSocLimit` |
| ESS assistant | dynamic cut-off 49.0 V at all four C-rates, sustain 51.0 V | 49.0 V | VE.Bus backup `nightly_20261110.rvms` |
| MUST BMS (DVCC) | BatteryLowVoltage 48.0 V | 48.0 V | `socketcan_can0 /Info/BatteryLowVoltage` |
| VE.Bus DC low shutdown | 37.2 V, restart +6.4 V | 37.2 V | rvms, decoder confidence HIGH |

Param version 6 lowered the cell limits from 3.10 / 3.20 V (PROT) and 3.05 / 3.20 V (LOW) to the Pytes values
(low battery cutout 47 V, cut-in 48 V); the Pytes BMS gives the low-battery warning. Expectation at the time
(commit 9f2b3c0): the ESS cut-off at 49.0 V would now trip before batmonitor.

### What happened

| Time | Pytes SoC | MUST SoC | DC V | weakest cell Pytes / MUST (id) | Pytes W | MUST W | setpoint sum W | rule L1 | VE.Bus low-bat alarm | LED low bat | ess_cutoff_suspect |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 07:45 | 12.7 | 21 | 50.46 | 3.137 / 3.076 (10) | −410 | −272 | −570 | HOUSE SRC:Z2 STBY | 0 | 0 | 0 |
| 07:48 | 12.3 | 21 | 50.44 | 3.135 / 3.073 (10) | −401 | −231 | −550 | HOUSE SRC:Z2 STBY | 0 | 0 | 0 |
| 07:49 | 11.0 | 21 | 50.08 | 3.113 / 3.052 (10) | −1478 | −1167 | −2451 | HOUSE SRC:Z2 EQ | **1** | 3 | 0 |
| 07:50 | 8.0 | 20 | 49.82 | 3.092 / 3.033 (10) | −1674 | −1291 | −2829 | HOUSE SRC:Z2 EQ | **1** | 3 | 0 |
| 07:51 | **7.3** | 20 | 50.26 | 3.113 / 3.051 (10) | −259 | +70 | 0 | **PROT(7.3%)** | 1 | 0 | 0 |
| 07:52 | 7.3 | 20 | 50.40 | 3.122 / 3.061 (10) | −199 | +90 | 0 | PROT(7.3%) | 0 | 0 | 0 |
| 07:53 | 7.3 | 20 | 50.46 | 3.124 / 3.065 (10) | −185 | +60 | 0 | PROT(7.3%) | 0 | 0 | 0 |

LED: 0 off, 3 blinking inverted. VE.Bus low-bat alarm (`/Alarms/LowBattery`): 0 ok, 1 warning. No Venus notification
was raised (`pv_victron_notifications` has no row on 2026-11-10). VE.Bus state stayed 3, Pytes DCL stayed 586 A.

### Findings

1. **batmonitor's SoC rule tripped first, not the ESS cut-off.** PROT came from SOC_MIN (Pytes SoC 7.3 % < 8 %) at
   07:51. The DC voltage never went below 49.82 V, so the ESS cut-off (49.0 V) did not act (`ess_cutoff_suspect` 0
   in every minute). The cell rule was far away: the weakest cell was MUST cell 10 at 3.033 V, 93 mV above 2.94 V.
   The expectation in commit 9f2b3c0 ("Victron trips before batmonitor") did not hold in this discharge; it can
   still hold under a load high enough to pull the bank below 49 V before the SoC reaches 8 %.
2. **The Pytes SoC drops in steps at the bottom.** 12.3 → 11.0 → 8.0 → 7.3 % in three minutes. At ≈ 2.8 kW for
   two minutes the bank delivered about 0.09 kWh, ≈ 0.3 % of 30.7 kWh; the SoC fell 5 points. The BMS corrects its
   SoC against the voltage when the load pulls the voltage down. The MUST SoC stayed at 20–21 % (it reads 10–14
   points too high at the bottom, see 0.79-py). Consequence: a linear empty forecast is only rough below ≈ 15 %
   SoC; `web/victron_api.php` s1_eta said "empty ≈09:12 (SoC 8 %)" at 07:47 and the limit came at 07:51.
3. **The VE.Bus low-battery warning comes far above the ESS cut-off.** `/Alarms/LowBattery` = 1 and the LED blinking
   from 07:49 at 50.08 V DC (lowest 49.82 V), back to 0 at 07:52. Which VE.Bus threshold raises it is not known
   (pre-alarm offset in the rvms is 0); it is only a warning, the MultiPlus kept running. The earlier assumption that
   this alarm belongs to the 37.2 V DC low shutdown was wrong.
4. **Cross-flow between the stacks at rest.** With all setpoints 0 the Pytes still gave −185…−259 W while the MUST
   took +60…+90 W. Presumed (not measured): after the load step the Pytes recharges the MUST through the common
   bus, the rest is the idle draw of the three MultiPlus. The bank total at rest was ≈ −125…−190 W.

### Follow-ups

- Watch the next deep discharges for `ess_cutoff_suspect` = 1 (high load at low SoC) to see whether the 49.0 V
  cut-off ever acts first; until then SOC_MIN is the effective limit.
- Find the VE.Bus threshold of the low-battery warning (≈ 50 V under load) before trusting it as a signal.
- The empty forecast could use the voltage at the bottom (or stop at ≈ 15 % SoC) instead of a linear SoC model.

Changes of the day: 9f2b3c0 (cell limits), 412b184 (`vebus_alarm_low_battery`, `ess_cutoff_suspect`), e6c1e70
(bank empty forecast).
