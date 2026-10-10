/*
 * bm_wire: text encoding of struct bm_in and of the parameters (0.47-c), so a second instance - the ESP32 shadow on
 * the Waveshare relay board - runs bm_step on exactly the inputs and parameters of the Venus instance and the two
 * results can be compared cycle by cycle.
 *
 * Format: "key=v[,v...];key=v;..." doubles with %.17g (bit exact), time_t as integer. Unknown keys are skipped, so
 * an older or newer peer still decodes what it knows. When struct bm_in gets a field, add it to IN_FIELDS in
 * bm_wire.c, else the shadow runs without it (and its results show the difference).
 */
#ifndef BM_WIRE_H
#define BM_WIRE_H

#include "bm_logic.h"
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

int bm_wire_in_encode(const struct bm_in *in, char *buf, size_t n);      /* length, -1 = buffer too small */
int bm_wire_in_decode(const char *s, struct bm_in *in);                  /* number of known keys decoded */
int bm_wire_cfg_encode(const struct bm_cfg *cfg, char *buf, size_t n);   /* all BM_PARAMS by name + the flags */
int bm_wire_cfg_decode(const char *s, struct bm_cfg *cfg);               /* number of keys applied */

#ifdef __cplusplus
}
#endif

#endif
