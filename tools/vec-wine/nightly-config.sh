#!/bin/sh
# Nightly VE.Bus configuration snapshot: mk2vsc backup -> ~/vebus-backup/nightly_<date>.rvms -> wagodb.dim_config.
# Skipped while VEConfigure owns the MK3 (no Venus vebus service). Keeps the last 30 nightly files.
N=nightly_$(date +%Y%m%d)
L=$(sudo machinectl show venus -p Leader --value)
sudo nsenter -t "$L" -a dbus -y | grep -q "^com.victronenergy.vebus\." || { echo "$(date "+%F %T") skip: no vebus service"; exit 0; }
sh "$HOME/vec/vebus-backup.sh" "$N" || exit 1
python3 "$HOME/python/rvms2db.py" "$HOME/vebus-backup/$N.rvms"
ls -1t "$HOME"/vebus-backup/nightly_*.rvms 2>/dev/null | tail -n +31 | xargs -r rm -f
