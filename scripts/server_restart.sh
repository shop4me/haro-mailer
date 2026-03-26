#!/usr/bin/env bash
# Run from repo root after deploy: pip sync in venv, reload gunicorn (new code + deps).
# Uses same SSH auth as scripts/deploy.sh (.deploy.env + optional sshpass).
#
# Reload: kill -HUP to gunicorn master (no sudo). Alternative:
#   ssh haro@HOST 'sudo systemctl restart haro-mailer'
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT"

if [[ -f "$ROOT/.deploy.env" ]]; then
  # shellcheck source=/dev/null
  source "$ROOT/.deploy.env"
fi

DEPLOY_HOST="${DEPLOY_HOST:-142.93.187.80}"
DEPLOY_USER="${DEPLOY_USER:-haro}"
DEPLOY_PATH="${DEPLOY_PATH:-/home/haro/haro-mailer}"

q_path=$(printf '%q' "$DEPLOY_PATH")
_pg_pattern="${DEPLOY_PATH}/.venv/bin/gunicorn -c ${DEPLOY_PATH}/gunicorn.conf.py"
q_pg_pattern=$(printf '%q' "$_pg_pattern")

REMOTE_CMD="set -euo pipefail
cd ${q_path} || exit 1
source .venv/bin/activate
pip install -q -r requirements.txt
MASTER=\$(pgrep -o -f ${q_pg_pattern} || true)
if [[ -z \"\$MASTER\" ]]; then
  echo 'WARN: no gunicorn master; try: sudo systemctl start haro-mailer' >&2
  exit 1
fi
kill -HUP \"\$MASTER\"
sleep 2
curl -s -o /dev/null -w 'HTTP %{http_code} login\\n' http://127.0.0.1:18080/login
echo OK: gunicorn HUP pid=\$MASTER"

run_ssh() {
  if [[ -n "${DEPLOY_SSH_PASSWORD:-}" ]] && command -v sshpass >/dev/null 2>&1; then
    SSHPASS="$DEPLOY_SSH_PASSWORD" sshpass -e ssh \
      -o StrictHostKeyChecking=accept-new \
      -o ConnectTimeout=20 \
      "${DEPLOY_USER}@${DEPLOY_HOST}" "bash -lc $(printf '%q' "$REMOTE_CMD")"
  else
    ssh \
      -o StrictHostKeyChecking=accept-new \
      -o ConnectTimeout=20 \
      "${DEPLOY_USER}@${DEPLOY_HOST}" "bash -lc $(printf '%q' "$REMOTE_CMD")"
  fi
}

echo "==> ${DEPLOY_USER}@${DEPLOY_HOST}: pip + HUP gunicorn"
run_ssh
