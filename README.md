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
| `web/victron_api.php` | JSON/Markdown API over `wagodb.pv_victron` (latest row + 24 h SoC range), data source of the FUXA view |
| `web/fuxa_victron_project.py` | generates the FUXA project of venus.heissa.de (WebAPI device on `victron_api.php`, one view: grid/ATS, Pi, three MultiPlus, both stacks incl. Pytes pack cycles): `python3 web/fuxa_victron_project.py victron_api.json > project.json` |
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
| `/Info/ChargeRequest` | 1 below 5 % SoC until 8 %, at any time (the 11-13 h window was removed 2026-10-11): hub4control switches ESS to Recharge and charges from the grid; PV surplus charges at any time |

Without data for 120 s the service exits and only registers again with fresh data, so the MultiPlus fall back to their own charge settings. In practice the three MultiPlus-II 48/5000 limit charging to about 11 kW (3 × 70 A).

## batmonitor

`venus-addons/batmonitor/` replaces the VE.Bus battery monitor (off in all three units) for a system whose phases sit on separate battery banks: Stack1 (CAN BMS) feeds L1, Stack2 (EBox) feeds L2 + L3. Only the BMS SoC counts. It runs in Venus as a daemontools service and writes `/Hub4/L1..L3/AcPowerSetpoint` in Hub4Mode 3.

