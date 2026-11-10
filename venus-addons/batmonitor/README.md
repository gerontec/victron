# batmonitor

Charge / discharge control of the three MultiPlus-II phases on one battery bank (ESS external control, Hub4Mode 3).
Every 5 s one cycle reads the inputs (Sofar PCC and Bat1 over MQTT, BMS values and AC-in / AC-out from D-Bus),
computes one AC setpoint per phase and writes it to the Multis. Only the Python version runs (`BATMONITOR_PYTHON=1`).

**0.77-py (2026-11-07): the two 300 Ah stacks are physically parallel = one 600 Ah bank behind L1 + L2 + L3.**

Field observations from the running system (what tripped, measured values): [OBSERVATIONS.md](OBSERVATIONS.md).

| | |
|---|---|
| Bank | `BATTERY`, 600 Ah, phases L1 + L2 + L3 |
| Master BMS | Pytes `com.victronenergy.battery.ebox`: SoC, voltage, CCL / DCL (`/Info/Pack/*`, the Pytes alone) |
| Fallback BMS | MUST `com.victronenergy.battery.socketcan_can0`: takes over SoC, voltage, CCL / DCL while the Pytes BMS is missing (redundant BMS) |
| Both BMS | bank power = sum of both; lowest / highest cell over both (LOW force charge sees the weakest cell of either stack) |
| Charge end | only up to the Pytes limit (CVL 56.6 V from dbus-ebox-battery, Pytes SoC 100 % = FULL); MUST top hold and balancing lead removed |
| Split | total up to `SINGLE_MAX_W` 600 W: one MultiPlus (tag `1PH`, others `STBY`), the next phase every `ROTATE_S` 600 s (L1 → L2 → L3), charge and discharge alike; above 600 W all three alike (`EQ`), back to one below 500 W (`SINGLE_HYST_W` 100) |
| Watch | Pytes vs MUST voltage differ only by the cable drop; > 0.5 V logs an ERROR (`state.json` `bms_volt_diff_v`) |
| Decisions | 0.78-py: `bm_decisions.dmn` = the decision tables as standard DMN 1.3 (hit policy FIRST), the meaning of every column as its `inputExpression` (e.g. `soc < SOC_MIN`, common subset of FEEL and Python); opens in the Camunda Modeler / dmn-js. bm_logic.py reads it with the standard library (no engine on Venus). `bm_tables.h` keeps only the PARAM lines (written by `host/bm_params_pull.py`) |
| Test | `python3 tests/test_split.py` (closed loop on a simple plant), `python3 tests/test_parse.py`, `python3 tests/check_reach.py` (reachability matrix of the decision states, thresholds from the DMN, numpy + networkx, ~12 s), `python3 tests/check_dmn.py` (the DMN through the SpiffWorkflow engine against bm_logic: all table combinations + sampled column meaning), `python3 tests/diff_logic.py <old dir>` (old vs new bm_logic on random cycles). Notebook / dell: `pip install --user --break-system-packages SpiffWorkflow numpy networkx` |

The C files in `c/` and the sections below describe the two-bank versions up to 0.76 (history).

| File | Role |
|---|---|
| `c/bm_logic.c`, `c/bm_logic.h` | the control logic, a pure function `bm_step(cfg, in, state) -> out` (no D-Bus, MQTT, clock) |
| `c/batmonitor.c` | the daemon on Venus: collects inputs, writes setpoints, `state.json`, MQTT; rule history in its header comment |
| `bm_logic.py`, `batmonitor.py`, `bm_parse.py` | Python port, byte-identical to the C logic; fallback with `BATMONITOR_PYTHON=1` |
| `c/tests/check_matrix.c` | exhaustive check of the decision matrix (`make check`) |
| `c/tests/test_logic.c` | scenario tests with a plant model (`make test`) |
| `c/tests/cbmc_harness.c` | CBMC proofs of predicates and amounts (`make cbmc`, `make cbmc-full`) |
| `c/tests/fuzz_dump.c`, `tests/fuzz_replay.py` | differential tests old vs. new revision and C vs. Python |

