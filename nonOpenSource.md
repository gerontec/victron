# Non-open-source packages of Venus OS

The official Venus OS image contains 42 packages that **cannot be built from the public Yocto layers**: their recipes live in Victron's private layer `meta-victronenergy-private`. Some of them are proprietary without public source (the VE.Bus driver, the ESS control loop, the VRM logger, the firmware updaters); for a few the source is public on GitHub, only the recipe is missing.

This build installs all of them as prebuilt binaries from the official feed of the same release (`host/prepare_venus_rootfs.sh`):

```
https://updates.victronenergy.com/feeds/venus/release/packages/scarthgap/{all,cortexa7hf-neon-vfpv4,raspberrypi4}
```

They are not redistributed in this repository. The feed only carries the current release, so the image must be built from the same tag (here v3.81).

## Columns

- **Version**: in the v3.81 feed (07.10.2026)
- **License field**: the `License:` entry of the feed package. Most private recipes say `MIT`, which is a recipe default and **not** a statement that the source is public. Only six are explicitly marked `CLOSED`.
- **Public repo**: whether `github.com/victronenergy/<name>` exists and is public (checked 07.10.2026). "–" means no public repository of that name was found; one under another name is possible.
- Functions marked \* are inferred from the name; the feed description is only "<name> version <x>".

## Essential for this system (MultiPlus-II, VE.Bus, ESS)

| Package | Version | License field | Public repo | Function |
|---|---|---|---|---|
| mk2-dbus | 4.01 | MIT | – | VE.Bus driver: talks to the MultiPlus through the MK2/MK3 interface, publishes `com.victronenergy.vebus` |
| hub4control | 1.4.2 | MIT | – | ESS control loop ("controls the hub-4 behaviour of a Multi/Quattro"): grid setpoint, charge/discharge, DVCC limits, Recharge on battery `ChargeRequest` |
| serial-starter | 0.0 | MIT | – | detects serial devices and starts the matching driver (`mk2-dbus` for an MK3) |
| vebus-system-config | 1.10 | MIT | – | configuration of a VE.Bus system (several units, phases) |
| mk2vsc | 1.33 | MIT | – | reads/writes MultiPlus settings files, base of Remote VEConfigure via VRM |
| vebus-updater | 1.19 | MIT | – | firmware update of VE.Bus devices |

## VRM portal and remote access

| Package | Version | License field | Public repo | Function |
|---|---|---|---|---|
| vrmlogger | 2.415 | MIT | – | sends data to the VRM portal |
| mqtt-rpc | 1.131 | MIT | – | remote procedure calls over MQTT (used by VRM)\* |
| support-keys | 1.0 | MIT | – | ssh keys for Victron support access\* |
| service-advertiser | 0.3 | MIT | – | advertises the Venus services on the local network\* |

## Interfaces and protocols

| Package | Version | License field | Public repo | Function |
|---|---|---|---|---|
| dbus-modbustcp | 1.0.103 | MIT | – | Modbus-TCP server (port 502) |
| dbus-mqtt-integrations | 1.6.1 | **CLOSED** | – | EV chargers and EVs over MQTT |
| dbus-eebus | 0.9 | **CLOSED** & MIT | – | EEBUS (smart grid / EV charging) |
| vecan-dbus | 3.91 | MIT | – | VE.Can / NMEA 2000 |
| can-bus-bms | 0.72 | MIT | – | batteries with CAN BMS (low voltage) |
| can-bus-bms-hv | 0.04 | MIT | – | batteries with CAN BMS (high voltage) |
| dbus-parallel-bms | 1.1.6 | **CLOSED** | – | combines several BMS into one battery |
| dbus-rv-c | 1.11 | MIT | – | RV-C bus (motorhomes) |
| vesmart-server | 0.5.17 | MIT | – | VE.Smart networking (data sharing between Bluetooth devices)\* |

## Drivers for other devices (not needed here)

| Package | Version | License field | Public repo | Function |
|---|---|---|---|---|
| vedirect-interface | 3.92 | MIT | – | VE.Direct devices (MPPT, BMV, ...) |
| dbus-fronius | 1.7.28 | MIT | public | PV inverters (Fronius, SunSpec) |
| dbus-cgwacs | 2.0.31 | MIT | public | Carlo Gavazzi energy meters (EM24, ET340, ...) |
| dbus-adc | 1.49 | MIT | public | analog tank and temperature inputs |
| dbus-ble-sensors | 0.35 | MIT | public | Bluetooth sensors (Ruuvi, Mopeka, ...) |
| gps-dbus | 1.11 | MIT | – | GPS receivers |
| dbus-motordrive | 1.11 | MIT | – | electric propulsion motors |
| dbus-canopen-motordrive | 1.14 | MIT | public | electric propulsion motors over CANopen |
| dbus-fzsonick-48tl | 3.0.4 | MIT | – | FZSoNick 48TL batteries |
| dbus-valence | 1.14 | MIT | – | Valence batteries |

## User interface and system

| Package | Version | License field | Public repo | Function |
|---|---|---|---|---|
| gui | 6.9.6 | MIT | – | the old user interface (GUI v1); GUI v2 is built here from public source |
| start-gui-v1 | 6.9.6 | MIT | – | starts GUI v1 |
| venus-opportunity-loads | 0.1.4 | MIT | – | switches loads on surplus\* |
| venus-eeprom | 1.0 | MIT | – | device EEPROM data (serial, hardware info)\* |
| velib-tools | 0.12 | MIT | – | Victron library tools\* |
| dup | 1.0.28 | MIT | – | firmware update tool\* |
| vup | 2.1.8 | MIT | – | firmware update tool\* |
| xupc | 1.0.6 | **CLOSED** | – | "Victron VE.Can Xup Updater" |
| xupd | 1.0.15 | **CLOSED** | – | "Victron VE.Direct Xup Updater" |
| xupt | 1.0.4 | **CLOSED** | – | "Victron Ethernet Xup Updater" |
| prodtest | 0.0 | MIT | – | production test\* |

## Built here from public source

Everything else of the image: the base system (openembedded-core), GUI v2 (`gui-v2-webassembly`), `venus-platform`, `flashmq` + `dbus-flashmq` (MQTT), `dbus-systemcalc-py`, `dbus-modbus-client`, `dbus-digitalinputs`, `dbus-switch`, Node-RED, ... (see `venus-build/local.conf.append` and `venus-build/layers.lock`), plus the own add-on `venus-addons/dbus-ebox-battery`.
