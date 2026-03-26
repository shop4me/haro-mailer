#!/usr/bin/env bash
# Run ON THE SERVER as root: enable asset automation flags in the app .env (idempotent).
# Usage: sudo bash deploy/enable_asset_env.sh /home/haro/haro-mailer/.env
set -euo pipefail
F="${1:-}"
if [[ -z "$F" ]]; then
  echo "Usage: $0 /path/to/.env" >&2
  exit 1
fi
touch "$F"

set_kv() {
  local k="$1" v="$2"
  if grep -q "^${k}=" "$F" 2>/dev/null; then
    sed -i "s/^${k}=.*/${k}=${v}/" "$F"
  else
    echo "${k}=${v}" >> "$F"
  fi
}

set_kv ENABLE_ASSET_AUTOMATION true
set_kv ENABLE_AI_CONCEPT_VISUALS true
set_kv ENABLE_INLINE_IMAGE_PREVIEWS true
set_kv AUTO_SEND_CONCEPT_VISUALS true
set_kv AUTO_SEND_REAL_ASSETS true
set_kv ASSET_PLANNER_USE_LLM false
set_kv MAX_INLINE_PREVIEW_IMAGES 2
set_kv MAX_GENERATED_CANDIDATES 6
echo "OK: updated $F"
