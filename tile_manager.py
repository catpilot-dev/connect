"""OSM tile download manager for mapd offline data.

Handles downloading, extracting, scanning, and deleting 2x2 degree OSM tiles
from map-data.pfeifer.dev. Tiles are stored at /data/media/0/osm/.

Grid: 2 degree x 2 degree tiles, lat/lon aligned to even integers.
URL pattern: https://map-data.pfeifer.dev/offline/{lat}/{lon}.tar.gz
"""

import logging
import os
import shutil
import tarfile
import threading
import time
import urllib.request
from pathlib import Path

logger = logging.getLogger("connect.tiles")

from config import OSM_DIR
OSM_BASE = Path(OSM_DIR)
OSM_OFFLINE = OSM_BASE / "offline"
OSM_TMP = OSM_BASE / "tmp"

TILE_URL = "https://map-data.pfeifer.dev/offline/{lat}/{lon}.tar.gz"

# Module-level download state (polled by API)
_state = {
    "active": False,
    "total": 0,
    "done": 0,
    "current": None,
    "error": None,
    "cancelled": False,
}
_state_lock = threading.Lock()


def _snap_to_grid(lat: int, lon: int) -> tuple[int, int]:
    """Snap lat/lon to the 2-degree even grid."""
    return (lat // 2) * 2, (lon // 2) * 2


def get_downloaded_tiles() -> list[dict]:
    """Scan /data/media/0/osm/offline/ for downloaded tile directories.

    Returns list of {lat, lon, size_mb} for each tile found.
    """
    tiles = []
    if not OSM_OFFLINE.is_dir():
        return tiles

    for lat_dir in sorted(OSM_OFFLINE.iterdir()):
        if not lat_dir.is_dir():
            continue
        try:
            lat = int(lat_dir.name)
        except ValueError:
            continue

        for lon_dir in sorted(lat_dir.iterdir()):
            if not lon_dir.is_dir():
                continue
            try:
                lon = int(lon_dir.name)
            except ValueError:
                continue

            # Calculate directory size
            size = 0
            for f in lon_dir.rglob("*"):
                if f.is_file():
                    size += f.stat().st_size

            tiles.append({
                "lat": lat,
                "lon": lon,
                "size_mb": round(size / (1024 * 1024), 1),
            })

    return tiles


def get_storage_info() -> dict:
    """Get total OSM storage usage."""
    total = 0
    if OSM_BASE.is_dir():
        for f in OSM_BASE.rglob("*"):
            if f.is_file():
                total += f.stat().st_size

    return {
        "total_mb": round(total / (1024 * 1024), 1),
        "tile_count": len(get_downloaded_tiles()),
    }


def download_tile(lat: int, lon: int) -> bool:
    """Download and extract a single tile.

    Returns True on success, False on failure.
    """
    lat, lon = _snap_to_grid(lat, lon)
    url = TILE_URL.format(lat=lat, lon=lon)

    OSM_TMP.mkdir(parents=True, exist_ok=True)
    tmp_file = OSM_TMP / f"{lat}_{lon}.tar.gz"

    try:
        logger.info("Downloading tile %d,%d from %s", lat, lon, url)
        urllib.request.urlretrieve(url, str(tmp_file))

        # Extract to OSM base — tar entries have relative paths like offline/lat/lon/...
        logger.info("Extracting tile %d,%d", lat, lon)
        with tarfile.open(str(tmp_file), "r:gz") as tar:
            # Security: check for path traversal
            for member in tar.getmembers():
                if member.name.startswith("/") or ".." in member.name:
                    raise ValueError(f"Unsafe tar entry: {member.name}")
            tar.extractall(path=str(OSM_BASE))

        logger.info("Tile %d,%d extracted successfully", lat, lon)
        return True

    except Exception as e:
        logger.error("Failed to download tile %d,%d: %s", lat, lon, e)
        raise

    finally:
        # Cleanup temp file
        if tmp_file.exists():
            tmp_file.unlink()


def download_tiles(tile_list: list[dict]) -> None:
    """Download multiple tiles sequentially with progress tracking.

    Called in a background thread. Updates _state for polling.
    tile_list: [{lat, lon}, ...]
    """
    global _state
    with _state_lock:
        _state = {
            "active": True,
            "total": len(tile_list),
            "done": 0,
            "current": None,
            "error": None,
            "cancelled": False,
        }

    for tile in tile_list:
        with _state_lock:
            if _state["cancelled"]:
                _state["active"] = False
                return
            _state["current"] = f"{tile['lat']},{tile['lon']}"

        try:
            download_tile(tile["lat"], tile["lon"])
        except Exception as e:
            with _state_lock:
                _state["error"] = f"Tile {tile['lat']},{tile['lon']}: {e}"
                _state["active"] = False
            return

        with _state_lock:
            _state["done"] += 1

    with _state_lock:
        _state["active"] = False
        _state["current"] = None


def cancel_download() -> None:
    """Signal the download thread to stop."""
    with _state_lock:
        _state["cancelled"] = True


def get_progress() -> dict:
    """Return a copy of the current download state."""
    with _state_lock:
        return dict(_state)


def delete_tile(lat: int, lon: int) -> bool:
    """Delete a downloaded tile directory."""
    lat, lon = _snap_to_grid(lat, lon)
    tile_dir = OSM_OFFLINE / str(lat) / str(lon)

    if not tile_dir.is_dir():
        return False

    shutil.rmtree(str(tile_dir))
    logger.info("Deleted tile %d,%d", lat, lon)

    # Clean up empty parent lat directory
    lat_dir = OSM_OFFLINE / str(lat)
    if lat_dir.is_dir() and not any(lat_dir.iterdir()):
        lat_dir.rmdir()

    return True
