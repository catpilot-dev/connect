"""CORS and onroad-safety middleware for aiohttp."""

import json
import logging
import re

from aiohttp import web

from handler_helpers import read_param

logger = logging.getLogger("connect")


@web.middleware
async def cors_middleware(request, handler):
    if request.method == "OPTIONS":
        resp = web.Response()
    else:
        resp = await handler(request)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, DELETE, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
    return resp


# Endpoints that change the device or the openpilot install. COD must never
# touch a moving car, and a UI-side check cannot guarantee that — a page opened
# while parked goes stale, and the API is reachable directly. Enforced here so
# no individual handler can forget it.
_ONROAD_FORBIDDEN = re.compile(
    r"^/v1/("
    r"device/(reboot|poweroff|language)"
    r"|software/(download|install|branch|uninstall|prepare-plugins|venv-sync)"
    r"|models/(swap|download)"
    r"|updates/apply"
    r"|plugins/"          # toggle, param, repo, repo/install
    r"|screencast/"       # takes over the C3 display and restarts the UI
    r"|route/[^/]+/hud/prerender"  # heavy headless render — competes with driving stack
    r")"
)

_MUTATING = {"POST", "PUT", "PATCH", "DELETE"}

# Reads that cost seconds of CPU on the device — decompressing and parsing
# rlogs, or tarring whole routes. They change nothing, but they compete with
# the driving stack on a machine that is also running the car, and a browser
# left open on a route page issues them in bulk (HUD overlay prefetch walks
# every segment). Route listings and metadata stay available while driving.
_ONROAD_FORBIDDEN_READ = re.compile(
    r"^/v1/route/[^/]+/("
    r"hud_data"          # rlog extraction, seconds of CPU per segment
    r"|frame_times/"     # rlog parse per segment
    r"|signals/"         # rlog parse per segment
    r"|download"         # tars whole-route media
    r")"
)


def is_onroad() -> bool:
    """True when openpilot reports the vehicle is driving."""
    return read_param("IsOnroad") == "1"


@web.middleware
async def onroad_guard_middleware(request, handler):
    blocked = (
        (request.method in _MUTATING and _ONROAD_FORBIDDEN.match(request.path))
        or (request.method in ("GET", "HEAD") and _ONROAD_FORBIDDEN_READ.match(request.path))
    )
    if blocked and is_onroad():
        logger.warning("Refused %s %s — vehicle is onroad", request.method, request.path)
        return web.json_response(
            {"error": "Vehicle is driving — this action is unavailable onroad",
             "isOnroad": True},
            status=409,
        )
    return await handler(request)
