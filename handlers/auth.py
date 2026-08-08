import asyncio
import json
import logging
import time

from aiohttp import web

from handler_helpers import error_response, parse_json, read_param, write_param
from route_store import _route_counter
from storage_management import get_storage_info

logger = logging.getLogger("connect")


async def handle_me(request: web.Request) -> web.Response:
    """GET /v1/me/ — user profile (static)"""
    return web.json_response({
        "id": "local",
        "email": "local@device",
        "prime": False,
        "regdate": 0,
        "superuser": False,
        "user_id": "local",
        "username": "local",
    })


async def handle_auth(request: web.Request) -> web.Response:
    """POST /v2/auth/ — return a dummy token"""
    return web.json_response({"access_token": "local-device-token"})


# ─── Device handlers ──────────────────────────────────────────────────

def _device_dict(store) -> dict:
    # Find device_type from most recent enriched route
    device_type = None
    for meta in store._metadata.values():
        dt = meta.get("device_type")
        if dt:
            device_type = dt
            break

    DEVICE_TYPE_MAP = {"tici": "three", "tize": "threex", "mici": "four"}

    return {
        "alias": None,
        "athena_host": None,
        "device_type": DEVICE_TYPE_MAP.get(device_type, "three"),
        "device_type_raw": device_type,
        "agnos_version": store._agnos_version,
        "dongle_id": store.dongle_id,
        "eligible_features": {"prime": False, "prime_data": False},
        "ignore_uploads": None,
        "is_paired": True,
        "is_owner": True,
        "last_athena_ping": 0,
        "openpilot_version": None,
        "prime": False,
        "prime_type": 0,
        "public_key": "",
        "serial": "local",
        "sim_id": None,
        "sim_type": None,
        "trial_claimed": False,
    }


async def handle_devices(request: web.Request) -> web.Response:
    """GET /v1/me/devices/ — list devices (use cached scan, rescan only if stale)"""
    store = request.app["store"]
    await store.async_scan()
    return web.json_response([_device_dict(store)])


async def handle_device_get(request: web.Request) -> web.Response:
    """GET /v1.1/devices/{dongleId}/ — single device"""
    store = request.app["store"]
    await store.async_scan()
    return web.json_response(_device_dict(store))


async def handle_device_stats(request: web.Request) -> web.Response:
    """GET /v1.1/devices/{dongleId}/stats — GPS-anchored driving statistics.

    Serves the aggregate materialized by RouteStore._compute_stats (recomputed
    when routes change / a drive is POSTed). Does NOT read the system wall clock:
    on AGNOS it is build-time-seeded and unreliable.
    """
    store = request.app["store"]
    await store.async_scan()

    # Fallback: brief-enrich routes still missing a GPS time (device had no
    # lock, or an old route predating device-supplied gps_time). Skipped while
    # onroad to avoid competing with openpilot. Uses qlog GPS time — never the
    # wall clock.
    if not store._is_onroad():
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, store.enrich_new_routes)

    stats = store._stats
    if not stats.get("all"):
        stats = store._compute_stats()
    return web.json_response({"all": stats["all"], "week": stats["week"]})


async def handle_device_location(request: web.Request) -> web.Response:
    """GET /v1/devices/{dongleId}/location — last known GPS"""
    store = request.app["store"]
    routes = await store.async_scan()

    # Find most recent route with GPS (sort by route counter, not create_time)
    for r in sorted(routes.values(), key=lambda x: _route_counter(x.get("_local_id", "")), reverse=True):
        if r.get("start_lat"):
            return web.json_response({
                "dongle_id": store.dongle_id,
                "lat": r["start_lat"],
                "lng": r["start_lng"],
                "time": r["create_time"],
            })

    raise web.HTTPNotFound(text=json.dumps({"error": "No GPS data"}))


async def handle_storage(request: web.Request) -> web.Response:
    """GET /v1/storage — disk usage and route management stats"""
    store = request.app["store"]
    return web.json_response(get_storage_info(store))


# ─── Device params & control ─────────────────────────────────────────

DEVICE_PARAMS = ["DongleId", "HardwareSerial", "LanguageSetting"]

async def handle_device_info(request: web.Request) -> web.Response:
    """GET /v1/device — read device identity and language setting."""
    result = {}
    for key in DEVICE_PARAMS:
        val = read_param(key)
        result[key] = val if val else None
    return web.json_response(result)


async def handle_device_is_onroad(request: web.Request) -> web.Response:
    """GET /v1/device/isOnroad — check if vehicle is currently driving."""
    val = read_param("IsOnroad")
    return web.json_response({"isOnroad": val == "1"})


async def handle_device_reboot(request: web.Request) -> web.Response:
    """POST /v1/device/reboot — trigger device reboot."""
    try:
        write_param("DoReboot", "1")
    except Exception as e:
        logger.error("Failed to set DoReboot: %s", e)
        return error_response(str(e), 500)
    return web.json_response({"status": "rebooting"})


async def handle_device_poweroff(request: web.Request) -> web.Response:
    """POST /v1/device/poweroff — trigger device shutdown."""
    try:
        write_param("DoShutdown", "1")
    except Exception as e:
        logger.error("Failed to set DoShutdown: %s", e)
        return error_response(str(e), 500)
    return web.json_response({"status": "shutting_down"})


async def handle_device_language(request: web.Request) -> web.Response:
    """POST /v1/device/language — set LanguageSetting param."""
    body = await parse_json(request)
    lang = body.get("language", "").strip()
    if not lang:
        raise web.HTTPBadRequest(text=json.dumps({"error": "Missing language"}))
    try:
        write_param("LanguageSetting", lang)
    except Exception as e:
        return error_response(str(e), 500)
    return web.json_response({"status": "ok", "language": lang})
