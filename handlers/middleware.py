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


def is_onroad() -> bool:
    """True when openpilot reports the vehicle is driving."""
    return read_param("IsOnroad") == "1"


@web.middleware
async def onroad_guard_middleware(request, handler):
    if request.method in _MUTATING and _ONROAD_FORBIDDEN.match(request.path):
        if is_onroad():
            logger.warning("Refused %s %s — vehicle is onroad", request.method, request.path)
            return web.json_response(
                {"error": "Vehicle is driving — this action is unavailable onroad",
                 "isOnroad": True},
                status=409,
            )
    return await handler(request)
