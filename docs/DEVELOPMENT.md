# Development method for rule logic

How decision code in this repo (batmonitor first of all) is changed. The model is the batmonitor 0.45-c
discharge matrix (`BM_SRC_RULE` in `venus-addons/batmonitor/c/bm_logic.c`); see
[decision_flow.pdf](decision_flow.pdf) for the resulting cycle.

The goal is robustness through simplicity: every decision has a name, every rule has a table row, every table has an
automatic check.

## The seven rules

| # | Rule | batmonitor example |
|---|------|--------------------|
| 1 | **Name decisions in the output first**, then structure them. One reason per stage in the rule text / log. | `PROP WP:FC SRC:STK EQ`; the reason enums later became the table rows |
| 2 | **Keep decisions apart from amounts.** Decisions are pure functions of predicate bits + hysteresis state; amounts (W) are formulas outside them. | `bm_d1` / `bm_d2` / `bm_dlead` / `bm_d3` contain no watts |
| 3 | **Every consumer of a decision bit becomes a named column.** grep the bit, one column per use. | 4 uses of `night_floor` -> `b1_signed`, `trickle`, `idle_w`, `prop_hold` |
| 4 | **Refactor without behaviour change first, proven by a differential test.** The table values come from the current behaviour, not from a redesign. | `make fuzz-compare OLD=<old bm_logic.c>`: identical over 5 x 48000 cycles |
| 5 | **Behaviour changes afterwards, one at a time, each with its own test.** | 0.45-c step 5 (source independent of the forecast) + `test_forecast_keeps_source` |
| 6 | **Keep axes orthogonal.** Factorise into small one-dimensional tables instead of a product table. | `BM_SRC_RULE[4]` and `bm_wp_kind[4]` instead of a 4 x 7 table |
| 7 | **Every rule gets an automatic check.** Exhaustive enumeration of the discrete layer, proofs for the amounts. | `make check`, `make cbmc` |

Be honest about what a table holds: the 0.45-c source table has four columns but only one bit of information (only
the STK row differs, and `make check` asserts that). Its value is that each meaning has a name and can be reviewed and
changed on its own.

## Checks

Run in `venus-addons/batmonitor/c`:

| Command | What it does | Time |
|---------|--------------|------|
| `make test` | unit tests: plant simulation, setpoints and rule texts per scenario | seconds |
| `make check` | enumerates every input / state combination of the decision layer (invariants, fixed point after one step, reachability), prints the discharge matrix, plus amount properties over 200000 random cycles | ~1 min |
| `make fuzz-compare OLD=...` | differential test of the setpoints and rule texts against an older `bm_logic.c` | ~10 s |
| `make cbmc` | CBMC proofs of the predicate constraints and the amount formula, plus one harness that must give a counterexample | seconds |
| `make cbmc-full` | adds the floating-point / split proofs (`h_alloc_discharge`, `h_charge`, `h_discharge`) | minutes, ~5 GB RAM |
| `make cbmc-remote CBMC_HOST=... CBMC_REMOTE_BIN=...` | runs `cbmc-full` on a faster build host | |

CBMC notes: use cbmc 5.95 with `--external-sat-solver cadical`; cbmc 6.6 blows the `h_charge` formula up from
~0.5 GB to more than 8 GB CNF. Keep the CNF files off tmpfs (`TMPDIR`). Harness assumptions must match the real
system (e.g. the phases of one bank share one cap) and are stated next to each harness.

## Order of work for a change

1. Write down the decision in words and the name it gets in the rule text.
2. Find every place the decision is used; decide whether it is a new row, a new column or a new axis.
3. Refactor so the current behaviour sits in the table; `make test check`, `make fuzz-compare` must be identical.
4. Change the behaviour in one place; add a unit test for exactly that case; quantify the difference with
   `make fuzz-compare` and explain every cycle that differs by more than rounding.
5. Extend `make check` invariants / CBMC harnesses for the new rule; `make cbmc-full`.
6. Bump the version, build and test on the target, deploy with a backup of the old binary, check the live state.
