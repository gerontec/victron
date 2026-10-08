# victron

Venus OS (Victron Energy) built from source and run as a **systemd-nspawn subsystem under Raspberry Pi OS trixie (arm64)** on a Raspberry Pi 4. Target: 3× MultiPlus-II 48/5000 as a three-phase system, connected through one MK3-USB.

The host stays a normal Debian system (apt, systemd, own services); Venus OS large (GUI v2, Node-RED, Signal K, MQTT, Modbus-TCP, VRM, ESS) runs beside it.

## Layout

| Path | Purpose |
|---|---|
| `venus/` | submodule: victronenergy/venus at v3.81 (the Yocto build setup) |
| `venus-build/layers.lock` | exact commits of all Yocto layers of the build |
| `venus-build/Dockerfile` | Ubuntu 24.04 build container for Yocto scarthgap (newer hosts such as Ubuntu 26.04 / gcc 15 are too new) |
| `venus-build/local.conf.append` | public part of Victron's private `packagegroup-ve-addons` (GUI v2, venus-platform, flashmq, dbus-modbus-client, ...) |
| `venus-build/gitconfig` | mounted as `~/.gitconfig`: rewrites the ssh GitHub URLs of some recipes/submodules to https |
| `venus-build/build_rpi4.sh` | `bitbake -k venus-image-large` for `MACHINE=raspberrypi4`, run inside the container |
| `pi-gen/config.example` | pi-gen config: trixie arm64 Lite + `stage-venus` |
| `pi-gen/stage-venus/` | extra pi-gen stage: systemd-container, can-utils, NetworkManager WiFi profile |
| `pi-gen/setup.sh` | copies `stage-venus` into a pi-gen checkout |
| `host/prepare_venus_rootfs.sh` | ext4 image of the Venus build → container rootfs tarball: nspawn fixups + closed packages from the official feed |
| `host/venus_fixups.sh` | the nspawn adaptations of the Venus rootfs (idempotent, also for an installed rootfs) |
| `host/install_venus.sh` | installs unit, nspawn settings, udev rule and the rootfs tarball into a host root (`/`, pi-gen chroot, mmdebstrap tree) |
| `host/files/venus-can-profile`, `can@.service` | CAN for Venus: host sets the bitrate, script switches the Venus CAN profile ([can.md](can.md)) |
| `venus-addons/dbus-ebox-battery/` | battery service for the EBox (Pytes LFP without CAN): MQTT `ebox/pwr` → `com.victronenergy.battery.ebox` for DVCC/ESS |
| `venus-addons/batmonitor/` | battery protection and charge/discharge control of the three MultiPlus phases on two separate stacks (ESS external control, Hub4Mode 3); C port in `c/`, reference `batmonitor.py` |
| `pi-gen/stage-venus/03-venus/` | pi-gen step calling `host/install_venus.sh` with `files/venus-rootfs.tar.zst` |
| `ncr/build_ncr.sh` | x86_64 variant: Debian trixie amd64 host (mmdebstrap) for a PC; runs the same armv7 Venus rootfs via qemu-user binfmt |
| `tools/mk3_version.py` | reads the MK3-USB firmware version (MK2 protocol 'V' frame, 2400 8N1) |

## Sources

The Venus OS source is not copied into this repository; it is referenced at the exact revision that was built:

