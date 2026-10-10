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
	rm -f batmonitor tests/test_logic

# behaviour comparison against an older bm_logic.c (refactor check): same random inputs, outputs must be identical
# usage: git show HEAD:venus-addons/batmonitor/c/bm_logic.c > /tmp/bm_old.c && make fuzz-compare OLD=/tmp/bm_old.c
fuzz-compare: tests/fuzz_dump.c bm_logic.c bm_logic.h
	$(CC) -O2 -I. -o /tmp/bm_fz_new tests/fuzz_dump.c bm_logic.c -lm
	$(CC) -O2 -I. -o /tmp/bm_fz_old tests/fuzz_dump.c $(OLD) -lm
	for s in 1 2 3 4 5; do /tmp/bm_fz_old $$s > /tmp/bm_fz_old.$$s; /tmp/bm_fz_new $$s > /tmp/bm_fz_new.$$s; \
		cmp -s /tmp/bm_fz_old.$$s /tmp/bm_fz_new.$$s && echo "seed $$s identical" || { echo "seed $$s DIFFERS"; exit 1; }; done
