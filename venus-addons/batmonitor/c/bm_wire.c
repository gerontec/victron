/* bm_wire: see bm_wire.h */
#include "bm_wire.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

enum { WT_D, WT_I, WT_T };
struct wfield {
	const char *name;
	size_t off;
	int type, n;
};

#define F(field, type, n) {#field, offsetof(struct bm_in, field), type, n}
static const struct wfield IN_FIELDS[] = {
	F(now, WT_D, 1), F(t, WT_T, 1), F(have_pcc, WT_I, 1), F(have_pv, WT_I, 1), F(pcc, WT_D, 1), F(pv, WT_D, 1),
	F(bat1, WT_D, 1), F(pcc_avg5, WT_D, 1), F(bat1_avg5, WT_D, 1), F(inv_time, WT_D, 1), F(have_soc_bat1, WT_I, 1),
	F(soc_bat1, WT_D, 1), F(r290_hz, WT_I, 1), F(r290_time, WT_D, 1), F(aussen, WT_D, 1), F(aussen_time, WT_D, 1),
	F(wp, WT_D, 1), F(wp_time, WT_D, 1), F(season, WT_I, 1), F(season_ts, WT_T, 1), F(fc_target, WT_D, 1),
	F(fc_ts, WT_T, 1), F(ac_ok, WT_I, NPH), F(ac_in, WT_D, NPH), F(ac_out_ok, WT_I, NPH), F(ac_out, WT_D, NPH),
	F(bms_ok, WT_I, NBANK), F(soc, WT_D, NBANK), F(power_ok, WT_I, NBANK), F(power, WT_D, NBANK),
	F(volt_ok, WT_I, NBANK), F(volt, WT_D, NBANK), F(lim_ok, WT_I, NBANK), F(ccl, WT_D, NBANK), F(dcl, WT_D, NBANK),
};
#undef F
#define N_IN_FIELDS ((int)(sizeof(IN_FIELDS) / sizeof(IN_FIELDS[0])))

/* the bm_cfg flags that are no BM_PARAMS */
struct wflag {
	const char *name;
	size_t off;
};
static const struct wflag CFG_FLAGS[] = {
	{"ladesperre", offsetof(struct bm_cfg, ladesperre)},
	{"pi_armed", offsetof(struct bm_cfg, pi_armed)},
	{"forecast", offsetof(struct bm_cfg, forecast)},
};
#define N_CFG_FLAGS ((int)(sizeof(CFG_FLAGS) / sizeof(CFG_FLAGS[0])))

/* append printf output at *pos; -1 once the buffer is full */
static int put(char *buf, size_t n, size_t *pos, const char *fmt, double d, long long ll, int is_ll)
{
	if (*pos >= n)
		return -1;
	int k = is_ll ? snprintf(buf + *pos, n - *pos, fmt, ll) : snprintf(buf + *pos, n - *pos, fmt, d);
	if (k < 0 || (size_t)k >= n - *pos)
		return -1;
	*pos += (size_t)k;
	return 0;
}

int bm_wire_in_encode(const struct bm_in *in, char *buf, size_t n)
{
	size_t pos = 0;
	const char *base = (const char *)in;
	if (!n)
		return -1;
	buf[0] = 0;
	for (int f = 0; f < N_IN_FIELDS; f++) {
		const struct wfield *w = &IN_FIELDS[f];
		int k = snprintf(buf + pos, n - pos, "%s%s=", pos ? ";" : "", w->name);
		if (k < 0 || (size_t)k >= n - pos)
			return -1;
		pos += (size_t)k;
		for (int i = 0; i < w->n; i++) {
			const char *sep = i ? "," : "";
			if (pos + 1 >= n)
				return -1;
			strcat(buf + pos, sep);
			pos += strlen(sep);
			int r;
			if (w->type == WT_D)
				r = put(buf, n, &pos, "%.17g", ((const double *)(base + w->off))[i], 0, 0);
			else if (w->type == WT_I)
				r = put(buf, n, &pos, "%lld", 0, ((const int *)(base + w->off))[i], 1);
			else
				r = put(buf, n, &pos, "%lld", 0, (long long)((const time_t *)(base + w->off))[i], 1);
			if (r)
				return -1;
		}
	}
	return (int)pos;
}

