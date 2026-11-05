#!/bin/sh
# Nightly VE.Bus configuration snapshot: mk2vsc backup -> ~/vebus-backup/nightly_<date>.rvms -> wagodb.dim_config.
# Skipped while VEConfigure owns the MK3 (no Venus vebus service).
# Files (user 2026-11-05: "ich moechte immer nur diese regel also zwei files sehen"): after a successful backup only the
# oldest and the newest .rvms stay, in ~/vebus-backup and in /data/vebus-backup (the Venus copy); the worst case state
# for a restore is archived in wagodb.vebus_config_backup (readers/rvms_archive.py).
N=nightly_$(date +%Y%m%d)
L=$(sudo machinectl show venus -p Leader --value)
sudo nsenter -t "$L" -a dbus -y | grep -q "^com.victronenergy.vebus\." || { echo "$(date "+%F %T") skip: no vebus service"; exit 0; }
sh "$HOME/vec/vebus-backup.sh" "$N" || exit 1
python3 "$HOME/python/rvms2db.py" "$HOME/vebus-backup/$N.rvms"
# keep the oldest and the newest file (by mtime), delete the ones in between; nothing to do with 2 files or fewer
keep2() { ls -1t "$1"/*.rvms 2>/dev/null | sed '1d;$d' | while read -r f; do sudo rm -f -- "$f" && echo "removed $f"; done; }
keep2 "$HOME/vebus-backup"
keep2 /data/vebus-backup
