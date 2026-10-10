/* fuzz_dump: random input sequences through bm_step, one line per cycle (setpoints, rules, wp_eff, surplus, PI,
   forecast / Sofar-first flags). Compare two logic versions for identical behaviour:  make fuzz-compare REV=<commit>.
   "fuzz_dump <seed> io" also prints every cycle's inputs (line "IN ...", %.17g = bit exact) before its result, so
   tests/fuzz_replay.py runs the Python port (bm_logic.py) on the same inputs:  make py-compare */
#include "bm_logic.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
static double r(double a, double b) { return a + (b - a) * (rand() / (double)RAND_MAX); }
static int ri(int n) { return rand() % n; }
int main(int argc, char **argv)
{
	unsigned seed = argc > 1 ? atoi(argv[1]) : 1;
	int io = argc > 2 && !strcmp(argv[2], "io");
	srand(seed);
	setenv("TZ", "Europe/Berlin", 1);
	for (int run = 0; run < 400; run++) {
		struct bm_cfg cfg; struct bm_state st; struct bm_out out; struct bm_in in;
		bm_cfg_default(&cfg);
		cfg.pi_armed = ri(4) == 0;
		cfg.forecast = ri(5) != 0;
		bm_init(&st);
		time_t t0 = 1767225600 + (time_t)ri(365) * 86400 + ri(86400);
		double now = 1000, soc[2] = {r(0, 100), r(0, 100)}, sb1 = r(0, 100);
		for (int k = 0; k < 120; k++, now += 5) {
			memset(&in, 0, sizeof(in));
			in.now = now; in.t = t0 + k * 5;
			in.have_pcc = ri(20) != 0; in.have_pv = ri(20) != 0;
			in.pcc = r(-8000, 12000); in.pv = ri(3) ? r(0, 25000) : r(0, 150); in.bat1 = r(-2500, 2500);
			if (run % 7 == 0 && k >= 30 && k < 90)          /* 0.50-c: export peaks for D5 (deterministic, no rand) */
				in.pcc = 21000 + 400 * (k % 31);
			in.pcc_avg5 = in.pcc + r(-500, 500); in.bat1_avg5 = in.bat1 + r(-300, 300);
			in.inv_time = ri(15) ? now : now - 400;
			sb1 += r(-2, 2); if (sb1 < 0) sb1 = 0; if (sb1 > 100) sb1 = 100;
			in.have_soc_bat1 = ri(10) != 0; in.soc_bat1 = ri(6) ? sb1 : r(3, 9);
			in.r290_hz = ri(2) ? ri(90) : 0; in.r290_time = ri(10) ? now : 0;
			in.aussen = r(-15, 35); in.aussen_time = ri(5) ? now : 0;
			in.wp = ri(3) ? r(0, 5000) : r(200, 450); in.wp_time = ri(6) ? now : now - 300;
			in.season = ri(4); in.season_ts = in.t - ri(4) * 86400 / 2;
			in.fc_target = ri(4) ? (ri(2) ? 100 : r(5, 100)) : -1; in.fc_ts = in.t - ri(5) * 3600;
			/* 0.49-c: OWM slots for the peak window, deterministic (no rand: the input sequence stays the one of older
			   revisions, so make fuzz-compare still compares the same inputs) */
			in.fc_corr = (run % 3) * 0.5;
			in.fc_n = (run + k / 20) % 9;
			for (int i = 0; i < in.fc_n; i++) {
				in.fc_slot_t[i] = (in.t / 10800) * 10800 + (time_t)(i - 2) * 10800;
				in.fc_slot_kt[i] = 0.15 + 0.2 * ((run + i) % 5);
			}
			for (int p = 0; p < NPH; p++) { in.ac_ok[p] = ri(15) != 0; in.ac_in[p] = st.setpoints[p] + r(-200, 200); }
			in.grid_ok = 1;
			if (run % 5 == 1 && k >= 60 && k < 100) {       /* 0.51-c: PV feed into AC-out, grid off part of the time */
				in.grid_ok = (k / 10) % 2;                  /* (deterministic, no rand) */
				for (int p = 0; p < NPH; p++) {
					in.ac_out_ok[p] = 1;
					in.ac_out[p] = p == 1 ? -(4000 + 100 * (k % 25)) : 30;
				}
			}
			for (int b = 0; b < NBANK; b++) {
				soc[b] += r(-3, 3); if (soc[b] < 0) soc[b] = 0; if (soc[b] > 100) soc[b] = 100;
				in.bms_ok[b] = ri(25) != 0; in.soc[b] = ri(8) ? soc[b] : r(0, 10);
				in.power_ok[b] = ri(10) != 0; in.power[b] = r(-6000, 6000);
				in.volt_ok[b] = ri(5) != 0; in.volt[b] = r(48, 57);
				in.lim_ok[b] = ri(2); in.ccl[b] = r(0, 150); in.dcl[b] = r(0, 250);
			}
			if (io) {
				printf("IN %d %d %d %d %.17g %lld %d %d %.17g %.17g %.17g %.17g %.17g %.17g %d %.17g %d %.17g %.17g %.17g "
					   "%.17g %.17g %d %lld %.17g %lld", run, k, cfg.pi_armed, cfg.forecast, in.now, (long long)in.t,
					   in.have_pcc, in.have_pv, in.pcc, in.pv, in.bat1, in.pcc_avg5, in.bat1_avg5, in.inv_time,
					   in.have_soc_bat1, in.soc_bat1, in.r290_hz, in.r290_time, in.aussen, in.aussen_time, in.wp,
					   in.wp_time, in.season, (long long)in.season_ts, in.fc_target, (long long)in.fc_ts);
				for (int p = 0; p < NPH; p++)
					printf(" %d %.17g", in.ac_ok[p], in.ac_in[p]);
				for (int b = 0; b < NBANK; b++)
					printf(" %d %.17g %d %.17g %d %.17g %d %.17g %.17g", in.bms_ok[b], in.soc[b], in.power_ok[b],
						   in.power[b], in.volt_ok[b], in.volt[b], in.lim_ok[b], in.ccl[b], in.dcl[b]);
				printf(" %d", in.grid_ok);
				for (int p = 0; p < NPH; p++)
					printf(" %d %.17g", in.ac_out_ok[p], in.ac_out[p]);
				printf(" %.17g %d", in.fc_corr, in.fc_n);
				for (int i = 0; i < in.fc_n; i++)
					printf(" %lld %.17g", (long long)in.fc_slot_t[i], in.fc_slot_kt[i]);
				putchar('\n');
			}
			bm_step(&cfg, &in, &st, &out);
			if (io && k % 7 == 0) {                         /* the full_at forecast too, deterministic slots (no rand) */
				struct bm_fc_in fi;
				struct bm_fc_out fo;
				memset(&fi, 0, sizeof(fi));
				fi.t = in.t;
				fi.have_pv = in.have_pv;
				fi.pv_sofar = in.pv;
				fi.lead = st.lead;
				fi.corr = (run % 3) * 0.5;
				fi.n_slots = run % 5;
				for (int i = 0; i < fi.n_slots; i++) {
					fi.slot_t[i] = (in.t / 10800) * 10800 + (time_t)i * 10800;
					fi.slot_kt[i] = 0.1 + 0.2 * ((k + i) % 5);
				}
				for (int b = 0; b < NBANK; b++) {
					fi.soc_ok[b] = st.soc_ok[b];
					fi.soc[b] = st.soc[b];
					fi.has_avg[b] = in.power_ok[b];
					fi.avg[b] = in.power[b];
					fi.volt_ok[b] = in.volt_ok[b];
					fi.volt[b] = in.volt[b];
				}
				bm_full_forecast(&cfg, &fi, &fo);
				printf("FCIN %d %d %lld %d %.17g %d %.17g %d", run, k, (long long)fi.t, fi.have_pv, fi.pv_sofar, fi.lead,
					   fi.corr, fi.n_slots);
				for (int i = 0; i < fi.n_slots; i++)
					printf(" %lld %.17g", (long long)fi.slot_t[i], fi.slot_kt[i]);
				for (int b = 0; b < NBANK; b++)
					printf(" %d %.17g %d %.17g %d %.17g", fi.soc_ok[b], fi.soc[b], fi.has_avg[b], fi.avg[b], fi.volt_ok[b],
						   fi.volt[b]);
				printf("\nFC %d %d %lld %lld %.6f %.6f %.6f %.6f %.3f %.6f %d\n", run, k, (long long)fo.full_at[0],
					   (long long)fo.full_at[1], fo.soc_sunset[0], fo.soc_sunset[1], fo.anchor, fo.kt_now, fo.pv_now_w,
					   fo.rest_kwh, fo.weather);
			}
			printf("%d %d %d %d %d\t%s\t%s\t%s\t%.3f %.3f %.3f %.3f %d %d %d %d %d %d\n", run, k, out.sp[0], out.sp[1], out.sp[2],
				   out.why[0], out.why[1], out.why[2], out.wp_eff, out.surplus, out.pi.u, out.pi.y, out.pi.sp[0], out.pi.sp[1],
				   out.pi.sp[2], out.fc_active, out.fc_bad, out.bat1_first);
			if (io)                                         /* the DO4 pulse too (D5 changes no setpoint) */
				printf("D5 %d %d %d\n", run, k, out.do4_pulse);
		}
	}
	return 0;
}
