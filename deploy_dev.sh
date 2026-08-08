#!/bin/bash
# Dev deploy: build frontend + rsync to C3
# Usage: ./deploy_dev.sh [host]
set -e

HOST="${1:-c3}"
REMOTE_DIR="/data/connect-on-device"
LOCAL_DIR="$(cd "$(dirname "$0")" && pwd)"

# Build frontend if static/ is stale or missing
if [ ! -d "$LOCAL_DIR/static" ] || [ "$LOCAL_DIR/frontend/src" -nt "$LOCAL_DIR/static/index.html" ]; then
  echo "Building frontend..."
  cd "$LOCAL_DIR/frontend"
  npm run build
  cd "$LOCAL_DIR"
else
  echo "Frontend up to date, skipping build"
fi

echo "Deploying to ${HOST}:${REMOTE_DIR}..."

# Sync source files only (no --delete to preserve runtime data)
rsync -avz \
  --exclude='.venv' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='.git' \
  --exclude='.gitignore' \
  --exclude='build' \
  --exclude='.envrc' \
  --exclude='.pytest_cache' \
  --exclude='frontend' \
  --exclude='node_modules' \
  --exclude='tests' \
  --exclude='reference' \
  --exclude='test_overlay.png' \
  "$LOCAL_DIR/" "${HOST}:${REMOTE_DIR}/"

echo "Cleaning caches..."
ssh "$HOST" "find ${REMOTE_DIR} -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null; find ${REMOTE_DIR} -name '*.pyc' -delete 2>/dev/null; true"

echo "Cleaning stale assets..."
# Build a list of current asset filenames, then remove anything on the device not in that list
CURRENT_ASSETS=$(ls "$LOCAL_DIR/static/assets/")
ssh "$HOST" "cd ${REMOTE_DIR}/static/assets && for f in *; do echo '$CURRENT_ASSETS' | grep -qxF \"\$f\" || rm -v \"\$f\"; done"

echo "Restarting COD..."
ssh "$HOST" "pkill -f 'python.*server\.py' 2>/dev/null; sleep 3; bash ${REMOTE_DIR}/setup_service.sh; sleep 3; curl -sf http://localhost:8082/health && echo ' COD is up' || echo 'COD not responding yet'"

echo "Dev deploy complete"
