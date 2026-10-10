# Build inside Venus (gcc 13 and the dbus headers are in the image; libmosquitto and libcjson exist only as .so.1,
# their headers are in include/: mosquitto 2.0.22 and cJSON 1.7.19 from the Venus v3.81 sources)
CFLAGS ?= -O2 -Wall -Wextra
CPPFLAGS += -Iinclude -I/usr/include/dbus-1.0 -I/usr/lib/dbus-1.0/include
LDLIBS += -l:libdbus-1.so.3 -l:libmosquitto.so.1 -l:libcjson.so.1 -lpthread -lm

batmonitor: batmonitor.c bm_logic.c bm_logic.h
	$(CC) $(CFLAGS) $(CPPFLAGS) -o $@ batmonitor.c bm_logic.c $(LDLIBS)

# logic tests with a plant model: no D-Bus/MQTT needed, runs on any host (dell, notebook, Venus)
test: tests/test_logic
	./tests/test_logic

tests/test_logic: tests/test_logic.c bm_logic.c bm_logic.h
	$(CC) $(CFLAGS) -I. -o $@ tests/test_logic.c bm_logic.c -lm

clean:
	rm -f batmonitor tests/test_logic tests/check_matrix

# behaviour comparison against an older bm_logic.c (refactor check): same random inputs, outputs must be identical
# usage: git show HEAD:venus-addons/batmonitor/c/bm_logic.c > /tmp/bm_old.c && make fuzz-compare OLD=/tmp/bm_old.c
fuzz-compare: tests/fuzz_dump.c bm_logic.c bm_logic.h
	$(CC) -O2 -I. -o /tmp/bm_fz_new tests/fuzz_dump.c bm_logic.c -lm
	$(CC) -O2 -I. -o /tmp/bm_fz_old tests/fuzz_dump.c $(OLD) -lm
	for s in 1 2 3 4 5; do /tmp/bm_fz_old $$s > /tmp/bm_fz_old.$$s; /tmp/bm_fz_new $$s > /tmp/bm_fz_new.$$s; \
		cmp -s /tmp/bm_fz_old.$$s /tmp/bm_fz_new.$$s && echo "seed $$s identical" || { echo "seed $$s DIFFERS"; exit 1; }; done

# exhaustive check of the decision layer (bm_d1 / bm_d2 / bm_dlead / bm_d3) + amount properties of bm_step
check: tests/check_matrix
	./tests/check_matrix

tests/check_matrix: tests/check_matrix.c bm_logic.c bm_logic.h
	$(CC) $(CFLAGS) -I. -o $@ tests/check_matrix.c bm_logic.c -lm

# CBMC proofs (tests/cbmc_harness.c; apt install cbmc cadical; not on Venus). "cbmc": the predicate proofs (seconds)
# plus the counterexample check (without the threshold order the check_matrix constraints must fail);
# "cbmc-full" adds the floating-point amount proofs (cbmc 5.95 + cadical: alloc_discharge ~6 min, charge ~6 min,
# discharge ~1 min)
CBMC_BIN ?= cbmc
CBMC = $(CBMC_BIN) tests/cbmc_harness.c -I. --unwind 32 --unwinding-assertions --external-sat-solver cadical
CBMC_FAST = h_d2_bits h_d1_bits
CBMC_SLOW = h_alloc_discharge h_charge h_discharge
# one target per harness, so "make -j cbmc-full" runs them in parallel (the slow ones take minutes each)
cbmc-h_%:
	$(CBMC) --function h_$* > /dev/null && echo "h_$* proved" || { echo "h_$* FAILED"; exit 1; }
cbmc-any-order:
	! $(CBMC) --function h_d2_bits_any_order > /dev/null && echo "h_d2_bits_any_order: counterexample as expected"
cbmc: $(addprefix cbmc-,$(CBMC_FAST)) cbmc-any-order
cbmc-full: cbmc $(addprefix cbmc-,$(CBMC_SLOW))

# run the proofs on a faster build host (cbmc + cadical there): sync sources, make -j there, e.g.
#   make cbmc-remote CBMC_HOST=user@host CBMC_REMOTE_BIN='LD_LIBRARY_PATH=~/cbmc/bin595 ~/cbmc/bin595/cbmc'
# Use cbmc 5.95: 6.6 (Ubuntu 26.04) blows the h_charge formula up from ~0.5 GB to > 8 GB CNF (OOM).
# h_charge needs ~5 GB RAM; the CNF files go to $(CBMC_DIR)/tmp, not to /tmp (often a tmpfs).
CBMC_HOST ?= dell
CBMC_DIR ?= cbmc/batmonitor
CBMC_REMOTE_BIN ?= cbmc
cbmc-remote:
	tar cf - bm_logic.c bm_logic.h Makefile tests/cbmc_harness.c | ssh $(CBMC_HOST) 'mkdir -p $(CBMC_DIR)/tmp && tar xf - -C $(CBMC_DIR)'
	ssh $(CBMC_HOST) 'cd $(CBMC_DIR) && TMPDIR=$$PWD/tmp make -j8 --no-print-directory cbmc-full CBMC_BIN="$(CBMC_REMOTE_BIN)"'
