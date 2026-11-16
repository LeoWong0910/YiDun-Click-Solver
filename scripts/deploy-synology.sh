#!/usr/bin/env bash
# One-click deploy of YiDun Click Solver to a Synology NAS (DSM 7 + Container Manager).
#
# Usage:
#   cp .env.example .env   # edit NAS_SSH / NAS_DIR / YIDUN_API_TOKEN
#   ./scripts/deploy-synology.sh
#
# Or pass SSH target explicitly:
#   ./scripts/deploy-synology.sh admin@192.168.1.50
#
# Requirements on your PC: bash, rsync or tar, ssh
# Requirements on NAS: Container Manager (Docker), SSH enabled, user in docker group
#   (or run with sudo). DSM path usually /volume1/docker/...

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ -f "$ROOT/.env" ]]; then
  # shellcheck disable=SC1091
  set -a
  source "$ROOT/.env"
  set +a
fi

NAS_SSH="${1:-${NAS_SSH:-}}"
NAS_DIR="${NAS_DIR:-/volume1/docker/yidun-click-solver}"
HOST_PORT="${HOST_PORT:-8765}"
YIDUN_API_TOKEN="${YIDUN_API_TOKEN:-}"
YIDUN_MAX_ATTEMPTS="${YIDUN_MAX_ATTEMPTS:-10}"
YIDUN_SOLVE_TIMEOUT="${YIDUN_SOLVE_TIMEOUT:-120}"
YIDUN_ZONE_ID="${YIDUN_ZONE_ID:-CN31}"
NOTIFY_ENABLED="${NOTIFY_ENABLED:-}"
NOTIFY_ON="${NOTIFY_ON:-success,failure}"
SERVERCHAN_SENDKEY="${SERVERCHAN_SENDKEY:-}"
PUSHPLUS_TOKEN="${PUSHPLUS_TOKEN:-}"
WECOM_WEBHOOK_URL="${WECOM_WEBHOOK_URL:-}"
NOTIFY_WEBHOOK_URL="${NOTIFY_WEBHOOK_URL:-}"
NOTIFY_EMAIL="${NOTIFY_EMAIL:-}"
SMTP_HOST="${SMTP_HOST:-}"
SMTP_PORT="${SMTP_PORT:-465}"
SMTP_SSL="${SMTP_SSL:-true}"
SMTP_STARTTLS="${SMTP_STARTTLS:-false}"
SMTP_USER="${SMTP_USER:-}"
SMTP_PASSWORD="${SMTP_PASSWORD:-}"
SMTP_FROM="${SMTP_FROM:-}"
NOTIFY_EMAIL_TO="${NOTIFY_EMAIL_TO:-}"

if [[ -z "$NAS_SSH" ]]; then
  echo "ERROR: set NAS_SSH in .env or pass user@nas-ip as the first argument." >&2
  echo "Example: ./scripts/deploy-synology.sh admin@192.168.1.50" >&2
  exit 1
fi

echo "==> Deploy target: ${NAS_SSH}:${NAS_DIR}"
echo "==> Published port: ${HOST_PORT}"

ssh -o BatchMode=yes -o ConnectTimeout=15 "$NAS_SSH" "mkdir -p '$NAS_DIR'"

echo "==> Syncing project files..."
if command -v rsync >/dev/null 2>&1; then
  rsync -az --delete \
    --exclude '.git' \
    --exclude '.venv' \
    --exclude 'venv' \
    --exclude '__pycache__' \
    --exclude '.env' \
    --exclude 'captures/*' \
    --exclude 'results/*' \
    --exclude '.cursor' \
    --exclude 'docs/assets' \
    --include 'captures/.gitkeep' \
    --include 'results/.gitkeep' \
    "$ROOT/" "$NAS_SSH:$NAS_DIR/"
else
  echo "rsync not found; falling back to tar over ssh"
  tar czf - \
    --exclude '.git' \
    --exclude '.venv' \
    --exclude 'venv' \
    --exclude '__pycache__' \
    --exclude '.env' \
    --exclude 'captures' \
    --exclude 'results' \
    --exclude '.cursor' \
    --exclude 'docs/assets' \
    -C "$ROOT" . \
    | ssh "$NAS_SSH" "mkdir -p '$NAS_DIR' && tar xzf - -C '$NAS_DIR'"
  ssh "$NAS_SSH" "mkdir -p '$NAS_DIR/captures' '$NAS_DIR/results' && touch '$NAS_DIR/captures/.gitkeep' '$NAS_DIR/results/.gitkeep'"
fi

echo "==> Writing remote .env"
# Values are expanded locally; remote file must not contain secrets in shell history tips.
ssh "$NAS_SSH" "cat > '$NAS_DIR/.env'" <<EOF
HOST_PORT=${HOST_PORT}
YIDUN_API_TOKEN=${YIDUN_API_TOKEN}
YIDUN_MAX_ATTEMPTS=${YIDUN_MAX_ATTEMPTS}
YIDUN_SOLVE_TIMEOUT=${YIDUN_SOLVE_TIMEOUT}
YIDUN_ZONE_ID=${YIDUN_ZONE_ID}
NOTIFY_ENABLED=${NOTIFY_ENABLED}
NOTIFY_ON=${NOTIFY_ON}
SERVERCHAN_SENDKEY=${SERVERCHAN_SENDKEY}
PUSHPLUS_TOKEN=${PUSHPLUS_TOKEN}
WECOM_WEBHOOK_URL=${WECOM_WEBHOOK_URL}
NOTIFY_WEBHOOK_URL=${NOTIFY_WEBHOOK_URL}
NOTIFY_EMAIL=${NOTIFY_EMAIL}
SMTP_HOST=${SMTP_HOST}
SMTP_PORT=${SMTP_PORT}
SMTP_SSL=${SMTP_SSL}
SMTP_STARTTLS=${SMTP_STARTTLS}
SMTP_USER=${SMTP_USER}
SMTP_PASSWORD=${SMTP_PASSWORD}
SMTP_FROM=${SMTP_FROM}
NOTIFY_EMAIL_TO=${NOTIFY_EMAIL_TO}
EOF

echo "==> Building and starting containers on NAS..."
ssh "$NAS_SSH" bash -s -- "$NAS_DIR" <<'EOF'
set -euo pipefail
NAS_DIR="$1"
cd "$NAS_DIR"

if docker compose version >/dev/null 2>&1; then
  docker compose -f compose.yaml up -d --build --remove-orphans
  docker compose -f compose.yaml ps
elif command -v docker-compose >/dev/null 2>&1; then
  docker-compose -f compose.yaml up -d --build --remove-orphans
  docker-compose -f compose.yaml ps
else
  echo "Docker Compose not found on NAS. Install Container Manager first." >&2
  exit 1
fi
EOF

echo
echo "✅ Deploy finished."
echo "   Health:  http://<nas-ip>:${HOST_PORT}/health"
echo "   Solve:   curl -X POST http://<nas-ip>:${HOST_PORT}/solve"
if [[ -n "$YIDUN_API_TOKEN" ]]; then
  echo "   Auth:    Authorization: Bearer **** (YIDUN_API_TOKEN is set)"
fi
echo
echo "Tip: In DSM → Container Manager → Project you should see 'yidun-click-solver'."
