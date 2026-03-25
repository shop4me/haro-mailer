#!/usr/bin/env bash
# Deploy HARO mailer: git push, then git pull only inside DEPLOY_PATH on the server.
# No other directories or vhosts are touched.
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
BRANCH="${DEPLOY_BRANCH:-main}"
REMOTE="${DEPLOY_GIT_REMOTE:-origin}"
MARKER=".haro-mailer-root"

echo "==> Repo root: $ROOT"
echo "==> Server: ${DEPLOY_USER}@${DEPLOY_HOST}"
echo "==> Deploy path (only this directory is updated): ${DEPLOY_PATH}"

if ! git rev-parse --git-dir >/dev/null 2>&1; then
  echo "ERROR: Not a git repository." >&2
  exit 1
fi

if [[ -n "$(git status --porcelain 2>/dev/null)" ]]; then
  echo "WARNING: Uncommitted changes present." >&2
  read -r -p "Continue with git push anyway? [y/N] " ans || true
  if [[ "${ans:-}" != "y" && "${ans:-}" != "Y" ]]; then
    echo "Aborted."
    exit 1
  fi
fi

echo "==> git push ${REMOTE} ${BRANCH}"
git push "${REMOTE}" "${BRANCH}"

# Quote for remote shell: path must be literal on server
q_path=$(printf '%q' "$DEPLOY_PATH")
q_marker=$(printf '%q' "$MARKER")
q_remote=$(printf '%q' "$REMOTE")
q_branch=$(printf '%q' "$BRANCH")

REMOTE_CMD="set -euo pipefail
cd ${q_path} || { echo 'ERROR: cannot cd to deploy path' >&2; exit 1; }
test -f ${q_marker} || { echo 'ERROR: missing .haro-mailer-root in deploy dir — refusing to touch this path.' >&2; exit 1; }
git pull --ff-only ${q_remote} ${q_branch}
echo OK: deployed at \$(pwd) commit \$(git rev-parse --short HEAD)"

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

echo "==> SSH: git pull --ff-only in deploy dir only (marker required)"
run_ssh

echo "==> Done."
