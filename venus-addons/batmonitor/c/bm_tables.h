/*
 * bm_tables.h - the batmonitor decisions as tables. bm_logic.py reads this file; keep the format:
 *
 *   TABLE(name, "column column ...", "output output ...")
 *   ROW(name,   " 1      0      -   ", OUTPUT[, OUTPUT])
 *
 * One condition per column, separated by spaces (aligned under the column names):
 *   1 = the condition is true, 0 = false, - = any.
 * The first matching row wins (top to bottom), so a row only applies when no row above it matched.
 * The last row of every table is all "-": it always yields a result.
 * Outputs: numbers, KEEP (the state stays as it was) or the names BM_SRC_* / BM_WP_* / BM_M_* / LEAD_*.
 * Comments: C block comments on their own lines or at the end of a ROW. Nothing else (no #if, no macros,
 * no expressions): bm_logic.py refuses every other line. Check after a change: make -C c py-compare, and the table test.
 */

/* D1: Sofar first latch. Off at or below BAT1_SOC_MIN, on again from BAT1_SOC_MIN + 2 %. */
TABLE(D1_LATCH, "b1_above_min bat1_first b1_ge_release", "bat1_first")
ROW(D1_LATCH,   "0            -          -            ", 0)
ROW(D1_LATCH,   "1            0          1            ", 1)
ROW(D1_LATCH,   "-            -          -            ", KEEP)

/* D1: Sofar first acts only while the heat pump runs. */
TABLE(D1_B1_FIRST, "bat1_first wp_on", "b1_first")
ROW(D1_B1_FIRST,   "1          1    ", 1)
ROW(D1_B1_FIRST,   "-          -    ", 0)

/* D1: S3 source of the discharge. DAY by day, B1 = Sofar first at night,
 * WPG = winter night with the heat pump running (no night floor), STK = night floor. */
TABLE(D1_SRC, "night b1_first winter wp_on", "src")
ROW(D1_SRC,   "0     -        -      -    ", BM_SRC_DAY)
ROW(D1_SRC,   "1     1        -      -    ", BM_SRC_B1)
ROW(D1_SRC,   "1     -        1      1    ", BM_SRC_WPG)
ROW(D1_SRC,   "-     -        -      -    ", BM_SRC_STK)

/* D1: winter heat pump rule. Stacks give 0 W while the Sofar Bat1 discharges, else at most WP_WINTER_W. */
TABLE(D1_WP_CAP, "winter wp_on b1_dis", "wp_cap")
ROW(D1_WP_CAP,   "1      1     1     ", BM_WP_ZERO)
ROW(D1_WP_CAP,   "1      1     0     ", BM_WP_MAX)
ROW(D1_WP_CAP,   "-      -     -     ", BM_WP_NONE)

/* D2: charge mode on the surplus (> 200 W), held down to 20 W once charging. */
TABLE(D2_CHARGE_MODE, "surplus_gt_on chg_prev surplus_gt_hold", "charge_mode")
ROW(D2_CHARGE_MODE,   "1             -        -              ", 1)
ROW(D2_CHARGE_MODE,   "-             1        1              ", 1)
ROW(D2_CHARGE_MODE,   "-             -        -              ", 0)

/* D2: SoC protection per stack, SOC_MIN .. SOC_MIN_RELEASE (only while the BMS reports). */
TABLE(D2_PROT, "bms_ok soc_lt_min soc_ge_release", "prot")
ROW(D2_PROT,   "1      1          -             ", 1)
ROW(D2_PROT,   "1      -          1             ", 0)
ROW(D2_PROT,   "-      -          -             ", KEEP)

/* D2: grid force charge per stack, SOC_FORCE .. SOC_FORCE_RELEASE. */
TABLE(D2_FORCE, "bms_ok soc_lt_force soc_ge_force_release", "force")
ROW(D2_FORCE,   "1      1            -                   ", 1)
ROW(D2_FORCE,   "1      -            1                   ", 0)
ROW(D2_FORCE,   "-      -            -                   ", KEEP)

/* D2: LOW force charge per stack: DCL 0 or lowest cell < CELL_FORCE_V;
 * ends once DCL is back, the cell >= CELL_FORCE_RELEASE_V and the SoC >= SOC_MIN_RELEASE. */
TABLE(D2_LOW, "bms_ok low_on low_off soc_ge_release", "low")
ROW(D2_LOW,   "1      1      -       -             ", 1)
ROW(D2_LOW,   "1      -      1       1             ", 0)
ROW(D2_LOW,   "-      -      -       -             ", KEEP)

/* D2: mode per stack, priority from top to bottom (force / low / prot = the latches above,
 * ff = force_full_from_pv names this stack, top = top hold window of this stack). */
