#!/bin/sh
# install_web_vendor.sh - Bootstrap (MIT) for web/bm_editor.php and Swagger UI (Apache-2.0) for web/bm_swagger.php into
# <web1>/vendor/. Downloads the npm tarballs, checks the sha512 of the npm registry, copies only the files in use.
# usage (on heissa): sh install_web_vendor.sh /var/www/web1
set -e
WEB=${1:?usage: install_web_vendor.sh <web root, e.g. /var/www/web1>}

# fetch <package> <version> <sha512>: leaves the unpacked package in $T/package
fetch() {
    T=$(mktemp -d)
    curl -sf -m 60 -o "$T/p.tgz" "https://registry.npmjs.org/$1/-/$1-$2.tgz"
    GOT=$(openssl dgst -sha512 -binary "$T/p.tgz" | base64 -w0)
    [ "$GOT" = "$3" ] || { echo "$1: sha512 mismatch: $GOT" >&2; exit 1; }
    tar -xzf "$T/p.tgz" -C "$T"
}

fetch bootstrap 5.3.3 '8HLCdWgyoMguSO9o+aH+iuZ+aht+mzW0u3HIMzVu7Srrpv7EBBxTnrFlSCskwdY1+EOFQSm7uMJhNQHkdPcmjg=='
D="$WEB/vendor/bootstrap-5.3.3"
mkdir -p "$D"
cp "$T/package/dist/css/bootstrap.min.css" "$T/package/dist/js/bootstrap.bundle.min.js" "$T/package/LICENSE" "$D/"
rm -rf "$T"
echo "bootstrap in $D"

fetch swagger-ui-dist 5.17.14 'CVbSfaLpstV65OnSjbXfVd6Sta3q3F7Cj/yYuvHMp1P90LztOLs6PfUnKEVAeiIVQt9u2SaPwv0LiH/OyMjHRw=='
D="$WEB/vendor/swagger-ui-dist-5.17.14"
mkdir -p "$D"
cp "$T/package/swagger-ui-bundle.js" "$T/package/swagger-ui.css" "$T/package/LICENSE" "$D/"
rm -rf "$T"
echo "swagger-ui in $D"
