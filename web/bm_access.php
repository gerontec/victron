<?php
// bm_access.php - who may write through bm_api.php and sees the forms of bm_editor.php / bm_swagger.php.
//   VPN clients (10.8.0.0/24, the direct peer) everywhere. The instance on the Pi (.123:8088, for an internet outage,
//   systemd unit bm-editor.service) sets BM_EDITOR_LAN=1 and also lets the intranet 192.168.178.0/24 write.
function bm_can_write(): bool {
    $ip = $_SERVER['REMOTE_ADDR'] ?? '';
    if (preg_match('/^10\.8\.0\.\d{1,3}$/', $ip)) return true;
    return getenv('BM_EDITOR_LAN') === '1' && preg_match('/^192\.168\.178\.\d{1,3}$/', $ip) === 1;
}