TABLE(D2_MODE, "bms_ok force low stale charge_mode block soc_lt_100 ff top prot charging", "mode")
ROW(D2_MODE,   "0      -     -   -     -           -     -          -  -   -    -       ", BM_M_BMS_MISSING)
ROW(D2_MODE,   "-      1     -   -     -           -     -          -  -   -    -       ", BM_M_FORCE)
ROW(D2_MODE,   "-      -     1   -     -           -     -          -  -   -    -       ", BM_M_FORCE)
ROW(D2_MODE,   "-      -     -   1     -           -     -          -  -   -    -       ", BM_M_STALE)
ROW(D2_MODE,   "-      -     -   -     1           1     -          -  -   -    -       ", BM_M_LADESPERRE)
ROW(D2_MODE,   "-      -     -   -     1           -     1          -  -   -    -       ", BM_M_CHARGE)
ROW(D2_MODE,   "-      -     -   -     1           -     -          1  -   -    -       ", BM_M_CHARGE)
ROW(D2_MODE,   "-      -     -   -     1           -     -          -  1   -    -       ", BM_M_CHARGE)
ROW(D2_MODE,   "-      -     -   -     1           -     -          -  -   -    -       ", BM_M_FULL)
ROW(D2_MODE,   "-      -     -   -     -           -     -          1  -   -    -       ", BM_M_HOLD)
ROW(D2_MODE,   "-      -     -   -     -           -     -          -  -   1    -       ", BM_M_PROT)
ROW(D2_MODE,   "-      -     -   -     -           -     -          -  -   -    1       ", BM_M_CHARGING)
ROW(D2_MODE,   "-      -     -   -     -           -     -          -  -   -    -       ", BM_M_DIS)

/* SoC balancing: the stack more than SOC_BALANCE_ON ahead leads, cleared below SOC_BALANCE_OFF. */
TABLE(DLEAD, "both_ok has_lead diff_gt_on diff_lt_off", "lead")
ROW(DLEAD,   "0       -        -          -          ", LEAD_NONE)
ROW(DLEAD,   "-       0        1          -          ", LEAD_NEW)
ROW(DLEAD,   "-       1        -          1          ", LEAD_END)
ROW(DLEAD,   "-       -        -          -          ", KEEP)

/* D3: PROPORTIONAL (1) while importing, or (already proportional / night floor) while the deficit > 20 W. */
TABLE(D3, "any_dis house_import prop_prev prop_hold deficit_gt_hold", "prop")
ROW(D3,   "0       -            -         -         -              ", 0)
ROW(D3,   "-       1            -         -         -              ", 1)
ROW(D3,   "-       -            1         -         1              ", 1)
ROW(D3,   "-       -            -         1         1              ", 1)
ROW(D3,   "-       -            -         -         -              ", 0)

/* D4: PCC above 20 kW seen today (display). */
TABLE(D4_PEAK, "pcc_peak new_day", "peak_today")
ROW(D4_PEAK,   "1        -      ", 1)
ROW(D4_PEAK,   "-        1      ", 0)
ROW(D4_PEAK,   "-        -      ", KEEP)

/* D4: summer peak window, outputs block (wait below EXPORT_CAP) and cap (charge only above it). */
TABLE(D4, "enabled season fc_cap_ahead surplus_cap_on chg_prev surplus_cap_hold", "block cap")
ROW(D4,   "0       -      -            -              -        -               ", 0, 0)
ROW(D4,   "-       0      -            -              -        -               ", 0, 0)
ROW(D4,   "-       -      0            -              -        -               ", 0, 0)
ROW(D4,   "-       -      -            1              -        -               ", 0, 1)
ROW(D4,   "-       -      -            -              1        1               ", 0, 1)
ROW(D4,   "-       -      -            -              -        -               ", 1, 1)

/* D5: DO4 pulse (FoxESS shedding): PCC > 20 kW for the grace time or > DO4_HARD_W,
 * or grid offline and PV overfeeds one AC-out; never twice within the lockout. */
TABLE(D5, "lockout_over acout_over pcc_over grace_over pcc_hard", "do4")
ROW(D5,   "0            -          -        -          -       ", 0)
ROW(D5,   "-            1          -        -          -       ", 1)
ROW(D5,   "-            -          1        1          -       ", 1)
ROW(D5,   "-            -          1        -          1       ", 1)
ROW(D5,   "-            -          -        -          -       ", 0)

/* D7: house on AC-out1. HOUSE_ACOUT 0 = off, 1 = on, auto: on above 99 W, off after 60 s below 30 W. */
TABLE(D7, "mode_off mode_on over_on under_off under_long on", "on under")
ROW(D7,   "1        -       -       -         -          - ", 0, 0)
ROW(D7,   "-        1       -       -         -          - ", 1, 0)
ROW(D7,   "-        -       1       -         -          - ", 1, 0)
ROW(D7,   "-        -       -       1         1          - ", 0, 1)
ROW(D7,   "-        -       -       1         -          1 ", 1, 1)
ROW(D7,   "-        -       -       1         -          - ", 0, 1)
ROW(D7,   "-        -       -       -         -          - ", KEEP, 0)
