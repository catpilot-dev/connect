"""Shared handler utilities to reduce boilerplate in handlers.py."""

import json
from pathlib import Path

from aiohttp import web

from config import PARAMS_DIR, PLUGINS_RUNTIME_DIR


def error_response(msg, status=400):
    """Return a JSON error response."""
    return web.json_response({"error": msg}, status=status)


async def parse_json(request):
    """Parse JSON body or raise HTTPBadRequest."""
    try:
        return await request.json()
    except Exception:
        raise web.HTTPBadRequest(text=json.dumps({"error": "Invalid JSON body"}))


def get_route_or_404(request):
    """Resolve route_name from URL and look it up in store. Raises HTTPNotFound.

    Route identifier is local_id (e.g. "00000123--ecd17bc154").
    Legacy pipe-encoded fullnames ("dongle|date") are also supported.
    """
    route_name = request.match_info["routeName"].replace("|", "/")
    store = request.app["store"]
    route = store.get_route(route_name)
    if not route:
        raise web.HTTPNotFound(text=json.dumps({"error": f"Route {route_name} not found"}))
    return route_name, route, store


def resolve_route_name(request):
    """Extract route identifier from URL path (no store lookup).

    Canonical format is local_id; legacy pipe-encoded fullnames also handled.
    """
    return request.match_info["routeName"].replace("|", "/")


def read_param(key, default=""):
    """Read an openpilot param file, returning default if missing."""
    try:
        return Path(f"{PARAMS_DIR}/{key}").read_text().strip()
    except FileNotFoundError:
        return default


def write_param(key, value):
    """Write a value to an openpilot param file."""
    Path(f"{PARAMS_DIR}/{key}").write_text(str(value))


def read_plugin_param(plugin_id, key, default=""):
    """Read a plugin param from plugins runtime data dir."""
    try:
        return Path(f"{PLUGINS_RUNTIME_DIR}/{plugin_id}/data/{key}").read_text().strip()
    except (FileNotFoundError, OSError):
        return default


def write_plugin_param(plugin_id, key, value):
    """Write a plugin param to plugins runtime data dir."""
    data_dir = Path(f"{PLUGINS_RUNTIME_DIR}/{plugin_id}/data")
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / key).write_text(str(value))
