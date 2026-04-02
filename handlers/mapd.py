import asyncio
import json
import logging
import os
import re
import subprocess
import threading
from pathlib import Path

from aiohttp import web

from handler_helpers import error_response, parse_json, read_param
import tile_manager

logger = logging.getLogger("connect")

# ─── OSM tile management ──────────────────────────────────────────────

_tile_download_thread = None

# ─── Mapd binary update ──────────────────────────────────────────────

from config import OPENPILOT_DIR, PYTHON_BIN, PLUGINS_RUNTIME_DIR

MAPD_MANAGER = Path(PLUGINS_RUNTIME_DIR) / "mapd" / "mapd_manager.py"
_MAPD_ENV = {**os.environ, "PYTHONPATH": OPENPILOT_DIR, "PWD": OPENPILOT_DIR}


async def handle_tile_list(request: web.Request) -> web.Response:
    """GET /v1/mapd/tiles — list downloaded tiles with storage info."""
    loop = asyncio.get_event_loop()
    tiles = await loop.run_in_executor(None, tile_manager.get_downloaded_tiles)
    storage = await loop.run_in_executor(None, tile_manager.get_storage_info)
    return web.json_response({"tiles": tiles, "storage": storage})


async def handle_tile_download(request: web.Request) -> web.Response:
    """POST /v1/mapd/tiles/download — start downloading tiles in background."""
    global _tile_download_thread

    progress = tile_manager.get_progress()
    if progress["active"]:
        return error_response("Download already in progress", 409)

    body = await parse_json(request)

    tiles = body.get("tiles", [])
    if not tiles:
        raise web.HTTPBadRequest(text=json.dumps({"error": "No tiles specified"}))

    # Validate tile coordinates
    for t in tiles:
        if "lat" not in t or "lon" not in t:
            raise web.HTTPBadRequest(text=json.dumps({"error": "Each tile must have lat and lon"}))

    import threading
    _tile_download_thread = threading.Thread(
        target=tile_manager.download_tiles,
        args=(tiles,),
        daemon=True,
    )
    _tile_download_thread.start()

    return web.json_response({"status": "started", "total": len(tiles)})


async def handle_tile_progress(request: web.Request) -> web.Response:
    """GET /v1/mapd/tiles/progress — poll download progress."""
    return web.json_response(tile_manager.get_progress())


async def handle_tile_cancel(request: web.Request) -> web.Response:
    """POST /v1/mapd/tiles/cancel — cancel active download."""
    tile_manager.cancel_download()
    return web.json_response({"status": "cancelling"})


async def handle_tile_delete(request: web.Request) -> web.Response:
    """DELETE /v1/mapd/tiles/{lat}/{lon} — delete a downloaded tile."""
    try:
        lat = int(request.match_info["lat"])
        lon = int(request.match_info["lon"])
    except (ValueError, KeyError):
        raise web.HTTPBadRequest(text=json.dumps({"error": "Invalid lat/lon"}))

    loop = asyncio.get_event_loop()
    deleted = await loop.run_in_executor(None, tile_manager.delete_tile, lat, lon)
    if not deleted:
        raise web.HTTPNotFound(text=json.dumps({"error": f"Tile {lat},{lon} not found"}))

    return web.json_response({"status": "deleted", "lat": lat, "lon": lon})


async def handle_mapd_check_update(request: web.Request) -> web.Response:
    """POST /v1/mapd/check-update — check for mapd binary updates via mapd_manager.py."""
    try:
        proc = await asyncio.wait_for(
            asyncio.create_subprocess_exec(
                PYTHON_BIN, str(MAPD_MANAGER), "check",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(OPENPILOT_DIR),
                env=_MAPD_ENV,
            ),
            timeout=30,
        )
        stdout, stderr = await proc.communicate()
        output = stdout.decode().strip()

        # Extract optional date suffix like " (2026-01-31)"
        import re
        date_match = re.search(r"\((\d{4}-\d{2}-\d{2})\)", output)
        release_date = date_match.group(1) if date_match else None
        # Strip the date suffix for version parsing
        clean = re.sub(r"\s*\(\d{4}-\d{2}-\d{2}\)", "", output)

        if "UP_TO_DATE:" in clean:
            version = clean.split("UP_TO_DATE:")[-1].strip()
            return web.json_response({
                "current": version, "latest": version,
                "update_available": False, "release_date": release_date,
            })
        elif "UPDATE_AVAILABLE:" in clean:
            parts = clean.split("UPDATE_AVAILABLE:")[-1].strip()
            current, latest = [p.strip() for p in parts.split("->")]
            return web.json_response({
                "current": current, "latest": latest,
                "update_available": True, "release_date": release_date,
            })
        else:
            return web.json_response(
                {"error": stderr.decode().strip() or output or "Unknown check output"},
                status=500,
            )
    except asyncio.TimeoutError:
        return error_response("Check timed out", 504)
    except Exception as e:
        return error_response(str(e), 500)


async def handle_mapd_update(request: web.Request) -> web.Response:
    """POST /v1/mapd/update — download and install mapd binary update."""
    try:
        proc = await asyncio.wait_for(
            asyncio.create_subprocess_exec(
                PYTHON_BIN, str(MAPD_MANAGER), "update",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(OPENPILOT_DIR),
                env=_MAPD_ENV,
            ),
            timeout=120,
        )
        stdout, stderr = await proc.communicate()
        output = stdout.decode().strip()

        if proc.returncode == 0:
            # Read updated version from param
            version = read_param("MapdVersion") or None
            return web.json_response({"status": "ok", "version": version})
        else:
            return web.json_response(
                {"error": stderr.decode().strip() or output or "Update failed"},
                status=500,
            )
    except asyncio.TimeoutError:
        return error_response("Update timed out (120s)", 504)
    except Exception as e:
        return error_response(str(e), 500)
