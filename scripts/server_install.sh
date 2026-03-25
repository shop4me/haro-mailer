#!/usr/bin/env bash
# Run ON THE SERVER as user haro (sudo only for optional systemd step).
# Only modifies paths under the HARO mailer app directory.
set -euo pipefail

APP="${DEPLOY_PATH:-/home/haro/haro-mailer}"
cd "$APP"

if [[ ! -f "$APP/.haro-mailer-root" ]]; then
  echo "ERROR: $APP/.haro-mailer-root missing — refusing (wrong directory?)." >&2
  exit 1
fi

WITH_SYSTEMD=0
for arg in "$@"; do
  case "$arg" in
    --with-systemd) WITH_SYSTEMD=1 ;;
    -h|--help)
      echo "Usage: $0 [--with-systemd]"
      echo "  Installs venv + deps, init_db. With --with-systemd: install unit (needs sudo)."
      exit 0
      ;;
  esac
done

echo "==> venv + pip"
python3 -m venv .venv
# shellcheck source=/dev/null
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo "==> init_db"
python -c "from app.db import init_db; init_db()"

if [[ "$WITH_SYSTEMD" -eq 1 ]]; then
  echo "==> systemd (requires sudo)"
  sudo cp "$APP/deploy/haro-mailer.service" /etc/systemd/system/haro-mailer.service
  sudo systemctl daemon-reload
  sudo systemctl enable haro-mailer
  sudo systemctl restart haro-mailer
  sudo systemctl status haro-mailer --no-pager || true
  echo "==> Listening on 0.0.0.0:18080 (see gunicorn.conf.py). Point nginx at it or open that port in the firewall."
else
  echo "==> Done (no systemd). Run manually:"
  echo "    cd $APP && source .venv/bin/activate && gunicorn -c gunicorn.conf.py app:create_app()"
  echo "Or: $0 --with-systemd"
fi