- **C port** `c/batmonitor.c` (live): 5 s cycle (the Sofar PCC arrives every 4 s), about 3 MB RSS instead of 23 MB for Python. Build inside Venus with `make -C /data/batmonitor/c` (the image has gcc and the dbus headers); `install.sh` copies and builds. `batmonitor.py` stays as the reference and fallback (`BATMONITOR_PYTHON=1` in `/data/batmonitor/env`).
- Rules (1:1 from the soyo calculation of the former Waveshare ESP32): per-bank SoC protection (no discharge below 5 % until 7 %, grid charge 500 W per phase below 3 %), discharge proportional to grid import, PV surplus charging in stack priority up to the charger capacity, SoC balancing within ±3 %, heat-pump cap October to April.
- Discharge split (0.26-c): every battery bank delivers the same power, so the two 300 Ah stacks drain alike. Stack1 (MUST) feeds only L1, Stack2 (Pytes) feeds L2 + L3, hence L1 50 %, L2 25 %, L3 25 % (Unit 2 discharges twice as much as Unit 1 / Unit 3). Each phase is capped at `DISCHARGE_MAX_PHASE` = 3800 W (0.34-c): 4000 W is the MultiPlus-II 48/5000 limit but not its best operating point. Above 7.6 kW total, L1 stays at 3800 W and the rest goes to L2/L3; Stack2 then drains faster, and the SoC balancing (one stack more than 3 % ahead discharges alone until the gap is below 1 %) catches up later.
- BMS current limits (0.35-c): batmonitor reads `/Info/MaxChargeCurrent` and `/Info/MaxDischargeCurrent` of each battery service and caps the phases of that stack at CCL x U / `CHARGE_EFF` (charge) and DCL x U x `CHARGE_EFF` (discharge), split over the stack's phases (U = BMS voltage, 51.2 V without one). Needed for the MUST CAN BMS: DVCC only follows the active BMS (the EBox, 586 A), so the MUST's CCL (100 A, tapering 45/40/35/25/20/15/0 A from 91 % SoC) never reached the L1 MultiPlus and up to 61 A above it were measured. The rule gets `|CCL` / `|DCL` when the BMS limit cut the setpoint; `state.json` shows `bms_limits`, `charge_cap_w`, `discharge_cap_w`. CCL 0 = L1 does not charge, the surplus goes to Stack2.
- AC-out1 load (0.36-c): batmonitor reads `/Ac/Out/L1..L3/P` of the VE.Bus system and writes it to `state.json` (`ac_out_w`), not yet used by the logic. Planned once the critical loads (computers, phone) hang on AC-out1: the setpoint applies at AC-in, so an AC-out1 load is served from the battery even at setpoint 0; protection/idle/force charge will then add the AC-out1 load to the setpoint, and the own charge becomes AC-in − AC-out. With nothing connected the units report 5–30 W, hence a ~30 W dead band.
- **Grid-serving charging (netzdienliche Ladestrategie) by charging in the peak window** (0.49-c, D4 `bm_d4`): from May to August, while the forecast (clear-sky DC model with NOAA sun position, fitted strings and tree horizon, x OWM 3 h cloud slots x forecast.py correction, anchored to the measured PV) still expects export above `EXPORT_CAP` (18 kW) today, the stacks do not fill up in the morning: they wait below the cap (`LADESPERRE`) and charge only the export above it (charge target PCC = +18 kW). Their capacity is thus used to shave the midday/afternoon feed-in peaks instead of being full before noon; summer 2026 without it the 20 kW export limit was hit on 55 days (480 DO4 curtailment pulses, 11:45-16:15). Only if the export still stays above 20 kW for `DO4_GRACE_S` (after the R290 boost and the attic air conditioning had their chance) is the FoxESS curtailed (DO4). Outside the window and the rest of the year every watt of surplus charges at once. Chart: `docs/charge_flow.pdf`.
- **Full-charge forecast `full_at` per stack from real weather data** (0.28-c ff.): the time each stack reaches 100 % (shown on venus.heissa.de, e.g. "full 15:24") is computed every cycle from an internet weather forecast, not from a fixed profile. Chain: `save_weather.py` on heissa fetches the OpenWeatherMap 5-day / 3-hour forecast every 30 min into `wagodb.weather_data`; `readers/forecast.py` on the Sofar host (hourly at :10) turns every daytime 3 h slot of today and tomorrow into a clear-sky index kt (regression on cloudiness, rain, rain probability, temperature, humidity, visibility, wind) and publishes `kt_slots` plus a `correction` (measured PV / model x kt over the last complete days) as retained MQTT `batmonitor/forecast`; batmonitor (`bm_full_forecast`) steps from now to sunset in 5 min: PV = clear-sky model x kt of the slot x correction, anchored to the measured Sofar PV (the ratio fades out over 1.5 h), split over the stacks like the real charge allocation, at most `CHARGER_DC_W` (3.6 kW DC) per MultiPlus. `state.json` shows `forecast.method` = `weather` (OWM slots used) or `months` (fallback: monthly mean kt, when `batmonitor/forecast` is older than 3 h, e.g. because forecast.py publishes nothing while the weather data does not reach 84 h ahead, i.e. the OWM import stalled).
- State per cycle in `/data/batmonitor/state.json` (read by `readers/victron2db.py`). Logs are capped at 20 MB.
- Control logic in `c/bm_logic.c` as a pure function (`bm_step`), tested on any host with a plant model: `make -C c test`.
- Parameters with defaults and allowed ranges in `BM_PARAMS` (`c/bm_logic.c`), overridable in `/data/batmonitor/env` as
  `BATMONITOR_<NAME>=value` (out-of-range values are ignored and logged); the effective list is logged at start:

  | Parameter | Default | Meaning |
  |---|---|---|
  | `W_MAX` | 10000 W | total discharge |
  | `DISCHARGE_MAX_PHASE` | 3800 W | discharge per phase (since 0.34-c; 4000 W is the MP2 limit, not its best operating point) |
  | `CHARGE_MAX_PHASE` / `CHARGER_CAP_PHASE` | 4900 / 4200 W | PV charge per phase / real charger capacity |
  | `SOC_MIN` / `SOC_MIN_RELEASE` | 5 / 7 % | discharge protection (Lenggries env since 2026-10-11: 8 / 10 %) |
  | `SOC_FORCE` / `SOC_FORCE_RELEASE` / `FORCE_CHARGE_W` | 3 / 5 % / 500 W | grid force charge per phase (Lenggries env since 2026-10-24: `SOC_FORCE=0` = off, see below) |
  | `DCL_FORCE_A` | 1 A | LOW force charge while the BMS discharge limit is below this, no discharge (0 = off; 0.52-c) |
  | `CELL_FORCE_V` / `CELL_FORCE_RELEASE_V` | 3.00 / 3.25 V | LOW force charge while the lowest cell is below this; ends when DCL is back and the cell has reached the release value (0.52-c; since 0.55-c LOW also waits for `SOC_MIN_RELEASE`, otherwise the cell recovers within minutes under the force charge current and LOW and discharge take turns; Lenggries env since 2026-10-24: `CELL_FORCE_V=3.05`, so batmonitor stops before the 49 V ESS cut-off, where the weakest MUST cell was already at 2.997 V) |
  | `WP_WINTER_W` | 1000 W | winter half-year (October..March) with the heat pump running (>= 300 W): the stacks discharge 0 W while the Sofar Bat1 discharges (< -100 W), else at most this; rule text `WPMAX(W)`, state.json `wp_max_w` (-1 = not active); heat pump off or summer half-year: PCC near 0; charging unchanged (0.61-c, replaces `WP_SOC_MIN`) |
  | `SOC_BALANCE_ON` / `SOC_BALANCE_OFF` | 3 / 1 % | SoC balancing between the stacks |
  | `SOYO_TARGET` | 0 W | PCC target |
  | `B_NIGHT` | 936 W | night base discharge |
