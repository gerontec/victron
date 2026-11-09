#!/bin/sh
# install_dmn_js.sh - dmn-js (bpmn.io, MIT + watermark clause, see LICENSE) for web/bm_editor.php into <web1>/vendor/
# Downloads the npm tarball, checks the sha512 of the npm registry, copies dist/ (bundles + assets) and LICENSE.
# usage (on heissa): sh install_dmn_js.sh /var/www/web1
set -e
VER=18.0.2
SHA512='0Ryhf7sRE6/JZTH1sN+uC911l9Wow8R9v2VjNABcas4zD4P0+aaGtyXH/SatRmsgEaEO1HyGkyEL8rqniNsB7Q=='
WEB=${1:?usage: install_dmn_js.sh <web root, e.g. /var/www/web1>}
T=$(mktemp -d)
curl -sf -m 60 -o "$T/dmn-js.tgz" "https://registry.npmjs.org/dmn-js/-/dmn-js-$VER.tgz"
GOT=$(openssl dgst -sha512 -binary "$T/dmn-js.tgz" | base64 -w0)
[ "$GOT" = "$SHA512" ] || { echo "sha512 mismatch: $GOT" >&2; exit 1; }
tar -xzf "$T/dmn-js.tgz" -C "$T"
D="$WEB/vendor/dmn-js-$VER"
mkdir -p "$D"
cp -r "$T/package/dist/." "$D/"
cp "$T/package/LICENSE" "$D/"
rm -f "$D"/*.development.js
rm -rf "$T"
echo "dmn-js $VER in $D"
