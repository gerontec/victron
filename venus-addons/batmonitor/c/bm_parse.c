/* bm_parse: see bm_parse.h */
#include "bm_parse.h"
#include <cjson/cJSON.h>
#include <ctype.h>
#include <math.h>
#include <stdlib.h>

/* plausible ranges in the units of the payload (kW, %, Hz) */
#define PCC_MAX_KW 100.0
#define BAT1_MAX_KW 20.0
#define PV_MAX_KW 60.0
#define R290_MAX_HZ 200.0

/* o[k] as a finite JSON number in lo..hi; 1 = valid */
static int num(const cJSON *o, const char *k, double lo, double hi, double *v)
{
	const cJSON *x = cJSON_GetObjectItemCaseSensitive(o, k);
	if (!cJSON_IsNumber(x) || !isfinite(x->valuedouble) || x->valuedouble < lo || x->valuedouble > hi)
		return 0;
	*v = x->valuedouble;
	return 1;
}

int bm_parse_inverter(const char *json, struct bm_inv_sample *s)
{
	cJSON *d = cJSON_Parse(json);
	double pcc, bat1, v, pv1, pv2;
	int ok = cJSON_IsObject(d) && num(d, "ActivePower_PCC_Total", -PCC_MAX_KW, PCC_MAX_KW, &pcc)
			 && num(d, "Power_Bat1", -BAT1_MAX_KW, BAT1_MAX_KW, &bat1);
	if (ok) {
		s->pcc = pcc * 1000.0;
		s->bat1 = bat1 * 1000.0;
		s->pcc_avg5 = (num(d, "ActivePower_PCC_Total_avg5", -PCC_MAX_KW, PCC_MAX_KW, &v) ? v : pcc) * 1000.0;
		s->bat1_avg5 = (num(d, "Power_Bat1_avg5", -BAT1_MAX_KW, BAT1_MAX_KW, &v) ? v : bat1) * 1000.0;
		s->have_pv = num(d, "Power_PV1", -0.5, PV_MAX_KW, &pv1) && num(d, "Power_PV2", -0.5, PV_MAX_KW, &pv2);
		s->pv = s->have_pv ? (pv1 + pv2) * 1000.0 : 0.0;
		s->have_soc_bat1 = num(d, "SOC_Bat1", 0.0, 100.0, &v);
		s->soc_bat1 = s->have_soc_bat1 ? v : -1.0;
	}
	cJSON_Delete(d);
	return ok;
}

int bm_parse_r290(const char *json, int *hz)
{
	cJSON *d = cJSON_Parse(json);
	double v;
	int ok = cJSON_IsObject(d) && num(d, "comp_freq_actual", 0.0, R290_MAX_HZ, &v);
	if (ok)
		*hz = (int)v;
	cJSON_Delete(d);
	return ok;
}

int bm_parse_number(const char *text, double lo, double hi, double *v)
{
	char *end;
	double x = strtod(text, &end);
	if (end == text)
		return 0;
	while (isspace((unsigned char)*end))
		end++;
	if (*end || !isfinite(x) || x < lo || x > hi)    /* "123xyz", "nan", "inf" are no numbers here */
		return 0;
	*v = x;
	return 1;
}