## How the decision matrix works

The logic is split strictly into two layers:

1. **Decision layer** – small pure functions `bm_d1` … `bm_d7` that only see *bits* (threshold comparisons of the
   inputs, e.g. `soc_lt_min`, `surplus_gt_on`, `night`) plus a few hysteresis state bits. They return a *name*: a
   source row, a mode, a lead bank, PROPORTIONAL / IDLE.
2. **Amount layer** – the formulas in watts (soyo P controller, ceilings, split over the phases). It never decides
   *whether* a phase charges or discharges, only *how much*.

The names chosen by the decision layer are looked up in two tables, which turn them into behaviour.

### One cycle

```
inputs ─► D7 house on AC-out1 ─► D1 source (S3) + winter heat pump cap ─► S0 ceilings (charger, CCL/DCL, top hold)
       ─► D4 summer peak window ─► D2 mode per stack ─► BM_MODE_RULE row per phase
       ─► D-lead SoC balancing
       ─► charge path   (charge amount, split: priority order, lead last)
       ─► discharge path (BM_SRC_RULE row, D3 PROP/IDLE, discharge amount, split: lead first)
       ─► CCL / DCL / CELL tags ─► force_full end ─► D5 DO4 pulse ─► PI shadow
```

`bm_step()` at the end of `bm_logic.c` / `bm_logic.py` runs exactly these `chain_*` stages in this order.

### The decisions

| Function | Inputs (bits) | Output | Hysteresis state |
|---|---|---|---|
| `bm_d1` | night, wp_on, b1_above_min, b1_ge_release, winter, b1_dis | source `DAY` / `B1` / `STK` / `WPG`, wp_cap none / 0 W / `WP_WINTER_W` | `bat1_first` |
| `bm_d2` | per stack: bms_ok, SoC thresholds, charging, low_on/off, ff, top; global: stale, block, surplus | mode per stack | prot, force, low per stack, chg_prev |
| `bm_dlead` | both BMS ok, SoC difference > ON / < OFF | lead bank or none | lead |
| `bm_d3` | import, deficit > hold, prop_hold of the source row | PROPORTIONAL / IDLE | prop_prev |
| `bm_d4` | season, export forecast above `EXPORT_CAP`, surplus above the cap | block (wait) / cap active | peak_today |
| `bm_d5` | PCC > 20 kW for the grace time or > `DO4_HARD_W`, AC-out overfeed off-grid, lockout | DO4 pulse | – |
| `bm_d7` | `HOUSE_ACOUT` mode, AC-out total > 99 W / < 30 W, 60 s timer | house on AC-out1 (Z1 / Z2 rule) | on, under timer |

`bm_d1_id` packs the D1 cell into one byte (bit 0 night … bit 6 bat1_first); it is logged in `state.json`
(`d1_id`) and `pv_victron.bm_d1_id`, so every logged cycle can be mapped back to its table cell.

### Mode per stack (D2) – first match wins

| Priority | Mode | Condition |
|---|---|---|
| 1 | `BMS_MISSING` | BMS not connected or no valid SoC |
| 2 | `FORCE` | SoC below `SOC_FORCE` (until `SOC_FORCE_RELEASE`), or LOW: DCL < `DCL_FORCE_A` or lowest cell < `CELL_FORCE_V` (tag `LOW`) |
| 3 | `STALE` | Sofar data older than 180 s |
| 4 | `LADESPERRE` | charge mode, but the summer peak window (D4) says wait |
| 5 | `CHARGE` / `FULL` | charge mode (surplus > 200 W, held down to 20 W); `FULL` at 100 % SoC unless force_full or top hold |
| 6 | `HOLD` | force_full_from_pv names this stack and there is no surplus |
| 7 | `PROT` | SoC below `SOC_MIN` (until `SOC_MIN_RELEASE`) |
| 8 | `CHARGING` | the BMS already reports charging > 200 W |
| 9 | `DIS` | everything else: the stack may discharge |

