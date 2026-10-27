#!/bin/sh
# Harden a Venus OS rootfs against write access from Victron / VRM. Idempotent.
# Usage: venus_harden.sh <venus rootfs dir>
#
# Goal (user 2026-10-27): the system reports to VRM (dashboard, live values), but nothing from outside writes to it,
# also not through a backdoor or the firmware automation. Found in the v3.81 sources / image:
#   - venus-platform writes /data/conf/flashmq.d/vrm_bridge.conf from two templates in /usr/share/flashmq:
#     GXdbus (publish N, subscribe R, W, I/in) and, only in VRM mode Full, GXrpc (subscribe P/in = remote commands).
#     Read-only keeps the same GXdbus bridge with "subscribe W"; only an ACL in the dbus-flashmq plugin rejects the
#     writes, and that ACL follows the setting /Settings/Network/VrmPortal.
#   - mqtt-rpc executes the P/in commands: GX firmware (swupdate), MultiPlus firmware and VE.Configure settings
#     (vebusupdate, vrmmk2vsc), vreg operations, file management. venus-platform starts it from service-templates.
#   - cron 02:05 and init (S99, after 300 s) run check-updates.sh -auto: installs a release when AutoUpdate is
#     "Check and update", and that setting can be written remotely.
#   - support-keys: SSH public keys of Victron staff, sshd reads /run/ssh_support_keys and generate_authorized_keys.sh
#     (user vnctunnel: port forwarding to GUI, Node-RED, Signal K, VNC when VrmPortal is Full).
# Measures: the bridge templates lose every inbound write topic (no W, no I/in; the RPC template is a comment, so
# no GXrpc bridge in any mode), mqtt-rpc is a stub, the update checks are stubs and out of cron / init, the support
# keys and the tunnel key command are empty. Reporting stays: GXdbus publish N + subscribe R (read requests,
# keepalive), vrmlogger over HTTPS.
set -eu
R="${1:?usage: venus_harden.sh <venus rootfs dir>}"
[ -d "$R/opt/victronenergy" ] || { echo "not a Venus rootfs: $R" >&2; exit 1; }
TAG="venus_harden.sh"

# 1. VRM bridge: report only. venus-platform needs both templates non-empty (else no VRM registration at all).
F="$R/usr/share/flashmq"
mkdir -p "$F"
cat > "$F/venus_dbus_bridge_template.conf" <<'EOF'
bridge {
  protocol_version mqtt5
  address %%dynamicaddress%%
  port 443
  tls on
  bridge_protocol_bit true
  publish N/%%vrmid%%/#
  subscribe R/%%vrmid%%/#
  publish   I/%%vrmid%%/out/#
  clientid_prefix GXdbus
  local_username GXdbus
  remote_username %%remote_username%%
  remote_password %%remote_password%%
  ca_file %%ca_file%%
}
EOF
echo "# GXrpc bridge removed by $TAG: no remote commands (P/in) from VRM" > "$F/venus_rpc_bridge_template.conf"

# 2. mqtt-rpc: venus-platform installs /service/mqtt-rpc from this template; keep the service, run nothing
T="$R/opt/victronenergy/service-templates/mqtt-rpc"
mkdir -p "$T/log"
cat > "$T/run" <<EOF
#!/bin/sh
# mqtt-rpc disabled by $TAG (remote commands: firmware, VE.Bus settings, files)
echo "*** mqtt-rpc disabled by $TAG ***"
while :; do sleep 86400; done
EOF
# a running Venus keeps its own copy in /service (tmpfs inside the container): copy this run there and svc -t,
# or it restarts the removed mqtt-rpc.py every second until the next container start
printf '#!/bin/sh\nexec multilog t s25000 n4 /var/log/mqtt-rpc\n' > "$T/log/run"
chmod 755 "$T/run" "$T/log/run"
rm -rf "$R/opt/victronenergy/mqtt-rpc"

# 3. firmware automation: no update check from cron, init or the GUI button; swupdate daemon not started
sed -i '/check-updates\.sh/d' "$R/etc/crontab"
rm -f "$R"/etc/rc*.d/*check-updates.sh "$R"/etc/rc*.d/*swupdate
U="$R/opt/victronenergy/swupdate-scripts/check-updates.sh"
cat > "$U" <<EOF
#!/bin/sh
# disabled by $TAG: no firmware download or install on this system
echo "check-updates disabled by $TAG" >&2
exit 1
EOF
chmod 755 "$U"

# 4. remote support / tunnel: no Victron staff keys, no dynamic tunnel keys
[ -d "$R/usr/share/support-keys" ] && : > "$R/usr/share/support-keys/authorized_keys"
G="$R/usr/sbin/generate_authorized_keys.sh"
if [ -e "$G" ]; then
	printf '#!/bin/sh\n# disabled by %s: no keys for vnctunnel / remote support\nexit 0\n' "$TAG" > "$G"
	chmod 755 "$G"
fi
S="$R/etc/ssh/sshd_config"
if [ -f "$S" ]; then
	sed -i -e 's|^AuthorizedKeysFile.*|AuthorizedKeysFile\t.ssh/authorized_keys|' \
		-e '/^AuthorizedKeysCommand/d' "$S"
fi

# check: no inbound topic left in the templates, the stubs in place
! grep -qE '^\s*subscribe\s+(W|P|I)/' "$F"/venus_*_bridge_template.conf || { echo "$TAG: inbound topic left" >&2; exit 1; }
! grep -q 'check-updates' "$R/etc/crontab" || { echo "$TAG: cron update left" >&2; exit 1; }
echo "hardened $R: VRM report only (N out, R in), no W / RPC / update / support keys"
