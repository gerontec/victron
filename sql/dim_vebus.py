#!/usr/bin/env python3
"""
dim_vebus.py - Klartext-Tabellen (dim_*) für die Victron-Codes in wagodb (2026-10-20), zum Joinen mit pv_victron
und für alarm_mail.py (liest sie beim Start):

  dim_vebus_error          code -> text      pv_victron.vebus_error, vebus/VebusError
  dim_vebus_ip_flag        bit  -> text      vebus/Devices/<n>/InterfaceProtectionLog/<i>/ErrorFlags (NA-Schutz)
  dim_vebus_state          code -> text      pv_victron.vebus_state, pv_victron.system_state
  dim_vebus_mode           code -> text      pv_victron.vebus_mode
  dim_vebus_active_input   code -> text      pv_victron.active_input
  dim_vebus_charge_state   code -> text      pv_victron.charge_state (VebusChargeState)

Quellen: VE.Bus-Fehler https://www.victronenergy.com/live/ve.bus:ve.bus_error_codes (offiziell);
NA-Schutz-Bits: Victron hat sie nicht dokumentiert (auch nicht im MK2-Protokoll 3.14), Bezeichnungen aus der Community
(community.victronenergy.com/t/60176) - Spalte quelle.
Beispiele:
  SELECT v.ts, s.text AS state, e.text AS fehler FROM pv_victron v
    LEFT JOIN dim_vebus_state s ON s.code = v.vebus_state LEFT JOIN dim_vebus_error e ON e.code = v.vebus_error;
  SELECT GROUP_CONCAT(text SEPARATOR ', ') FROM dim_vebus_ip_flag WHERE (68 & bit_value) <> 0;
Idempotent: CREATE TABLE IF NOT EXISTS + REPLACE; erneut ausführen nach Änderungen an den Listen.
"""
import os
import pymysql

def code_table(name, comment):
    return """CREATE TABLE IF NOT EXISTS %s (
  code       smallint unsigned NOT NULL,
  label      varchar(48)  NOT NULL,
  text       varchar(255) NOT NULL,
  quelle     varchar(255) NOT NULL DEFAULT '',
  PRIMARY KEY (code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='%s'""" % (name, comment)

SRC_ERR = "victronenergy.com/live/ve.bus:ve.bus_error_codes"
SRC_IP = "inoffiziell: Victron Community t/60176 (Skript cmock); Victron dokumentiert die Bits nicht"
SRC_DBUS = "Venus OS dbus (com.victronenergy.vebus)"

