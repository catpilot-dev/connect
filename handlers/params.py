import json
import logging
import sys

from aiohttp import web

from config import OPENPILOT_DIR as _OPENPILOT_DIR, PARAMS_DIR
from handler_helpers import error_response, parse_json, read_param, write_param, read_plugin_param

logger = logging.getLogger("connect")


async def handle_lateral_delay(request: web.Request) -> web.Response:
    """GET /v1/lateral-delay — read LiveDelay capnp param."""
    path = f"{PARAMS_DIR}/LiveDelay"
    try:
        raw = open(path, "rb").read()
    except FileNotFoundError:
        return web.json_response({"status": "no data"})

    try:
        sys.path.insert(0, _OPENPILOT_DIR) if _OPENPILOT_DIR not in sys.path else None
        from cereal import log
        with log.Event.from_bytes(raw) as msg:
            ld = msg.liveDelay
            return web.json_response({
                "lateralDelay": round(ld.lateralDelay, 4),
                "lateralDelayEstimate": round(ld.lateralDelayEstimate, 4),
                "lateralDelayEstimateStd": round(ld.lateralDelayEstimateStd, 6),
                "validBlocks": ld.validBlocks,
                "status": str(ld.status),
                "calPerc": ld.calPerc,
            })
    except Exception as e:
        logger.warning("Failed to decode LiveDelay: %s", e)
        return error_response(str(e), 500)


# ── Toggles panel ─────────────────────────────────────────────

TOGGLE_PARAMS = [
    "OpenpilotEnabledToggle", "ExperimentalMode",
    "DisengageOnAccelerator", "IsLdwEnabled", "AlwaysOnDM",
    "RecordFront", "RecordAudio", "IsMetric", "LongitudinalPersonality",
    # Cat-eye headless mode
    "CatEyePhoneRequired",
    # Developer toggles
    "AdbEnabled", "SshEnabled",
    "JoystickDebugMode", "LongitudinalManeuverMode",
    "AlphaLongitudinalEnabled",
]

# Mutual exclusion pairs — toggling one ON turns the other OFF
_TOGGLE_MUTEX = {
    "JoystickDebugMode": "LongitudinalManeuverMode",
    "LongitudinalManeuverMode": "JoystickDebugMode",
}

async def handle_toggles_get(request: web.Request) -> web.Response:
    """GET /v1/toggles — read all toggle params."""
    result = {}
    for key in TOGGLE_PARAMS:
        raw = read_param(key)
        if key == "LongitudinalPersonality":
            result[key] = int(raw) if raw else 2  # default: Relaxed
        else:
            result[key] = raw == "1"
    return web.json_response(result)


async def handle_toggles_set(request: web.Request) -> web.Response:
    """POST /v1/toggles — set a toggle param."""
    body = await parse_json(request)
    key = body.get("key", "")
    value = body.get("value")
    if key not in TOGGLE_PARAMS:
        raise web.HTTPBadRequest(text=json.dumps({"error": f"Unknown toggle: {key}"}))
    try:
        if key == "LongitudinalPersonality":
            write_param(key, str(int(value)))
        else:
            write_param(key, "1" if value else "0")
        # Mutual exclusion: if turning one on, turn the other off
        if value and key in _TOGGLE_MUTEX:
            write_param(_TOGGLE_MUTEX[key], "0")
    except Exception as e:
        return error_response(str(e), 500)
    return web.json_response({"status": "ok", "key": key, "value": value})


PARAMS = {
    "LongitudinalPersonality": {"type": "int", "label": "Driving Personality", "default": 2},
}

MAPD_PARAM_KEYS = {"MapdSpeedLimitControlEnabled", "MapdSpeedLimitOffsetPercent", "MapdCurveTargetLatAccel"}

OFFSET_VALUES = [0, 5, 10, 15]            # indexed by pill selection
LAT_ACCEL_VALUES = [1.5, 2.0, 2.5, 3.0]   # indexed by pill selection


def update_mapd_settings():
    """Regenerate MapdSettings JSON from plugin params (snake_case keys for mapd Go daemon).

    Reads from /data/plugins-runtime/speedlimitd/data/, writes to /data/params/d/MapdSettings.
    """
    enabled = read_plugin_param("speedlimitd", "MapdSpeedLimitControlEnabled") == "1"

    try:
        offset_idx = int(read_plugin_param("speedlimitd", "MapdSpeedLimitOffsetPercent", "2"))
    except ValueError:
        offset_idx = 2
    offset_pct = OFFSET_VALUES[offset_idx] if 0 <= offset_idx < len(OFFSET_VALUES) else 10

    try:
        lat_idx = int(read_plugin_param("speedlimitd", "MapdCurveTargetLatAccel", "0"))
    except ValueError:
        lat_idx = 0
    lat_accel = LAT_ACCEL_VALUES[lat_idx] if 0 <= lat_idx < len(LAT_ACCEL_VALUES) else 1.5

    settings = {
        "speed_limit_control_enabled": enabled,
        "map_curve_speed_control_enabled": enabled,
        "vision_curve_speed_control_enabled": enabled,
        "speed_limit_offset": offset_pct / 100.0,
        "map_curve_target_lat_a": lat_accel,
        "vision_curve_target_lat_a": lat_accel,
    }

    write_param("MapdSettings", json.dumps(settings))


async def handle_params_get(request: web.Request) -> web.Response:
    """GET /v1/params — read openpilot params from /data/params/d/."""
    result = {}
    for key, meta in PARAMS.items():
        raw = read_param(key)
        if meta["type"] == "bool":
            result[key] = raw == "1"
        elif meta["type"] == "int":
            result[key] = int(raw) if raw else meta.get("default", 0)
        else:
            result[key] = raw
    return web.json_response(result)


async def handle_params_set(request: web.Request) -> web.Response:
    """POST /v1/params — set a single param {key, value}"""
    body = await request.json()
    key = body.get("key")
    value = body.get("value")
    if key not in PARAMS:
        raise web.HTTPBadRequest(text=json.dumps({"error": f"Unknown param: {key}"}))
    meta = PARAMS[key]
    if meta["type"] == "bool":
        raw = "1" if value else "0"
    else:
        raw = str(int(value))
    write_param(key, raw)
    if key in MAPD_PARAM_KEYS:
        update_mapd_settings()
    return web.json_response({"status": "ok", "key": key, "value": value})
