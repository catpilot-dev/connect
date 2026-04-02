#!/bin/bash
# Build patched raylib with headless EGL support for Adreno GPU (C3)
#
# Patches commaai/raylib to add:
#   - GLFW null platform (headless, no display needed)
#   - GBM-backed EGL via /dev/dri/renderD128 (no DRM master)
#   - EGL pbuffer surfaces for offscreen rendering
#   - Adreno EGL library loading
#
# Usage: ./build_headless_raylib.sh [output_dir]
#   Run on C3 (aarch64) or cross-compile host
#   Requires: cmake, gcc, libEGL-dev, libgbm-dev, libGLESv2-dev

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OUTPUT_DIR="${1:-$SCRIPT_DIR/out}"
PATCH_FILE="$SCRIPT_DIR/raylib_headless_adreno.patch"

# Clone if not present
if [ ! -d "$SCRIPT_DIR/raylib/.git" ]; then
    echo "Cloning commaai/raylib..."
    git clone --depth 1 https://github.com/commaai/raylib.git "$SCRIPT_DIR/raylib"
fi

# Apply patch
echo "Applying headless Adreno patch..."
cd "$SCRIPT_DIR/raylib"
git checkout -- .
git apply "$PATCH_FILE"

# Build
echo "Building raylib..."
mkdir -p build && cd build
cmake .. \
    -DCMAKE_BUILD_TYPE=Release \
    -DPLATFORM=Desktop \
    -DOPENGL_VERSION="ES 2.0" \
    -DBUILD_EXAMPLES=OFF \
    -DCMAKE_C_FLAGS="-DEGL_NO_X11"
make -j$(nproc)

# Copy output
mkdir -p "$OUTPUT_DIR"
cp raylib/libraylib.a "$OUTPUT_DIR/"
echo "Built: $OUTPUT_DIR/libraylib.a"
echo ""
echo "Link flags: -lraylib -lEGL -lGLESv2 -lgbm -lm -lpthread -ldl"
echo "Env var:    OPENPILOT_UI_NULL_EGL=1"
