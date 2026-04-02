"""Home page WebSocket — streams device status for offroad home screen.

Sends deviceState (network, thermals, CPU/GPU usage) + GPS accuracy at ~2Hz.
"""

import asyncio
import json
import logging
import threading

from aiohttp import web

logger = logging.getLogger("connect.home")

_NETWORK_TYPE_MAP = {
    0: "none",
    1: "wifi",
    2: "cell2G",
    3: "cell3G",
    4: "cell4G",
    5: "cell5G",
    6: "ethernet",
}

_NETWORK_STRENGTH_MAP = {
    0: "unknown",
    1: "poor",
    2: "moderate",
    3: "good",
    4: "great",
}

_THERMAL_STATUS_MAP = {
    0: "green",
    1: "yellow",
    2: "red",
    3: "danger",
}


def _home_poller(latest, stop_event):
    """Background thread: poll deviceState + gpsLocationExternal."""
    import cereal.messaging as messaging

    sm = messaging.SubMaster(["deviceState", "gpsLocationExternal"])

    while not stop_event.is_set():
        sm.update(500)

        if sm.updated["deviceState"]:
            ds = sm["deviceState"]

            cpu_temps = list(ds.cpuTempC) if ds.cpuTempC else []
            gpu_temps = list(ds.gpuTempC) if ds.gpuTempC else []
            cpu_usage = list(ds.cpuUsagePercent) if ds.cpuUsagePercent else []

            latest["device"] = {
                "type": "device",
                "networkType": _NETWORK_TYPE_MAP.get(ds.networkType.raw, "none"),
                "networkStrength": _NETWORK_STRENGTH_MAP.get(ds.networkStrength.raw, "unknown"),
                "thermalStatus": _THERMAL_STATUS_MAP.get(ds.thermalStatus.raw, "green"),
                "maxTempC": round(float(ds.maxTempC), 1),
                "cpuTempC": round(float(cpu_temps[0]) if cpu_temps else 0.0, 1),
                "gpuTempC": round(float(gpu_temps[0]) if gpu_temps else 0.0, 1),
                "memoryTempC": round(float(ds.memoryTempC), 1),
                "cpuUsagePct": round(float(sum(cpu_usage) / len(cpu_usage)) if cpu_usage else 0.0, 1),
                "gpuUsagePct": int(ds.gpuUsagePercent),
                "memoryUsagePct": int(ds.memoryUsagePercent),
                "freeSpacePct": round(float(ds.freeSpacePercent), 1),
                "fanSpeedPct": int(ds.fanSpeedPercentDesired),
                "isOnroad": bool(ds.started),
            }

        if sm.updated["gpsLocationExternal"]:
            gps = sm["gpsLocationExternal"]
            latest["gps"] = {
                "type": "gps",
                "lat": round(float(gps.latitude), 6),
                "lng": round(float(gps.longitude), 6),
                "accuracy": round(float(gps.horizontalAccuracy), 1),
                "speed": round(float(gps.speed), 2),
                "hasFix": bool(gps.hasFix),
                "satellites": int(getattr(gps, 'satelliteCount', 0)),
            }


async def handle_home_ws(request):
    """GET /ws/home — WebSocket for offroad home page device status at 2Hz."""
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    logger.info("Home WebSocket connected")

    try:
        import cereal.messaging  # noqa: F401

        latest = {"device": None, "gps": None}
        stop_event = threading.Event()
        poller = threading.Thread(target=_home_poller, args=(latest, stop_event), daemon=True)
        poller.start()

        last_device = None
        last_gps = None
        try:
            while not ws.closed:
                dev = latest["device"]
                if dev is not None and dev is not last_device:
                    await ws.send_str(json.dumps(dev))
                    last_device = dev

                gps = latest["gps"]
                if gps is not None and gps is not last_gps:
                    await ws.send_str(json.dumps(gps))
                    last_gps = gps

                await asyncio.sleep(0.5)  # 2Hz
        finally:
            stop_event.set()
            poller.join(timeout=1)

    except ImportError:
        import math
        import time as _time
        logger.info("cereal not available — streaming mock home data")
        t0 = _time.time()
        while not ws.closed:
            t = _time.time() - t0
            strengths = ["unknown", "poor", "moderate", "good", "great"]
            msg = {
                "type": "device",
                "networkType": "wifi",
                "networkStrength": strengths[int(t / 5) % 5],
                "thermalStatus": "green",
                "maxTempC": round(45 + 10 * math.sin(t / 30), 1),
                "cpuTempC": round(42 + 8 * math.sin(t / 25), 1),
                "gpuTempC": round(40 + 6 * math.sin(t / 20), 1),
                "memoryTempC": round(38 + 5 * math.sin(t / 35), 1),
                "cpuUsagePct": round(20 + 15 * abs(math.sin(t / 10)), 1),
                "gpuUsagePct": int(10 + 8 * abs(math.sin(t / 12))),
                "memoryUsagePct": 55,
                "freeSpacePct": 40.0,
                "fanSpeedPct": 30,
            }
            await ws.send_str(json.dumps(msg))
            await asyncio.sleep(0.5)

    except Exception as e:
        if not ws.closed:
            logger.warning("Home WebSocket error: %s", e)
    finally:
        logger.info("Home WebSocket disconnected")

    return ws
