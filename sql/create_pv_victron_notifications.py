#!/usr/bin/env python3
# wagodb.pv_victron_notifications: the Venus notification list (platform/0/Notifications, last 20) kept for good,
# filled by victron2db.py every minute; dim_victron_notification_type for the type code (2026-10-20)
import os
import pymysql

DDL = [
    """CREATE TABLE IF NOT EXISTS pv_victron_notifications (
  id            int unsigned NOT NULL AUTO_INCREMENT,
  portal_id     varchar(16)  NOT NULL,
  notified_at   datetime     NOT NULL COMMENT 'Venus DateTime of the notification (local time)',
  type          tinyint unsigned NULL COMMENT 'dim_victron_notification_type',
  device_name   varchar(64)  NOT NULL DEFAULT '',
  description   varchar(128) NOT NULL DEFAULT '',
  value         varchar(64)  NULL,
  alarm_value   varchar(64)  NULL,
  service       varchar(96)  NULL,
  `trigger`     varchar(96)  NULL,
  active        tinyint(1)   NULL,
  acknowledged  tinyint(1)   NULL,
  silenced      tinyint(1)   NULL,
  first_seen    datetime     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  last_seen     datetime     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_notification (portal_id, notified_at, device_name, description),
  KEY k_notified_at (notified_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='Venus OS notifications (alarms/warnings), victron2db.py'""",
    """CREATE TABLE IF NOT EXISTS dim_victron_notification_type (
  code       tinyint unsigned NOT NULL,
  label      varchar(48)  NOT NULL,
  text       varchar(255) NOT NULL,
  quelle     varchar(255) NOT NULL DEFAULT '',
  PRIMARY KEY (code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='Venus platform/Notifications/<n>/Type'""",
]
TYPES = [
    (0, "Warning", "Warnung", "venus-platform"),
    (1, "Alarm", "Alarm", "venus-platform"),
    (2, "Info", "Information", "venus-platform"),
]

conn = pymysql.connect(read_default_file=os.path.expanduser("~/.victron2db.cnf"))
with conn.cursor() as cur:
    for d in DDL:
        cur.execute(d)
    cur.executemany("REPLACE INTO dim_victron_notification_type (code, label, text, quelle) VALUES (%s,%s,%s,%s)", TYPES)
conn.commit()
print("pv_victron_notifications, dim_victron_notification_type ok")