/* next "key=value" item of s at *p: key and value copied (value up to ';'), 0 at the end */
static int next_item(const char **p, char *key, size_t nk, char *val, size_t nv)
{
	const char *s = *p;
	while (*s == ';' || *s == ' ')
		s++;
	if (!*s)
		return 0;
	const char *eq = strchr(s, '='), *end = strchr(s, ';');
	if (!end)
		end = s + strlen(s);
	*p = end;
	if (!eq || eq > end || (size_t)(eq - s) >= nk || (size_t)(end - eq - 1) >= nv) {
		key[0] = 0;                                     /* malformed or too long: skipped */
		return 1;
	}
	memcpy(key, s, (size_t)(eq - s));
	key[eq - s] = 0;
	memcpy(val, eq + 1, (size_t)(end - eq - 1));
	val[end - eq - 1] = 0;
	return 1;
}

int bm_wire_in_decode(const char *s, struct bm_in *in)
{
	char key[32], val[256];
	char *base = (char *)in;
	int got = 0;
	while (next_item(&s, key, sizeof(key), val, sizeof(val))) {
		for (int f = 0; f < N_IN_FIELDS; f++) {
			const struct wfield *w = &IN_FIELDS[f];
			if (strcmp(key, w->name))
				continue;
			char *v = val;
			for (int i = 0; i < w->n && *v; i++) {
				char *e;
				if (w->type == WT_D)
					((double *)(base + w->off))[i] = strtod(v, &e);
				else if (w->type == WT_I)
					((int *)(base + w->off))[i] = (int)strtol(v, &e, 10);
				else
					((time_t *)(base + w->off))[i] = (time_t)strtoll(v, &e, 10);
				v = *e == ',' ? e + 1 : e;
			}
			got++;
			break;
		}
	}
	return got;
}

int bm_wire_cfg_encode(const struct bm_cfg *cfg, char *buf, size_t n)
{
	size_t pos = 0;
	if (!n)
		return -1;
	buf[0] = 0;
	for (int i = 0; i < BM_NPARAMS; i++) {
		int k = snprintf(buf + pos, n - pos, "%s%s=%.17g", pos ? ";" : "", BM_PARAMS[i].name,
						 *bm_param_ptr((struct bm_cfg *)cfg, i));
		if (k < 0 || (size_t)k >= n - pos)
			return -1;
		pos += (size_t)k;
	}
	for (int i = 0; i < N_CFG_FLAGS; i++) {
		int k = snprintf(buf + pos, n - pos, ";%s=%d", CFG_FLAGS[i].name,
						 *(const int *)((const char *)cfg + CFG_FLAGS[i].off));
		if (k < 0 || (size_t)k >= n - pos)
			return -1;
		pos += (size_t)k;
	}
	return (int)pos;
}

int bm_wire_cfg_decode(const char *s, struct bm_cfg *cfg)
{
	char key[48], val[64];
	int got = 0;
	while (next_item(&s, key, sizeof(key), val, sizeof(val))) {
		double v = strtod(val, NULL);
		for (int i = 0; i < BM_NPARAMS; i++)
			if (!strcmp(key, BM_PARAMS[i].name)) {
				got += bm_param_set(cfg, i, v) == 0;
				goto next;
			}
		for (int i = 0; i < N_CFG_FLAGS; i++)
			if (!strcmp(key, CFG_FLAGS[i].name)) {
				*(int *)((char *)cfg + CFG_FLAGS[i].off) = (int)v;
				got++;
				break;
			}
	next:;
	}
	return got;
}
