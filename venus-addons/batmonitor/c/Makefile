# Build inside Venus (gcc 13 and the dbus headers are in the image; libmosquitto and libcjson exist only as .so.1,
# their headers are in include/: mosquitto 2.0.22 and cJSON 1.7.19 from the Venus v3.81 sources)
CFLAGS ?= -O2 -Wall -Wextra
CPPFLAGS += -Iinclude -I/usr/include/dbus-1.0 -I/usr/lib/dbus-1.0/include
LDLIBS += -l:libdbus-1.so.3 -l:libmosquitto.so.1 -l:libcjson.so.1 -lpthread -lm

batmonitor: batmonitor.c bm_logic.c bm_logic.h bm_parse.c bm_parse.h
	$(CC) $(CFLAGS) $(CPPFLAGS) -o $@ batmonitor.c bm_logic.c bm_parse.c $(LDLIBS)

# logic tests with a plant model: no D-Bus/MQTT needed, runs on any host (dell, notebook, Venus)
test: tests/test_logic tests/test_parse
	./tests/test_logic
	./tests/test_parse

# MQTT payload parsing of batmonitor.c (bm_parse.c) with real payloads; cJSON from include/ + libcjson.so.1
tests/test_parse: tests/test_parse.c bm_parse.c bm_parse.h
	$(CC) $(CFLAGS) -I. -Iinclude -o $@ tests/test_parse.c bm_parse.c -l:libcjson.so.1 -lm

tests/test_logic: tests/test_logic.c bm_logic.c bm_logic.h
	$(CC) $(CFLAGS) -I. -o $@ tests/test_logic.c bm_logic.c -lm

clean:
	rm -f batmonitor tests/test_logic tests/test_parse tests/check_matrix

# behaviour comparison against an older revision (refactor check): same random inputs, setpoints and rule texts must
# be identical. The old bm_logic.c, bm_logic.h and tests/fuzz_dump.c come from git, so a changed header does not break
# the old build. usage: make fuzz-compare [REV=HEAD]
REV ?= HEAD
FZ = /tmp/bm_fz
fuzz-compare: tests/fuzz_dump.c bm_logic.c bm_logic.h
	rm -rf $(FZ) && mkdir -p $(FZ)/old/tests
	for f in bm_logic.c bm_logic.h tests/fuzz_dump.c; do git show $(REV):./$$f > $(FZ)/old/$$f || exit 1; done
	$(CC) -O2 -I$(FZ)/old -o $(FZ)/fz_old $(FZ)/old/tests/fuzz_dump.c $(FZ)/old/bm_logic.c -lm
	$(CC) -O2 -I. -o $(FZ)/fz_new tests/fuzz_dump.c bm_logic.c -lm
	@fail=0; for s in 1 2 3 4 5; do $(FZ)/fz_old $$s > $(FZ)/old.$$s; $(FZ)/fz_new $$s > $(FZ)/new.$$s; \
		n=$$(diff $(FZ)/old.$$s $(FZ)/new.$$s | grep -c '^>'); \
		if [ $$n -eq 0 ]; then echo "seed $$s identical"; else echo "seed $$s: $$n of $$(wc -l < $(FZ)/new.$$s) cycles differ"; fail=1; fi; \
	done; [ $$fail -eq 0 ] || { echo "outputs in $(FZ)/old.<seed> / new.<seed>"; exit 1; }

# the Python port (../bm_logic.py) against this C logic: fuzz_dump prints every cycle's inputs ("io"), fuzz_replay.py
# runs bm_logic.py on them; setpoints, rule texts, PI and the full_at forecast must be byte-identical (5 x 48000
# cycles + 36000 forecasts, ~1 min). Then the payload tests of bm_parse.py.
py-compare: tests/fuzz_dump.c bm_logic.c bm_logic.h ../bm_logic.py ../tests/fuzz_replay.py
	mkdir -p $(FZ)
	$(CC) -O2 -I. -o $(FZ)/fz_io tests/fuzz_dump.c bm_logic.c -lm
	@fail=0; for s in 1 2 3 4 5; do $(FZ)/fz_io $$s io > $(FZ)/io.$$s; grep -v -E '^(IN|FCIN) ' $(FZ)/io.$$s > $(FZ)/c.$$s; \
		python3 ../tests/fuzz_replay.py < $(FZ)/io.$$s > $(FZ)/py.$$s || fail=1; \
		n=$$(diff $(FZ)/c.$$s $(FZ)/py.$$s | grep -c '^>'); \
		if [ $$n -eq 0 ]; then echo "seed $$s: C and Python identical ($$(wc -l < $(FZ)/c.$$s) lines)"; \
		else echo "seed $$s: $$n lines differ"; fail=1; fi; \
	done; [ $$fail -eq 0 ] || { echo "outputs in $(FZ)/c.<seed> / py.<seed>"; exit 1; }
	python3 ../tests/test_parse.py

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
CBMC_FAST = h_d2_bits h_d1_bits h_dis_amount
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
