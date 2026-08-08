#!/bin/bash
# Build a COD release tarball (see plugins repo: docs/RELEASE_PROCESS.md).
#
#   ./make_release.sh            bootstrap release:  cod-vX.Y.Z.tar.gz
#   ./make_release.sh --rolling  rolling release:    cod-vX.Y.Z-YYYY.MM.DD.tar.gz
#
# The channel (X.Y.Z) comes from the repo VERSION file. The tarball contains a
# single top-level connect-on-device/ directory with the complete runtime —
# git-tracked sources only (never local working-tree edits) plus a freshly
# built static/ — matching what first_boot_setup.sh copies wholesale and what
# COD's self-update (_apply_cod_update) refreshes. The staged VERSION file
# carries the full release identity (e.g. 0.11.1-2026.08.09 for rolling), so
# devices can tell rolling builds apart from the bootstrap.
set -euo pipefail
cd "$(dirname "$0")"

CHANNEL=$(cut -d- -f1 VERSION)
if ! [[ "$CHANNEL" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "VERSION file does not start with X.Y.Z (got: $(cat VERSION))" >&2
  exit 1
fi

case "${1:-}" in
  "")        RELEASE_VERSION="$CHANNEL" ;;
  --rolling) RELEASE_VERSION="$CHANNEL-$(date +%Y.%m.%d)" ;;
  *)         echo "usage: $0 [--rolling]" >&2; exit 1 ;;
esac
TAG="v$RELEASE_VERSION"
ASSET="cod-$TAG.tar.gz"

echo "Building frontend..."
(cd frontend && npm run build)

STAGING=$(mktemp -d)
trap 'rm -rf "$STAGING"' EXIT
ROOT="$STAGING/connect-on-device"
mkdir -p "$ROOT"

# Runtime files from git HEAD: root modules, handlers/, service script.
# Deliberately from the committed tree — a release never picks up
# uncommitted working-tree changes.
ROOT_PY=$(git ls-files '*.py' | grep -v /)
git archive HEAD -- handlers setup_service.sh requirements.txt README.md VERSION $ROOT_PY \
  | tar -x -C "$ROOT"

cp -r static "$ROOT/static"
printf '%s\n' "$RELEASE_VERSION" > "$ROOT/VERSION"

# Sanity: it must at least parse and contain a servable frontend
python3 -m compileall -q "$ROOT"
find "$ROOT" -name __pycache__ -type d -exec rm -rf {} +  # bytecode was only a syntax check
[ -s "$ROOT/static/index.html" ] || { echo "static/index.html missing" >&2; exit 1; }
[ -s "$ROOT/server.py" ] || { echo "server.py missing" >&2; exit 1; }

mkdir -p dist
tar -czf "dist/$ASSET" -C "$STAGING" connect-on-device

echo
echo "Built dist/$ASSET ($(du -h "dist/$ASSET" | cut -f1)) — VERSION $RELEASE_VERSION"
echo
echo "Publish:"
echo "  git tag $TAG && git push origin $TAG"
echo "  gh release create $TAG dist/$ASSET --title $TAG --notes '<summary>'"
