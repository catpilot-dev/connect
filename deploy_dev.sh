#!/bin/bash
# Dev deploy: build frontend + rsync to C3
# Usage: ./deploy_dev.sh [host]
set -e

HOST="${1:-c3}"
REMOTE_DIR="/data/connect-on-device"
LOCAL_DIR="$(cd "$(dirname "$0")" && pwd)"

# Build frontend if static/ is stale or missing. Use `find -newer` rather than
# bash's `-nt` on the src directory — directory mtimes don't update when files
# inside them change, so plain `-nt` silently skipped rebuilds for src edits.
NEWER_SRC=""
if [ -f "$LOCAL_DIR/static/index.html" ]; then
  NEWER_SRC=$(find "$LOCAL_DIR/frontend" -type f \
    -not -path '*/node_modules/*' \
    -not -path '*/.vite/*' \
    -newer "$LOCAL_DIR/static/index.html" -print -quit 2>/dev/null)
fi

if [ ! -f "$LOCAL_DIR/static/index.html" ] || [ -n "$NEWER_SRC" ]; then
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
ssh "$HOST" "pkill -f 'python.*server\.py' 2>/dev/null; sleep 3; bash ${REMOTE_DIR}/setup_service.sh; for i in 1 2 3 4 5 6 7 8 9 10; do sleep 2; curl -sf http://localhost/health >/dev/null && { echo ' COD is up'; exit 0; }; done; echo 'COD not responding yet'"

echo "Dev deploy complete"