- `venus/` is a git submodule of [victronenergy/venus](https://github.com/victronenergy/venus) at tag v3.81 (`git clone --recurse-submodules`, or `git submodule update --init`).
- `venus-build/layers.lock` lists the commit of every Yocto layer the build used (bitbake, openembedded-core, meta-openembedded, meta-victronenergy, ...). The applications (dbus-systemcalc-py, gui-v2, venus-platform, flashmq, ...) are fetched by the recipes in meta-victronenergy at the revisions pinned there.
- Own code lives here: `venus-addons/` (dbus-ebox-battery, batmonitor), `host/` (nspawn integration), `pi-gen/`, `ncr/`, `tools/`.
- `venus-addons/batmonitor/c/include/` holds two unmodified third-party headers for building inside Venus, where only the shared libraries exist: `mosquitto.h` 2.0.22 (EPL-2.0 or BSD-3-Clause) and `cjson/cJSON.h` 1.7.19 (MIT).
- The 42 Victron packages without public recipe (mk2-dbus, hub4control, vrmlogger, ...) are not redistributed; `host/prepare_venus_rootfs.sh` installs them from the official feed. List, versions, license fields and functions: [nonOpenSource.md](nonOpenSource.md).

## Venus OS build

```
git clone https://github.com/victronenergy/venus.git && cd venus
git checkout v3.81
# meta-openembedded is listed with an ssh URL; fetch over https instead
GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=url.https://github.com/.insteadOf GIT_CONFIG_VALUE_0=git@github.com: make fetch
./repos checkout v3.81
cp /path/to/victron/venus-build/build_rpi4.sh .
make build/conf/bblayers.conf   # creates build/conf/local.conf
cat /path/to/victron/venus-build/local.conf.append >> build/conf/local.conf
sudo docker build -t venus-build:24.04 /path/to/victron/venus-build
# DL_DIR is ../../oe-downloads relative to build/, i.e. outside the checkout: mount it too,
# otherwise every new container loses the downloads and git-fetched recipes (dpkg) break
mkdir -p ../oe-downloads
sudo docker run -d --name venus-build -v $PWD:$PWD -v $(realpath ../oe-downloads):$(realpath ../oe-downloads) -v /path/to/victron/venus-build/gitconfig:$HOME/.gitconfig:ro -w $PWD venus-build:24.04 ./build_rpi4.sh
```

The private layer `meta-victronenergy-private` is not public. It defines `packagegroup-ve-addons` and `CORE_IMAGE_EXTRA_INSTALL`, which is how Victron's image gets its applications; without it the image lacks 146 packages of the official one. `local.conf.append` adds the ones with public recipes. The remaining closed packages exist only in the official feed of the same release (`https://updates.victronenergy.com/feeds/venus/release/packages/scarthgap/`, dirs `raspberrypi4/`, `cortexa7hf-neon-vfpv4/`, `all/`) and are installed afterwards with opkg:

`serial-starter mk2-dbus mk2vsc hub4control vecan-dbus vrmlogger vebus-system-config vebus-updater service-advertiser dbus-modbustcp dbus-mqtt-integrations dbus-parallel-bms dbus-eebus dbus-canopen-motordrive dbus-fzsonick-48tl dbus-adc dbus-ble-sensors dbus-cgwacs dbus-fronius dbus-motordrive dbus-rv-c dbus-valence gps-dbus can-bus-bms can-bus-bms-hv vedirect-interface gui start-gui-v1 mqtt-rpc venus-eeprom venus-opportunity-loads vesmart-server velib-tools support-keys dup vup xupc xupd xupt prodtest`

The feed keeps only the current release (v3.81 since 05.10.2026); build the same tag, otherwise the closed packages do not match the rest of the image.

## Venus as nspawn container

```
sudo ./host/prepare_venus_rootfs.sh ~/venus/deploy/venus/images/raspberrypi4/venus-image-large-raspberrypi4.ext4.gz venus-rootfs.tar.zst
sudo ./host/install_venus.sh / venus-rootfs.tar.zst      # on a running host, then:
sudo systemctl daemon-reload && sudo udevadm control --reload && sudo systemctl start venus
```

What has to be adapted, and why:

| Problem | Fix |
|---|---|
| nspawn refuses a tree without os-release | `venus_fixups.sh` writes `/usr/lib/os-release` |
| `--bind` on the command line replaces every `Bind=` of `venus.nspawn`, so `/data` was missing; Venus then mounts a 12 MB tmpfs on `/data/log` and reports a broken data partition | all binds (`/data`, `/etc/resolv.conf`, `/run/udev`, Victron ttys) in `venus-start` |
| a second udevd in the container fails on the host's `/run/udev` and stalls the boot | container udev disabled; host udev db bound read-only, `VE_SERVICE` set by `90-venus-serial-starter.rules` on the host |
| nobody fills `/dev/serial-starter` without udevd; serial-starter deletes each entry once it took the device | `serial-starter-devs.sh` (rcS S21) creates the entries at every boot; hotplug restarts the container |
| sysvinit starts rc with the console pty as controlling tty: when runlevel 5 ends, `svscanboot &` gets SIGHUP and all Venus services die | `setsid svscanboot` |
| sysvinit ignores the stop signal of nspawn: stop runs into the timeout, the SIGKILL leaves `/run/systemd/nspawn/unix-export/venus` behind and the next start fails | `venus-stop` halts from inside, `ExecStartPre` removes a stale mount |
| `Type=notify` never becomes ready (sysvinit) | `Type=simple` |
| journal link to `/var/log/journal` fails | `LinkJournal=no` |

Only Victron serial devices are passed into the container: serial-starter probes every tty it sees and would write into other consoles.

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

## x86 PC variant (disk boot)

The closed Victron packages (`mk2-dbus`, `hub4control`, ...) exist only for ARM, so an x86 build of Venus would lack the VE.Bus driver. Instead the x86 host runs the **same armv7 Venus rootfs** under qemu-user binfmt.

```
sudo ./ncr/build_ncr.sh authorized_keys /tmp/unused-netboot-out   # builds the chroot in /srv/netboot/venus-ncr
# turn the live chroot into a disk system
sudo chroot /srv/netboot/venus-ncr apt-get purge -y live-boot live-boot-initramfs-tools
sudo chroot /srv/netboot/venus-ncr update-initramfs -u -k all     # with /proc /sys /dev bind-mounted
# on the target PC: format a free partition, copy the tree, write fstab
mkfs.ext4 -L venusroot /dev/sdXN && mount /dev/sdXN /mnt/venus
ssh buildhost 'sudo tar -C /srv/netboot/venus-ncr --numeric-owner --xattrs --acls -cpf - . | zstd' | zstd -d | tar -C /mnt/venus --numeric-owner --xattrs --acls -xpf -
echo "UUID=<uuid> / ext4 errors=remount-ro,noatime 0 1" > /mnt/venus/etc/fstab
```

Boot it from the existing GRUB with `/etc/grub.d/43_venus`:

```
menuentry "Venus OS host (Debian trixie)" --id venus {
    insmod part_gpt
    insmod ext2
    search --no-floppy --fs-uuid --set=root <uuid>
    linux  /vmlinuz root=UUID=<uuid> ro net.ifnames=0 panic=20 console=tty0 console=ttyS0,115200n8
    initrd /initrd.img
}
```

and `GRUB_DEFAULT=venus` in `/etc/default/grub`, then `update-grub`. A GRUB default is more reliable than the UEFI BootOrder on firmware that rewrites BootOrder after POST. `/data` is a directory on the root partition and persists.

Tested on an Intel Core i3-4350T (UEFI, Secure Boot off): boots to `running`, NetworkManager DHCP on eth0, `qemu-arm` binfmt active.

## EBox battery (dbus-ebox-battery)

The EBox (2 × 15 kWh Pytes, 16S LFP, 6 modules of 100 Ah, only a serial console, no CAN) is read on the host by `ebox_mqtt.py` every 30 s (`ebox pwr` on the console) and published as JSON on `ebox/pwr`, also to the Venus broker on 127.0.0.1. `dbus-ebox-battery` (in Venus, started from `/data/rc.local`) turns it into a battery service:

| Path | Value |
|---|---|
| `/Soc`, `/Dc/0/Voltage`, `/Dc/0/Current`, `/Dc/0/Power` | from the EBox |
| `/Info/MaxChargeVoltage` | 56.6 V: charge end as before (highest pack voltage seen at 100 % SoC) |
| `/Info/MaxChargeCurrent` | 586 A (30 kW, 1C as allowed by Pytes); 10 A from a cell at 3.60 V, 0 A at 3.65 V |
| `/Info/MaxDischargeCurrent` | 586 A; 0 A below 5 % SoC until 8 % (at any time) |
| `/Info/ChargeRequest` | 1 below 5 % SoC until 8 %, **only from 11:00 to 13:00** (Europe/Berlin): hub4control switches ESS to Recharge and charges from the grid; PV surplus charges at any time |

Without data for 120 s the service exits and only registers again with fresh data, so the MultiPlus fall back to their own charge settings. In practice the three MultiPlus-II 48/5000 limit charging to about 11 kW (3 × 70 A).

## batmonitor

`venus-addons/batmonitor/` replaces the VE.Bus battery monitor (off in all three units) for a system whose phases sit on separate battery banks: Stack1 (CAN BMS) feeds L1, Stack2 (EBox) feeds L2 + L3. Only the BMS SoC counts. It runs in Venus as a daemontools service and writes `/Hub4/L1..L3/AcPowerSetpoint` in Hub4Mode 3.

- **C port** `c/batmonitor.c` (live): 5 s cycle (the Sofar PCC arrives every 4 s), about 3 MB RSS instead of 23 MB for Python. Build inside Venus with `make -C /data/batmonitor/c` (the image has gcc and the dbus headers); `install.sh` copies and builds. `batmonitor.py` stays as the reference and fallback (`BATMONITOR_PYTHON=1` in `/data/batmonitor/env`).
- Rules (1:1 from the soyo calculation of the former Waveshare ESP32): per-bank SoC protection (no discharge below 5 % until 7 %, grid charge 500 W per phase below 3 %), discharge proportional to grid import, PV surplus charging in stack priority up to the charger capacity, SoC balancing within ±3 %, heat-pump cap October to April.
- Summer charge block (May to August): a clear-sky DC model (NOAA sun position, fitted strings, tree horizon) holds PV charging back until the 20 kW peak window on forecast sunny days; clouds release it. The rest of the year every watt of surplus charges at once.
- State per cycle in `/data/batmonitor/state.json` (read by `readers/victron2db.py`). Logs are capped at 20 MB.

**PI prototype (not armed).** A velocity-form PI controller that drives PCC + Sofar Bat1 to 0 W is built in but only runs in shadow mode: every cycle it logs to `/data/batmonitor/pi_shadow.csv` what soyo sent and what the PI would have sent. `tools/compare.py` replays the PI in closed loop on the recorded disturbance and compares it with the measured soyo result (MAE/RMS, import/export Wh). `BATMONITOR_PI=1` would arm it; this is deliberately not done until enough sunny-day data shows it is better.

## MK3-USB

The MK3 microcontroller is powered from VE.Bus. Without a connected, awake MultiPlus it does not answer at all, so `tools/mk3_version.py` only works once the VE.Bus is connected.

## Status

- [x] Venus v3.80 layers fetched, Yocto build running
- [x] Raspberry Pi OS trixie base image built
- [x] x86 host (Debian trixie amd64) installed on disk and booting, qemu-arm binfmt active
- [x] Venus rootfs + closed packages + nspawn unit on the x86 host: 60 services, GUI on :80, MQTT, `mk2-dbus.ttyUSB0` started by serial-starter for the MK3
- [ ] Venus v3.81 rebuild (feed moved to v3.81), rootfs tarball
- [x] EBox SoC/limits to Venus: `dbus-ebox-battery`, tested on the x86 host with test messages (forced charge, cell taper)
- [ ] Final image with WiFi and Venus, flash Pi 4
- [ ] MK3 firmware read-out with VE.Bus connected

## Prior art

- [ehedman/victron-venus-container](https://github.com/ehedman/victron-venus-container) — official Venus rootfs in nspawn/docker/chroot
- [RafaelKa/victron-venus-os-in-docker](https://github.com/RafaelKa/victron-venus-os-in-docker) — official rootfs in Docker, aarch64
- [M-o-a-T/venusian](https://github.com/M-o-a-T/venusian) — Venus on Debian without container
