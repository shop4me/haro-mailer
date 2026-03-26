#!/usr/bin/env bash
# Run ON THE WEB SERVER as root: sudo bash install_floatfire_ssl.sh
#
# Expects nginx-floatfire.com.conf in the same directory as this script.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF_SRC="${SCRIPT_DIR}/nginx-floatfire.com.conf"
AVAILABLE="/etc/nginx/sites-available/floatfire.com.conf"
ENABLED="/etc/nginx/sites-enabled/floatfire.com.conf"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run with sudo: sudo bash $0" >&2
  exit 1
fi

if [[ ! -f "$CONF_SRC" ]]; then
  echo "Missing $CONF_SRC — run from directory containing nginx-floatfire.com.conf" >&2
  exit 1
fi

cp "$CONF_SRC" "$AVAILABLE"
ln -sf "$AVAILABLE" "$ENABLED"
nginx -t
systemctl reload nginx

if [[ -f /etc/letsencrypt/live/floatfire.com/fullchain.pem ]]; then
  echo "==> Certificate already present for floatfire.com; reloading nginx"
else
  echo "==> Requesting Let's Encrypt certificate"
  if ! certbot --nginx -d floatfire.com -d www.floatfire.com; then
    echo "==> Retrying apex only (add www DNS later if needed)"
    certbot --nginx -d floatfire.com
  fi
fi

nginx -t
systemctl reload nginx
echo "==> Done. Test: curl -sI https://floatfire.com/login | head -5"