TABLES = {
    "dim_vebus_error": ("VE.Bus-Fehlernummern (VebusError)", "code", [
        (0, "OK", "kein Fehler", SRC_ERR),
        (1, "PhaseOff", "Gerät aus, weil eine andere Phase im System abgeschaltet hat", SRC_ERR),
        (2, "MixedMk2", "neue und alte MK2-Protokolltypen im System gemischt", SRC_ERR),
        (3, "DeviceCount", "nicht alle (oder mehr als die) erwarteten Geräte im System gefunden", SRC_ERR),
        (4, "NoOtherDevice", "kein anderes Gerät gefunden", SRC_ERR),
        (5, "AcOutOvervoltage", "Überspannung an AC-out", SRC_ERR),
        (6, "DdcProgram", "Fehler im DDC-Programm", SRC_ERR),
        (7, "ExternalDevice", "externes Gerät angeschlossen, Assistent erforderlich", SRC_ERR),
        (8, "GroundRelay", "Test des Erdungsrelais fehlgeschlagen", SRC_ERR),
        (10, "TimeSync", "Problem bei der Zeitsynchronisation im System (meist Folge eines Neustarts, eigentliche "
             "Ursache oft Fehler 1)", SRC_ERR),
        (11, "RelayTest", "Relaistest fehlgeschlagen: Installationsfehler oder Relaisdefekt", SRC_ERR),
        (12, "ConfigMismatch2ndMcu", "Konfiguration passt nicht zur zweiten MCU", SRC_ERR),
        (14, "CannotTransmit", "Gerät kann keine Daten senden", SRC_ERR),
        (15, "Combination", "VE.Bus-Kombinationsfehler", SRC_ERR),
        (16, "DongleMissing", "VE.Bus-Dongle fehlt", SRC_ERR),
        (17, "PhaseMasterMissing", "Phasen-Master fehlt", SRC_ERR),
        (18, "SlaveAcOvervoltage", "AC-Überspannung am Ausgang eines ausgeschalteten Slaves", SRC_ERR),
        (19, "SlaveNoAcIn", "Slave hat keinen AC-Eingang", SRC_ERR),
        (20, "ConfigMismatch", "Konfiguration passt nicht zusammen", SRC_ERR),
        (22, "CannotBeSlave", "Gerät kann nicht als Slave arbeiten", SRC_ERR),
        (24, "SwitchoverProtection", "Schutz des Umschaltsystems ausgelöst (Rückstrom über das Rückspeiserelais)",
         SRC_ERR),
        (25, "IncompatibleConfig", "inkompatible VE.Bus-Konfiguration", SRC_ERR),
        (26, "Internal", "interner Fehler", SRC_ERR),
        (30, "Ghost30", "Geisterfehler (Kommunikation)", SRC_ERR),
        (31, "Ghost31", "Geisterfehler (Kommunikation)", SRC_ERR),
        (128, "RelayTestBlocked", "Netzzuschaltung verhindert: Relaistest schlägt länger als 5 min fehl", SRC_ERR),
    ]),
    "dim_vebus_ip_flag": ("Bits InterfaceProtectionLog ErrorFlags (NA-Schutz MultiPlus), Join: (flags & code) <> 0",
                          "code", [
        (1, "RoCoF", "NA-Schutz: Frequenzänderung df/dt zu schnell (RoCoF, Inselnetzerkennung)", SRC_IP),
        (2, "Bit2", "NA-Schutz: Bit 2, Bedeutung unbekannt", "nicht dokumentiert"),
        (4, "FreqLow", "NA-Schutz: Netzfrequenz zu niedrig (f<)", SRC_IP),
        (8, "VoltageLow", "NA-Schutz: Netzspannung zu niedrig (U<)", SRC_IP),
        (16, "VoltageHigh", "NA-Schutz: Netzspannung zu hoch (U>)", SRC_IP),
        (32, "Bit32", "NA-Schutz: Bit 32, Bedeutung unbekannt", "nicht dokumentiert"),
        (64, "PhaseIssue", "NA-Schutz: Phasenproblem (Phasenlage/PLL verloren)", SRC_IP),
        (128, "Bit128", "NA-Schutz: Bit 128, Bedeutung unbekannt", "nicht dokumentiert"),
    ]),
    "dim_vebus_state": ("VE.Bus /State und system /SystemState/State", "code", [
        (0, "Off", "aus", SRC_DBUS), (1, "LowPower", "Sparmodus (Low power)", SRC_DBUS),
        (2, "Fault", "Fehler", SRC_DBUS), (3, "Bulk", "Laden: Bulk", SRC_DBUS),
        (4, "Absorption", "Laden: Absorption", SRC_DBUS), (5, "Float", "Laden: Float", SRC_DBUS),
        (6, "Storage", "Laden: Storage", SRC_DBUS), (7, "Equalize", "Laden: Ausgleichsladung", SRC_DBUS),
        (8, "Passthru", "Durchleitung (Netz an AC-out)", SRC_DBUS),
        (9, "Inverting", "Wechselrichter (Inselbetrieb, Netz getrennt)", SRC_DBUS),
        (10, "PowerAssist", "Power Assist", SRC_DBUS), (11, "PowerSupply", "Netzteilbetrieb", SRC_DBUS),
        (244, "Sustain", "Sustain (Batterie leer, Erhaltungsladung)", SRC_DBUS),
        (252, "ExternalControl", "externe Steuerung (ESS)", SRC_DBUS),
        (256, "Discharging", "Entladen (System)", SRC_DBUS), (257, "SystemSustain", "Sustain (System)", SRC_DBUS),
        (258, "Recharge", "Nachladen (System)", SRC_DBUS),
        (259, "ScheduledRecharge", "geplantes Nachladen (System)", SRC_DBUS),
    ]),
    "dim_vebus_mode": ("VE.Bus /Mode", "code", [
        (1, "ChargerOnly", "nur Ladegerät", SRC_DBUS), (2, "InverterOnly", "nur Wechselrichter", SRC_DBUS),
        (3, "On", "ein", SRC_DBUS), (4, "Off", "aus", SRC_DBUS),
    ]),
    "dim_vebus_active_input": ("VE.Bus /Ac/ActiveIn/ActiveInput", "code", [
        (0, "AcIn1", "AC-Eingang 1 (Netz verbunden)", SRC_DBUS), (1, "AcIn2", "AC-Eingang 2", SRC_DBUS),
        (240, "Disconnected", "kein AC-Eingang: Netzrelais offen", SRC_DBUS),
    ]),
    "dim_vebus_charge_state": ("VE.Bus /VebusChargeState", "code", [
        (0, "Init", "Initialisierung", SRC_DBUS), (1, "Bulk", "Bulk", SRC_DBUS),
        (2, "Absorption", "Absorption", SRC_DBUS), (3, "Float", "Float", SRC_DBUS),
        (4, "Storage", "Storage", SRC_DBUS), (5, "RepeatedAbsorption", "wiederholte Absorption", SRC_DBUS),
        (6, "ForcedAbsorption", "erzwungene Absorption", SRC_DBUS), (7, "Equalise", "Ausgleichsladung", SRC_DBUS),
        (8, "BulkStopped", "Bulk gestoppt", SRC_DBUS),
    ]),
}

conn = pymysql.connect(read_default_file=os.path.expanduser("~/.victron2db.cnf"), autocommit=False)
with conn.cursor() as cur:
    for name, (comment, _, rows) in TABLES.items():
        cur.execute(code_table(name, comment))
        cur.executemany("REPLACE INTO %s (code, label, text, quelle) VALUES (%%s,%%s,%%s,%%s)" % name, rows)
conn.commit()
with conn.cursor() as cur:
    for name in TABLES:
        cur.execute("SELECT COUNT(*) FROM %s" % name)
        print("%-24s %3d Zeilen" % (name, cur.fetchone()[0]))
    for flags in (8, 9, 65, 4, 68):
        cur.execute("SELECT GROUP_CONCAT(label ORDER BY code SEPARATOR '+') FROM dim_vebus_ip_flag "
                    "WHERE (%s & code) <> 0", (flags,))
        print("ip_flags %3d = %s" % (flags, cur.fetchone()[0]))
