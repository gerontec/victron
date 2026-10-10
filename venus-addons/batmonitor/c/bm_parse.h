/*
 * bm_parse: the MQTT payloads batmonitor.c reads, as pure functions (0.48-c) so tests/test_parse.c drives them with
 * real payloads. A value counts only when it is a JSON number (cJSON_IsNumber), finite and in a plausible range: a
 * missing key, null, a string or 1e999 never becomes 0 W. A rejected sample leaves the last good one in place, and its
 * timestamp is not refreshed, so the stale logic in bm_logic takes over.
 */
#ifndef BM_PARSE_H
#define BM_PARSE_H

/* inverter/power_grid_exchange/json (sofar_fast.py, kW) -> W */
struct bm_inv_sample {
	double pcc, bat1, pcc_avg5, bat1_avg5;   /* W; the 5 min means fall back to the instant values */
	int have_pv;                             /* Power_PV1 and Power_PV2 both valid */
	double pv;                               /* W */
	int have_soc_bat1;
	double soc_bat1;                         /* % */
};
int bm_parse_inverter(const char *json, struct bm_inv_sample *s);   /* 1 = PCC and Bat1 valid, sample usable */

/* r290/heatpump/all: comp_freq_actual (Hz); 1 = valid */
int bm_parse_r290(const char *json, int *hz);

/* plain number topics (em0/power, outdoor temperature): the whole text one number in lo..hi; 1 = valid */
int bm_parse_number(const char *text, double lo, double hi, double *v);

#endif