- **PCC near 0 in every season** (0.59-c, 2026-10-24): the stacks charge and discharge on the real PCC; the heat pump is
  house load like any other. Removed: the Z2 rules (winter: the grid pays the heat pump, 0.19 / 0.30-c), the transition
  cap (1900 W), the forecast rule with its bad-forecast cap, WPPV and the `WP_CAP` fallback. Reason: the heat pump tariff
  (22 ct) saves only 8 ct/kWh against the house (30 ct), less than the ~15-20 % charge + discharge loss of storing PV
  while the grid pays the heat pump, and the Sofar Bat1 covered the heat pump instead of the grid anyway. 0.61-c
  (2026-10-25): `WP_SOC_MIN` replaced by the winter heat pump rule `WP_WINTER_W` (see the table): supporting the 22 ct
  heat pump and charging back from the grid at 30 ct loses money. The season (`readers/season.py`) is only shown in `state.json`.
- Several days without sun (Lenggries settings, 2026-10-24): there is no time-based force charge (the TOU window was
  removed on 2026-10-11), so the stacks are simply parked:
  1. The stacks discharge down to `SOC_MIN` (8 %) and then stop; from there the house runs on the grid.
  2. At 8 % the SoC only falls by standby consumption and self-discharge. The SoC force charge (`SOC_FORCE` 3 %)
     would practically never have started, so it is switched off (`SOC_FORCE=0`, 2026-10-24); the LOW stage covers it.
  3. The LOW stage comes earlier:
     - Pytes (EBox): the BMS discharge limit (DCL) drops to 0 below 5 % SoC, which starts the LOW force charge.
     - MUST (CAN BMS): on 2026-10-11 the BMS sent DCL 0 already at 10 % SoC and a 3.0 V cell; the `CELL_FORCE_V`
       limit (3.00 V) now protects against that.
  4. Victron as the last stage: if L1 falls to the ESS dynamic cut-off (49 V), the ESS assistant then keeps the
     battery at the sustain voltage (51 V) and charges from the grid with a small current to do so
     (see `tools/vec-wine/README.md`, "Pytes values in the ESS assistant").
  The low-battery pre-alarm of the units sits at cut-off + restart offset = 50.2 V: under a heavy load on L1
  (above about 100 A from the MUST stack) it shows a warning at 12-13 % SoC while discharging continues.
- Venus ESS fallback aligned with batmonitor (2026-10-24): `/Settings/CGwacs/BatteryLife/State` = 10 (Optimized
  without BatteryLife) and `MinimumSocLimit` = 8 %. batmonitor runs ESS in Hub4Mode 3, where these do not act; if
  ESS ever falls back to Victron's own control (Hub4Mode 1, as on 2026-10-11) it now uses the same 8 % limit
  instead of BatteryLife's 10/15 %.

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
- [victron-venus/inverter-control](https://github.com/victron-venus/inverter-control) — external ESS control daemon on Venus OS (writes `/Hub4/L1/AcPowerSetpoint` via D-Bus): grid-zero, split-phase compensation, solar forecast, tariffs; one battery, no per-stack split like batmonitor
