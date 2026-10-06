#!/usr/bin/env python3
"""Read the MK3-USB firmware version: listen for MK2-protocol V frames at 2400 8N1, query once if none arrive."""
import glob, serial, time, sys

# port as argument, otherwise the first MK3-USB found by id
PORT = sys.argv[1] if len(sys.argv) > 1 else (glob.glob("/dev/serial/by-id/usb-VictronEnergy_MK3-USB*") or ["/dev/ttyUSB0"])[0]

def frames(buf):
    """Yield (cmd, data) for every checksum-valid frame <len><0xFF><cmd><data..><cs>."""
    i = 0
    while i + 2 < len(buf):
        n = buf[i]
        if buf[i + 1] == 0xFF and 2 <= n <= 0x20 and i + n + 1 < len(buf):
            fr = buf[i:i + n + 2]
            if sum(fr) & 0xFF == 0:
                yield fr[2], fr[3:-1]
                i += n + 2
                continue
        i += 1

def read_for(s, secs):
    end, buf = time.time() + secs, bytearray()
    while time.time() < end:
        buf += s.read(64)
    return bytes(buf)

with serial.Serial(PORT, 2400, timeout=0.2) as s:
    raw = read_for(s, 10)
    mode = "passive"
    if not any(c == 0x56 for c, _ in frames(raw)):
        s.write(bytes([0x02, 0xFF, 0x56, 0xA9]))  # V version request
        raw += read_for(s, 3)
        mode = "after V request"
print("raw bytes:", len(raw), raw[:48].hex(" "))
seen = set()
for cmd, data in frames(raw):
    if cmd == 0x56 and len(data) >= 4:
        ver = int.from_bytes(data[:4], "little")
        key = (ver, data[4] if len(data) > 4 else None)
        if key not in seen:
            seen.add(key)
            print(f"[{mode}] MK3 firmware version {ver}  mode byte {key[1]!r}  frame data {data.hex(" ")}")
if not seen:
    print("no version frame found"); sys.exit(1)
