"""Driving page WebSocket — streams telemetry + model data for HUD overlay."""

import asyncio
import json
import logging
import threading

from aiohttp import web

logger = logging.getLogger("connect.driving")


def _driving_poller(latest, stop_event):
    """Background thread: poll cereal SubMaster for driving telemetry + model data.

    Produces two message types:
    - {"type": "telemetry", "data": {...}}  — carState/selfdriveState at ~10Hz
    - {"type": "model", "data": {...}}      — modelV2 lane/path/lead at ~10Hz
    """
    import cereal.messaging as messaging

    sm = messaging.SubMaster([
        "carState", "carControl", "selfdriveState",
        "modelV2", "radarState", "liveCalibration",
    ])

    while not stop_event.is_set():
        sm.update(200)

        # Telemetry update (on carState)
        if sm.updated["carState"]:
            cs = sm["carState"]
            cc = sm["carControl"]
            sd = sm["selfdriveState"]

            latest["telemetry"] = {
                "type": "telemetry",
                "data": {
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
                    "alertText1": str(sd.alertText1),
                    "alertText2": str(sd.alertText2),
                    "alertType": str(sd.alertType),
                },
            }

        # Model update (lane lines, path, lead car)
        if sm.updated["modelV2"]:
            model = sm["modelV2"]
            radar = sm["radarState"]

            # Extract lane lines as normalized canvas coords
            # modelV2 lane lines are arrays of y-values at fixed x distances
            lane_lines = []
            lane_probs = []
            try:
                for i, ll in enumerate(model.laneLines):
                    points = []
                    for j, y_val in enumerate(ll.y):
                        # x = distance ahead (model coordinates)
                        # Map to canvas: x_canvas = 0.5 + y/fov, y_canvas = 1 - dist/max_dist
                        x_dist = ll.x[j] if j < len(ll.x) else j * 1.0
                        if x_dist < 1.0 or x_dist > 100.0:
                            continue
                        cx = 0.5 + float(y_val) / 40.0   # lateral offset → canvas x
                        cy = 1.0 - float(x_dist) / 80.0  # distance → canvas y (perspective)
                        if 0.0 <= cx <= 1.0 and 0.0 <= cy <= 1.0:
                            points.append({"x": round(cx, 4), "y": round(cy, 4)})
                    lane_lines.append(points)
                    prob = float(model.laneLineProbs[i]) if i < len(model.laneLineProbs) else 0.0
                    lane_probs.append(round(prob, 3))
            except Exception:
                pass

            # Extract path prediction
            path = []
            try:
                pos = model.position
                for j in range(min(len(pos.x), 33)):
                    x_dist = pos.x[j]
                    y_off = pos.y[j] if j < len(pos.y) else 0.0
                    if x_dist < 1.0:
                        continue
                    cx = 0.5 + float(y_off) / 40.0
                    cy = 1.0 - float(x_dist) / 80.0
                    if 0.0 <= cy <= 1.0:
                        path.append({
                            "x": round(cx, 4),
                            "y": round(cy, 4),
                            "width": round(max(0.01, 0.08 * (1.0 - cy)), 4),
                        })
            except Exception:
                pass

            # Lead car
            lead = None
            try:
                rl = radar.leadOne
                if rl.status and rl.dRel > 0:
                    cx = 0.5 + float(rl.yRel) / 40.0
                    cy = 1.0 - float(rl.dRel) / 80.0
                    lead = {
                        "x": round(cx, 4),
                        "y": round(max(0.0, cy), 4),
                        "dRel": round(float(rl.dRel), 1),
                        "vRel": round(float(rl.vRel), 1),
                    }
            except Exception:
                pass

            latest["model"] = {
                "type": "model",
                "data": {
                    "laneLines": lane_lines,
                    "laneLineProbs": lane_probs,
                    "path": path,
                    "lead": lead,
                },
            }


async def handle_driving_ws(request):
    """GET /ws/driving — WebSocket for driving page telemetry + model data.

    Streams JSON messages at ~10Hz:
    - {"type": "telemetry", "data": {vEgo, steeringAngleDeg, ...}}
    - {"type": "model", "data": {laneLines, path, lead}}
    """
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    logger.info("Driving WebSocket connected")

    try:
        import cereal.messaging  # noqa: F401

        latest = {"telemetry": None, "model": None}
        stop_event = threading.Event()
        poller = threading.Thread(target=_driving_poller, args=(latest, stop_event), daemon=True)
        poller.start()

        last_tel = None
        last_model = None
        try:
            while not ws.closed:
                tel = latest["telemetry"]
                if tel is not None and tel is not last_tel:
                    await ws.send_str(json.dumps(tel))
                    last_tel = tel

                mdl = latest["model"]
                if mdl is not None and mdl is not last_model:
                    await ws.send_str(json.dumps(mdl))
                    last_model = mdl

                await asyncio.sleep(0.1)  # 10Hz
        finally:
            stop_event.set()
            poller.join(timeout=1)

    except ImportError:
        # Mock data when cereal unavailable
        import math
        import time as _time
        logger.info("cereal not available — streaming mock driving data")
        t0 = _time.time()
        while not ws.closed:
            t = _time.time() - t0
            msg = {
                "type": "telemetry",
                "data": {
                    "vEgo": round(max(0, 22 + 8 * math.sin(t / 15)), 3),
                    "steeringAngleDeg": round(30 * math.sin(t / 5), 2),
                    "gasPressed": False,
                    "brakePressed": False,
                    "cruiseSpeed": round(30 / 3.6, 2),
                    "cruiseEnabled": True,
                    "steerCmd": 0.0,
                    "accelCmd": 0.0,
                    "sdState": "enabled",
                    "sdEnabled": True,
                    "alertText1": "",
                    "alertText2": "",
                    "alertType": "",
                },
            }
            await ws.send_str(json.dumps(msg))
            await asyncio.sleep(0.1)

    except Exception as e:
        if not ws.closed:
            logger.warning("Driving WebSocket error: %s", e)
    finally:
        logger.info("Driving WebSocket disconnected")

    return ws
