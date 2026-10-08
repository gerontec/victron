# Build inside Venus (gcc 13 and the dbus headers are in the image; libmosquitto and libcjson exist only as .so.1,
# their headers are in include/: mosquitto 2.0.22 and cJSON 1.7.19 from the Venus v3.81 sources)
CFLAGS ?= -O2 -Wall -Wextra
CPPFLAGS += -Iinclude -I/usr/include/dbus-1.0 -I/usr/lib/dbus-1.0/include
LDLIBS += -l:libdbus-1.so.3 -l:libmosquitto.so.1 -l:libcjson.so.1 -lpthread -lm

batmonitor: batmonitor.c
	$(CC) $(CFLAGS) $(CPPFLAGS) -o $@ $< $(LDLIBS)

clean:
	rm -f batmonitor
