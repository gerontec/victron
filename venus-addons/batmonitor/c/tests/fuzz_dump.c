/* fuzz_dump: random input sequences through bm_step, one line per cycle (setpoints, rules, wp_eff, surplus, PI,
   forecast / Sofar-first flags). Compare two logic versions for identical behaviour:  make fuzz-compare OLD=<old bm_logic.c> */
#include "bm_logic.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
static double r(double a, double b) { return a + (b - a) * (rand() / (double)RAND_MAX); }
static int ri(int n) { return rand() % n; }
int main(int argc, char **argv)
{
	unsigned seed = argc > 1 ? atoi(argv[1]) : 1;
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
			in.pcc_avg5 = in.pcc + r(-500, 500); in.bat1_avg5 = in.bat1 + r(-300, 300);
			in.inv_time = ri(15) ? now : now - 400;
			sb1 += r(-2, 2); if (sb1 < 0) sb1 = 0; if (sb1 > 100) sb1 = 100;
			in.have_soc_bat1 = ri(10) != 0; in.soc_bat1 = ri(6) ? sb1 : r(3, 9);
			in.r290_hz = ri(2) ? ri(90) : 0; in.r290_time = ri(10) ? now : 0;
			in.aussen = r(-15, 35); in.aussen_time = ri(5) ? now : 0;
			in.wp = ri(3) ? r(0, 5000) : r(200, 450); in.wp_time = ri(6) ? now : now - 300;
			in.season = ri(4); in.season_ts = in.t - ri(4) * 86400 / 2;
			in.fc_target = ri(4) ? (ri(2) ? 100 : r(5, 100)) : -1; in.fc_ts = in.t - ri(5) * 3600;
			for (int p = 0; p < NPH; p++) { in.ac_ok[p] = ri(15) != 0; in.ac_in[p] = st.setpoints[p] + r(-200, 200); }
			for (int b = 0; b < NBANK; b++) {
				soc[b] += r(-3, 3); if (soc[b] < 0) soc[b] = 0; if (soc[b] > 100) soc[b] = 100;
				in.bms_ok[b] = ri(25) != 0; in.soc[b] = ri(8) ? soc[b] : r(0, 10);
				in.power_ok[b] = ri(10) != 0; in.power[b] = r(-6000, 6000);
				in.volt_ok[b] = ri(5) != 0; in.volt[b] = r(48, 57);
				in.lim_ok[b] = ri(2); in.ccl[b] = r(0, 150); in.dcl[b] = r(0, 250);
			}
			bm_step(&cfg, &in, &st, &out);
			printf("%d %d %d %d %d\t%s\t%s\t%s\t%.3f %.3f %.3f %.3f %d %d %d %d %d %d\n", run, k, out.sp[0], out.sp[1], out.sp[2],
				   out.why[0], out.why[1], out.why[2], out.wp_eff, out.surplus, out.pi.u, out.pi.y, out.pi.sp[0], out.pi.sp[1],
				   out.pi.sp[2], out.fc_active, out.fc_bad, out.bat1_first);
		}
	}
	return 0;
}