### Mode matrix `BM_MODE_RULE` – what a mode does with the setpoint

| Mode | action | capped | rule text |
|---|---|---|---|
| BMS_MISSING | 0 W | 0 | `NAME` |
| FORCE | `FORCE_CHARGE_W` | 1 | `NAME(soc %)` |
| STALE | 0 W | 0 | `NAME` |
| LADESPERRE | 0 W | 0 | `NAME(peak h)` |
| CHARGE | charge amount | 1 | `NAME SRC:<src>` |
| FULL | 0 W | 0 | `NAME` |
| PROT | 0 W | 0 | `NAME(soc %)` |
| CHARGING | 0 W | 0 | `NAME(BMS W)` |
| DIS | discharge amount | 1 | written by the discharge path |
| HOLD | 0 W | 0 | `NAME(soc %)` |

`capped` = the setpoint is limited by the phase ceiling (`chg_cap` / `dis_cap`: charger, `DISCHARGE_MAX_PHASE`,
BMS CCL / DCL, top hold).

### Discharge matrix `BM_SRC_RULE` – one row per S3 source

| Source | When | b1_signed | trickle | idle_w | prop_hold |
|---|---|---|---|---|---|
| `DAY` | PV ≥ 100 W | 0 | 0 | 20 W | 0 |
| `B1` | night, Sofar first (Bat1 above `BAT1_SOC_MIN`, heat pump running) | 0 | 0 | 20 W | 0 |
| `STK` | night floor: the stacks take the Sofar over | 1 | 1 | 0 | 1 |
| `WPG` | winter night, heat pump running, no Sofar first | 0 | 0 | 20 W | 0 |

- `b1_signed`: a Bat1 discharge counts as house load the stacks take over (else only a Bat1 charge counts).
- `trickle`: the target is `SOYO_TARGET + SOFAR_TRICKLE`, so the Sofar battery charges a few W steadily.
- `idle_w`: discharge while D3 says IDLE.
- `prop_hold`: IDLE → PROPORTIONAL already above the 20 W deficit, not only on import.

With D7 on (house on AC-out1) the source text becomes `Z2` (winter: discharge = AC-out1 load) or `Z1` (summer: PCC
near 0, at least the AC-out1 load), and source row, night floor and heat pump cap do not apply.

### Rule text

Each phase gets one text, built stage by stage, in `state.json` and the log:

```
<mode or PROP/IDLE/HOUSE> [SRC:<source>] [EQ|BAL|SPILL|WAIT|FF] [CAP] [TOP] [WPMAX(W)] [LOW] [CCL|DCL|CELL]
```

Examples: `CHARGE SRC:DAY EQ CAP` (charging, both stacks alike, summer export cap active),
`PROP SRC:STK BAL` (night floor, the stack ahead in SoC gives first), `FORCE(2.8%) LOW`, `PROT(4.6%)`.
`EQ` = both stacks the same share, `BAL` = lead bank first, `SPILL` / `WAIT` = the other bank takes the rest / nothing,
`CCL` / `DCL` / `CELL` = the BMS limit or the top hold cap cut this setpoint.

## Editing

The rule: **decision and amount stay apart, C and Python change together, and every change is checked.**

### 1. Change a parameter (no code change)

All tunables are in `BM_PARAMS` (`c/bm_logic.c`, mirrored in `bm_logic.py`) with default, allowed range, unit and
meaning. Override on Venus in `/data/batmonitor/env`:

```sh
BATMONITOR_LIVE=1              # without: dry run, nothing is written to the Multis
BATMONITOR_W_MAX=8000          # BATMONITOR_<NAME>=value; out of range or not a number -> default stays
BATMONITOR_TOP_HOLD_STACK=1
```

