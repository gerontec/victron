<?php
// bm_swagger.php - Swagger UI of bm_api.php (description: bm_api_openapi.yaml; vendor/ with web/install_web_vendor.sh)
//   "Try it out" offers the writes only to clients that may write (bm_access.php), the others could only get a 403.
//   Swagger UI has no dark theme: the page is dark Bootstrap, the UI itself is inverted (hue kept) by the filter below.
header('Cache-Control: no-store');
const SWAGGER = 'vendor/swagger-ui-dist-5.17.14';
const BOOTSTRAP = 'vendor/bootstrap-5.3.3';
require_once __DIR__ . '/bm_access.php';
$vpn = bm_can_write();
$methods = $vpn ? ['get', 'post'] : ['get'];
?><!DOCTYPE html>
<html lang="en" data-bs-theme="dark"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>batmonitor API</title>
<link rel="stylesheet" href="<?= BOOTSTRAP ?>/bootstrap.min.css">
<link rel="stylesheet" href="<?= SWAGGER ?>/swagger-ui.css">
<style>
#ui { filter: invert(88%) hue-rotate(180deg); }
#ui .microlight, #ui img { filter: invert(100%) hue-rotate(180deg); }
</style></head><body>
<nav class="navbar bg-body-tertiary border-bottom">
  <div class="container-fluid gap-2">
    <span class="navbar-brand mb-0 h1">batmonitor API</span>
    <div class="d-flex flex-wrap align-items-center gap-3">
      <?= $vpn ? '<span class="badge text-bg-warning">a POST without dry_run goes live within a minute</span>' : '<span class="badge text-bg-secondary">read only from this address</span>' ?>
      <a class="link-light" href="bm_editor.php">editor</a>
      <a class="link-light" href="bm_api_openapi.yaml">OpenAPI file</a>
    </div>
  </div>
</nav>
<div id="ui"></div>
<script src="<?= SWAGGER ?>/swagger-ui-bundle.js"></script>
<script>
SwaggerUIBundle({ url: 'bm_api_openapi.yaml', dom_id: '#ui', deepLinking: true, validatorUrl: null,
                  supportedSubmitMethods: <?= json_encode($methods) ?>, presets: [SwaggerUIBundle.presets.apis] });
</script>
</body></html>
