#!/bin/bash
# Deploy connect-on-device to C3 via GitHub release tarball
# Usage: ./deploy.sh [host]
set -e

HOST="${1:-comma@10.0.0.160}"
REMOTE_DIR="/data/connect-on-device"
LOCAL_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="catpilot-dev/connect-on-device"
VERSION="$(cat "$LOCAL_DIR/VERSION")"
TARBALL="cod-v${VERSION}.tar.gz"
RELEASE_URL="https://github.com/${REPO}/releases/download/v${VERSION}/${TARBALL}"

echo "Deploying COD v${VERSION} to ${HOST}:${REMOTE_DIR}"
echo "Source: ${RELEASE_URL}"

# Download tarball on device, extract, restart
ssh "$HOST" bash -s "$REMOTE_DIR" "$RELEASE_URL" "$TARBALL" << 'REMOTE'
set -e
REMOTE_DIR="$1"
RELEASE_URL="$2"
TARBALL="$3"

echo "Downloading ${TARBALL}..."
curl -fSL -o "/tmp/${TARBALL}" "$RELEASE_URL"

echo "Extracting to ${REMOTE_DIR}..."
rm -rf "${REMOTE_DIR}/handlers" "${REMOTE_DIR}/static"
tar xzf "/tmp/${TARBALL}" -C "${REMOTE_DIR}/" --strip-components=1
rm "/tmp/${TARBALL}"

echo "Version: $(cat ${REMOTE_DIR}/VERSION)"

echo "Restarting server..."
pkill -f 'python.*server.py' 2>/dev/null || true
sleep 2
${REMOTE_DIR}/setup_service.sh
sleep 2
pgrep -fa 'python.*server.py' || echo "WARNING: server not running"
REMOTE

echo ""
echo "Deployed COD v${VERSION} to ${HOST}"
