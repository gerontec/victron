# CAN for the Venus OS subsystem

A battery BMS with CAN is read by Venus OS (the GX side), not by the MultiPlus: the MultiPlus-II has no CAN port, its two RJ45 sockets are VE.Bus. Do not plug a CAN cable into them. The chain is

```
BMS --CAN--> host can0 (MCP2515) --> Venus can-bus-bms --> DVCC --VE.Bus/MK3--> MultiPlus
```

## Hardware on the Pi 4

MCP2515 CAN controller (Waveshare RS485 CAN HAT), **12 MHz crystal**, interrupt on GPIO25. In `/boot/firmware/config.txt`:

```
[all]
dtparam=spi=on
dtoverlay=mcp2515-can0,oscillator=12000000,interrupt=25,spimaxfrequency=1000000
```

The HAT exists with 8 MHz and 12 MHz crystals; read the marking on the crystal. With the wrong `oscillator` every bitrate is off by the ratio (12/8: "500k" is really 750k). The symptom is a flood of garbage frames with random IDs and error counters rising, often repeating stale bytes of the MCP2515 receive buffers, at every configured bitrate. A quick check without reboot: with `oscillator=8000000` on a 12 MHz board, `bitrate 333333` gives a real 500 kbit/s.

After a reboot `dmesg` shows `mcp251x spi0.0 can0: MCP2515 successfully initialized.` Bus wiring: CAN-H, CAN-L, GND, 120 Ω termination at both ends of the bus.

With a standard patch cable (T568A/B) on a Pylontech-style BMS port, pin 4 (blue) is CAN-H and pin 5 (white-blue) is CAN-L. Swapped H/L gives no frames at all.

## Two batteries

The system has two 15 kWh LFP storages in parallel on the same 48 V bus:

| | Speicher A | Speicher B |
|---|---|---|
| Link | CAN-bus BMS on can0 (500k, frames 0x351/355/356/35A/35E/373-377) | EBox console via `ebox_mqtt.py` -> MQTT `ebox/pwr` |
| Venus service | `com.victronenergy.battery.socketcan_can0`, instance 512 | `com.victronenergy.battery.ebox` (dbus-ebox-battery), instance 513 |
| wagodb.pv_victron | `a_*` columns | `soc`, `bat_*`, `cell_*`, `cvl_v`, `ccl_a`, `dcl_a` |

Both instances must differ, otherwise the MQTT topics `N/<portal>/battery/<instance>/...` mix. Battery monitor and DVCC BMS are pinned to Speicher B (`/Settings/SystemSetup/BatteryService`, `/Settings/SystemSetup/BmsInstance` in `host/venus-settings.conf`): on "Automatic" Venus picks Speicher A, whose limits (57.6 V, 100 A) do not protect Speicher B. Neither service alone sees the full system current.


## Why a host script

Venus OS picks the services and the bitrate of a CAN port from the setting `/Settings/Canbus/<if>/Profile` (venus-platform, `CanBusProfiles` in [veutil](https://github.com/victronenergy/veutil) `src/qt/canbus_interfaces.cpp`). On a profile change it runs `can-set-rate <if> <bitrate>`. Inside the nspawn container this fails: the container shares the host network namespace but has no `CAP_NET_ADMIN`, so it can use `can0` but not reconfigure it. The services still start, at whatever bitrate the host has set. The host therefore owns the bitrate:

- `can@.service` (template, `can@can0.service` enabled) brings the interface up at boot with `BITRATE` from `/etc/venus-can/<if>.conf` (default 500000).
- `venus-can-profile` sets the host bitrate, stores it in that file and then writes the profile to Venus.

Both are installed by `host/install_venus.sh` (`/usr/local/sbin/venus-can-profile`, link in `/usr/local/bin`, `/etc/systemd/system/can@.service`).

## venus-can-profile

```
venus-can-profile                 # show: host bitrate, Venus profile, running Venus CAN services
venus-can-profile bms             # CAN-bus BMS LV, 500 kbit/s
venus-can-profile vecan can1      # other interface
```

Runs itself with sudo. Profiles (number = value of `/Settings/Canbus/<if>/Profile`):

| Name | Profile | Bitrate | Venus services |
|---|---|---|---|
| `off` | 0 | interface down | none |
| `vecan` | 1 | 250 kbit/s | vecan-dbus (VE.Can, NMEA 2000); Venus default |
| `vecan-bms` | 2 | 250 kbit/s | vecan-dbus + can-bus-bms |
| `bms` | 3 | 500 kbit/s | can-bus-bms (CAN-bus BMS LV) |
| `hv` | 7 | 500 kbit/s | can-bus-bms-hv (high-voltage BMS) |
| `listen` | 8 | 500 kbit/s | none: interface up for `candump` |

Not offered by the script (other Venus profiles): 4 Oceanvolt (vecan + dbus-valence + dbus-motordrive, 250k), 5 interface only at 250k, 6 RV-C (250k), 9 CANopen motor drive + vecan (250k), 10 CANopen motor drive (500k).

Example output:

```
host:  bitrate 500000
venus: profile 3
       service can-bus-bms.can0
```

## Which BMS Venus understands

`can-bus-bms` reads the common "CAN-bus BMS" protocol at 500 kbit/s (Pylontech, BYD, Pytes and many others; frames 0x351 charge voltage and current limits, 0x355 SoC/SoH, 0x356 voltage/current/temperature, 0x35A alarms and warnings, 0x35E name). A BMS with another protocol is not understood; check with

```
venus-can-profile listen
candump -t a can0
```

which IDs it sends. Some BMS only talk after an inverter heartbeat. If the protocol differs, `venus-addons/dbus-ebox-battery` can be extended to translate the frames into the battery service itself.

Note: profile `vecan` transmits VE.Can frames (address claims). Switch to `listen` or `bms` before connecting an unknown BMS.

## Commissioned settings: host/venus-settings.sh

`sudo host/venus-settings.sh apply` writes everything above on the Pi, idempotent: CAN overlay in `config.txt` (asks for a reboot and a second run if it changed), `venus-can-profile bms`, dbus-ebox-battery, victron2db (script, cron, `sql/pv_victron_speicher_a.sql`) and the Venus settings listed in `host/venus-settings.conf`. After changing settings in Venus during commissioning, `sudo host/venus-settings.sh save [extra path ...]` reads them back into the conf file; commit it.
