# victron

Venus OS (Victron Energy) built from source and run as a **systemd-nspawn subsystem under Raspberry Pi OS trixie (arm64)** on a Raspberry Pi 4. Target: 3× MultiPlus-II 48/5000 as a three-phase system, connected through one MK3-USB.

The host stays a normal Debian system (apt, systemd, own services); Venus OS large (GUI v2, Node-RED, Signal K, MQTT, Modbus-TCP, VRM, ESS) runs beside it.

## Layout

| Path | Purpose |
|---|---|
| `venus-build/Dockerfile` | Ubuntu 24.04 build container for Yocto scarthgap (newer hosts such as Ubuntu 26.04 / gcc 15 are too new) |
| `venus-build/build_rpi4.sh` | `bitbake -k venus-image-large` for `MACHINE=raspberrypi4`, run inside the container |
| `pi-gen/config.example` | pi-gen config: trixie arm64 Lite + `stage-venus` |
| `pi-gen/stage-venus/` | extra pi-gen stage: systemd-container, can-utils, NetworkManager WiFi profile |
| `pi-gen/setup.sh` | copies `stage-venus` into a pi-gen checkout |
| `tools/mk3_version.py` | reads the MK3-USB firmware version (MK2 protocol 'V' frame, 2400 8N1) |

## Venus OS build

```
git clone https://github.com/victronenergy/venus.git && cd venus
git checkout v3.80
# meta-openembedded is listed with an ssh URL; fetch over https instead
GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=url.https://github.com/.insteadOf GIT_CONFIG_VALUE_0=git@github.com: make fetch
./repos checkout v3.80
cp /path/to/victron/venus-build/build_rpi4.sh .
sudo docker build -t venus-build:24.04 /path/to/victron/venus-build
sudo docker run -d --name venus-build -v $PWD:$PWD -w $PWD venus-build:24.04 ./build_rpi4.sh
```

The private layer `meta-victronenergy-private` is not public. Its closed-source packages are installed afterwards from the official feed of the same release (`https://updates.victronenergy.com/feeds/venus/release/packages/scarthgap/`):

- `raspberrypi4/`: `mk2-dbus` (VE.Bus / MultiPlus), `vecan-dbus`, `vrmlogger`
- `cortexa7hf-neon-vfpv4/`: `hub4control` (ESS), `mk2vsc`, `can-bus-bms`, `vedirect-interface`, `dbus-cgwacs`, `dbus-fronius`, `gps-dbus`

Venus for raspberrypi4 is 32-bit armv7 (`cortexa7hf-neon-vfpv4`); the arm64 Raspberry Pi OS kernel runs it through its 32-bit compat layer.

## Host image (pi-gen)

```
git clone --branch 2026-09-15-raspios-trixie-arm64 https://github.com/RPi-Distro/pi-gen.git
./pi-gen/setup.sh /path/to/pi-gen          # from this repo
cp pi-gen/config.example /path/to/pi-gen/config   # fill in password and authorized_keys
cp pi-gen/stage-venus/01-wifi/files/wifi.env.example /path/to/pi-gen/stage-venus/01-wifi/files/wifi.env  # SSID, PSK (empty = open network)
cd /path/to/pi-gen && sudo ./build-docker.sh
```

Building arm64 on an x86 host needs `qemu-user` and `qemu-user-binfmt`.

## MK3-USB

The MK3 microcontroller is powered from VE.Bus. Without a connected, awake MultiPlus it does not answer at all, so `tools/mk3_version.py` only works once the VE.Bus is connected.

## Status

- [x] Venus v3.80 layers fetched, Yocto build running
- [x] Raspberry Pi OS trixie base image built
- [ ] Venus rootfs + closed packages + nspawn unit (MK3, can0, `/data`)
- [ ] Final image with WiFi and Venus, flash Pi 4
- [ ] MK3 firmware read-out with VE.Bus connected

## Prior art

- [ehedman/victron-venus-container](https://github.com/ehedman/victron-venus-container) — official Venus rootfs in nspawn/docker/chroot
- [RafaelKa/victron-venus-os-in-docker](https://github.com/RafaelKa/victron-venus-os-in-docker) — official rootfs in Docker, aarch64
- [M-o-a-T/venusian](https://github.com/M-o-a-T/venusian) — Venus on Debian without container
