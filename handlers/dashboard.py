"""Dashboard telemetry handlers — REST replay + WebSocket live streaming."""

import asyncio
import json
import logging
import threading
from pathlib import Path

from aiohttp import web

from handler_helpers import error_response, resolve_route_name
from log_parser import extract_dashboard_telemetry

logger = logging.getLogger("connect.dashboard")


async def handle_dashboard_telemetry(request):
    """GET /v1/dashboard/telemetry/{routeName}/{segments}

    Extract dashboard telemetry from rlog files for the given route and segments.
    Segments can be a single number "3", a range "0-5", or comma-separated "0,1,3".
    Returns JSON array of telemetry samples at ~5Hz.
    """
    route_name = resolve_route_name(request)
    seg_str = request.match_info["segments"]
    store = request.app["store"]

    route = store.get_route(route_name)
    if not route:
        return error_response(f"Route {route_name} not found", 404)

    # Parse segment specification
    seg_nums = []
    for part in seg_str.split(","):
        part = part.strip()
        if "-" in part:
            lo, hi = part.split("-", 1)
            seg_nums.extend(range(int(lo), int(hi) + 1))
        else:
            seg_nums.append(int(part))

    # Resolve local_id for filesystem paths (dirs are {local_id}--{seg})
    local_id = store.get_local_id(route_name)
    if not local_id:
        return error_response(f"Cannot resolve local_id for {route_name}", 404)

    # Find log paths for requested segments (prefer qlog for speed)
    data_dir = store.data_dir
    seg_log_pairs = []  # [(seg_num, path), ...]
    for seg in sorted(seg_nums):
        seg_dir = data_dir / f"{local_id}--{seg}"
        for name in ("qlog.zst", "qlog", "rlog.zst", "rlog"):
            p = seg_dir / name
            if p.is_file():
                seg_log_pairs.append((seg, str(p)))
                break

    if not seg_log_pairs:
        return error_response("No log files found for requested segments", 404)

    # Run extraction in executor to avoid blocking event loop
    loop = asyncio.get_event_loop()
    samples = await loop.run_in_executor(None, extract_dashboard_telemetry, seg_log_pairs)

    return web.json_response(samples)


def _sm_poller(latest, stop_event):
    """Background thread: poll cereal SubMaster and stash latest telemetry dict.

    Runs sm.update() (blocking) in its own thread so the asyncio event loop
    is never blocked. The async WebSocket sender reads from `latest` dict.
    """
    import cereal.messaging as messaging

    sm = messaging.SubMaster([
        "carState", "carControl", "selfdriveState", "peripheralState",
    ])

    while not stop_event.is_set():
        sm.update(200)  # blocks up to 200ms — safe in this thread

        if not sm.updated["carState"]:
            continue

        cs = sm["carState"]
        cc = sm["carControl"]
        sd = sm["selfdriveState"]
        ps = sm["peripheralState"]

        latest["msg"] = {
            "t": round(sm.logMonoTime["carState"] / 1e9, 3),
            "coolantTemp": round(float(getattr(cs, "coolantTemp", 0)), 1),
            "oilTemp": round(float(getattr(cs, "oilTemp", 0)), 1),
            "vEgo": round(float(cs.vEgo), 3),
            "steeringAngleDeg": round(float(cs.steeringAngleDeg), 2),
            "gasPressed": bool(cs.gasPressed),
            "brakePressed": bool(cs.brakePressed),
            "cruiseSpeed": round(float(cs.cruiseState.speed), 2),
            "cruiseEnabled": bool(cs.cruiseState.enabled),
            "steerCmd": round(float(cc.actuators.steer), 4),
            "accelCmd": round(float(cc.actuators.accel), 4),
            "sdState": str(sd.state),
            "sdEnabled": bool(sd.enabled),
            "voltage": round(float(ps.voltage), 2),
        }


async def handle_dashboard_ws(request):
    """GET /ws/dashboard — WebSocket live telemetry at 5Hz.

    Subscribes to cereal SubMaster in a background thread (sm.update is blocking)
    and streams dashboard telemetry as JSON over WebSocket.
    Falls back to mock data if cereal is unavailable.
    """
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    logger.info("Dashboard WebSocket connected")

    try:
        import cereal.messaging  # noqa: F401 — probe availability

        latest = {"msg": None}
        stop_event = threading.Event()
        poller = threading.Thread(target=_sm_poller, args=(latest, stop_event), daemon=True)
        poller.start()
        logger.info("cereal available — SubMaster poller started")

        last_sent = None
        try:
            while not ws.closed:
                msg = latest["msg"]
                if msg is not None and msg is not last_sent:
                    await ws.send_str(json.dumps(msg))
                    last_sent = msg
                await asyncio.sleep(0.2)  # 5Hz
        finally:
            stop_event.set()
            poller.join(timeout=1)

    except ImportError:
        # Mock telemetry for testing UI when cereal is unavailable
        import math, time as _time
        logger.info("cereal not available — streaming mock telemetry")
        t0 = _time.time()
        while not ws.closed:
            t = _time.time() - t0
            msg = {
                "t": round(t, 3),
                "coolantTemp": round(80 + 10 * math.sin(t / 60), 1),
                "oilTemp": round(90 + 15 * math.sin(t / 80), 1),
                "vEgo": round(max(0, 22 + 8 * math.sin(t / 15)), 3),
                "steeringAngleDeg": round(30 * math.sin(t / 5), 2),
                "gasPressed": False,
                "brakePressed": t % 20 > 18,
                "cruiseSpeed": round(30 / 3.6, 2),
                "cruiseEnabled": True,
                "steerCmd": round(0.3 * math.sin(t / 5), 4),
                "accelCmd": round(0.5 * math.sin(t / 10), 4),
                "sdState": "enabled",
                "sdEnabled": True,
                "voltage": round(13.8 + 0.3 * math.sin(t / 30), 2),
            }
            await ws.send_str(json.dumps(msg))
            await asyncio.sleep(0.2)
    except Exception as e:
        if not ws.closed:
            logger.warning("Dashboard WebSocket error: %s", e)
    finally:
        logger.info("Dashboard WebSocket disconnected")

    return ws
