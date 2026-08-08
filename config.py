"""Centralized path configuration for Connect on Device.

All C3 device paths are defined here with env var overrides for local
development and non-standard installations (e.g. upstream openpilot forks).

Environment variables:
  OPENPILOT_DIR      — openpilot installation (default: /data/openpilot)
  PLUGINS_RUNTIME_DIR — installed plugins (default: /data/plugins-runtime)
  PLUGINS_REPO_DIR   — plugins git repo (default: /data/plugins)
  REALDATA_DIR       — route data directory (default: /data/media/0/realdata)
  PARAMS_DIR         — openpilot params (default: /data/params/d)
  MODELS_DIR         — driving models (default: /data/models)
  OSM_DIR            — offline OSM tiles (default: /data/media/0/osm)
"""

import os
from pathlib import Path

# Core openpilot paths
OPENPILOT_DIR = os.getenv("OPENPILOT_DIR", "/data/openpilot")
PARAMS_DIR = os.getenv("PARAMS_DIR", "/data/params/d")
PARAMS_BASE = str(Path(PARAMS_DIR).parent)  # /data/params

# Plugin paths
PLUGINS_RUNTIME_DIR = os.getenv("PLUGINS_RUNTIME_DIR", "/data/plugins-runtime")
PLUGINS_REPO_DIR = os.getenv("PLUGINS_REPO_DIR", "/data/plugins")

# Data paths
REALDATA_DIR = os.getenv("REALDATA_DIR", "/data/media/0/realdata")
MODELS_DIR = os.getenv("MODELS_DIR", "/data/models")
OSM_DIR = os.getenv("OSM_DIR", "/data/media/0/osm")

# COD's own directories (derived from install location)
COD_DIR = str(Path(__file__).parent)
COD_CACHE_DIR = os.getenv("COD_CACHE_DIR", "/data/connect-on-device/cache")
COD_HUD_CACHE_DIR = os.getenv("COD_HUD_CACHE_DIR", "/data/connect-on-device/hud_cache")
COD_HLS_TMP_DIR = os.getenv("COD_HLS_TMP_DIR", "/data/connect-on-device/hud_hls_tmp")

# Binaries
PYTHON_BIN = os.getenv("PYTHON_BIN", "/usr/local/venv/bin/python")
FFMPEG_BIN = os.getenv("FFMPEG_BIN", "/usr/local/bin/ffmpeg")
REPLAY_BIN = os.path.join(OPENPILOT_DIR, "tools/replay/replay")

# Staging paths for branch upgrades
STAGING_PATHS = [
    "/data/safe_staging/finalized/plugins",
    "/data/safe_staging/upper/plugins",
]

# Build hash file (cleared to trigger plugin rebuild)
BUILD_HASH_FILE = "/tmp/plugin_build_hash"

# Plugind internal API
PLUGIND_API_URL = os.getenv("PLUGIND_API_URL", "http://127.0.0.1:8083")

# Device identity
DEVICE_HOSTNAME = os.getenv("DEVICE_HOSTNAME", "catpilot")  # advertised via mDNS as catpilot.local


def ensure_openpilot_in_path():
    """Add OPENPILOT_DIR to sys.path if not already present."""
    import sys
    if OPENPILOT_DIR not in sys.path:
        sys.path.insert(0, OPENPILOT_DIR)