then restart with `svc -t /service/batmonitor`. The log line `params (* = from env): ...` at start shows what is
in effect. Further switches: `BATMONITOR_LADESPERRE=0`, `BATMONITOR_PI=1`, `BATMONITOR_PYTHON=1`
(see `service/run`).

A new parameter: append a row to `BM_PARAMS` in both files and a field to `struct bm_cfg` / `Cfg`. If it is a
threshold another one depends on (e.g. `SOC_FORCE <= SOC_MIN`), add the order to `check_threshold_order()` in
`c/tests/check_matrix.c`.

### 2. Change what an existing row does

Edit the row in `BM_MODE_RULE` or `BM_SRC_RULE` in **both** `c/bm_logic.c` and `bm_logic.py`. Example – the night
floor should idle with 20 W instead of 0 W:

```c
[BM_SRC_STK] = {1,        1,      B_DAY_IDLE, 1},
```

`make check` will now fail on `idle power 0 exactly with the night floor`: the invariants in `check_dis_table()` /
`check_mode_table()` describe the intended behaviour, so change the invariant together with the row (on purpose, in
the same commit), not to make the test pass.

### 3. Change a decision (when a mode / source is chosen)

1. The decision functions take only bits. If a new condition is needed, compute it as a new bit in the stage that
   builds the bits (`chain_d1`, `d2_bank_bits` / the D2 bits in `chain_mode`, `ladesperre`, …), add the field to the
   `bm_dX_in` struct, and use it in `bm_dX`. No watts or timestamps inside `bm_dX`.
2. Mode order in `bm_d2` is the priority list above: insert a new condition at the place where it should win.
3. A new mode or source: append it at the end of the enum (`BM_M_*` / `BM_SRC_*` in `bm_logic.h`; existing indices
   are logged and must stay), add its name to `BM_MODE_NAME` / `BM_SRC_NAME`, a row to the table, the same in
   `bm_logic.py`.
4. Extend `check_matrix.c`: enumerate the new bit (the loops run over all bit combinations), add constraints that
   exclude physically impossible combinations (`bank_ok()`), and invariants for the new rule. The output shows how
   often each mode / source is reached – a new mode that is never reached is a bug.
5. One behaviour change per commit; bump `VERSION` in `c/batmonitor.c` and `batmonitor.py` and add a line to the rule
   history in the header comment of `c/batmonitor.c` (date, reason, what changed).

### Checks before a commit

```sh
cd c
make check          # exhaustive decision matrix + 200000 random bm_step cycles (amount properties)
make test           # scenario tests with the plant model, payload parsing
make fuzz-compare   # vs. HEAD: pure refactor -> "identical"; behaviour change -> only the intended cycles differ
make py-compare     # C and Python byte-identical (5 x 48000 cycles + forecasts, ~1 min)
make cbmc           # predicate proofs (seconds); make cbmc-full / cbmc-remote for the amount proofs (minutes, ~5 GB)
```

`make fuzz-compare REV=<commit>` compares against any older revision; the differing cycles are in
`/tmp/bm_fz/old.<seed>` / `new.<seed>`.

## Deploy

On Venus: `sh install.sh` copies everything to `/data/batmonitor`, builds the C binary with the gcc of the Venus image
and links `/service/batmonitor` from `/data/rc.local`. Without a binary `service/run` starts the Python port.
State and rule texts: `/data/batmonitor/state.json`; PI shadow: `/data/batmonitor/pi_shadow.csv`.

## Proposal: opportunity loads (not built)

[`docs/opl_flow.png`](../../docs/opl_flow.png) (source `docs/opl_flow.dot`) shows how one cycle would look with a
load layer: table `BM_LOADS`, decision D8 per load with a reason (status page colours), the forecast-based SoC
threshold, minimum run and bridge timers, actuators with read-back and the OPL status page.
