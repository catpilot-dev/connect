"""Driving page WebSocket — streams telemetry + model data for HUD overlay.

Uses the same projection algorithm as catpilot's raylib UI (model_renderer.py):
  car_space_transform = video_transform @ intrinsic @ view_frame_from_device @ device_from_calib

All model points are projected server-side to normalized screen coordinates (0-1).
The browser Canvas just draws pre-projected polygons — no projection math in JS.
"""

import asyncio
import json
import logging
import threading

import numpy as np
from aiohttp import web

logger = logging.getLogger("connect.driving")

# ── Projection constants (from catpilot common/transformations/camera.py) ──

# device frame: x->forward, y->right, z->down
# view frame:   x->right,   y->down,  z->forward
_VIEW_FROM_DEVICE = np.array([
    [0., 0., 1.],
    [1., 0., 0.],
    [0., 1., 0.],
]).T  # = device_frame_from_view_frame.T

MIN_DRAW_DISTANCE = 10.0
MAX_DRAW_DISTANCE = 100.0

# Height offset from calibration (default 1.22m)
HEIGHT_INIT = 1.22


def _rot_from_euler(rpy):
    """Rotation matrix from roll/pitch/yaw (from common/transformations/orientation.py)."""
    roll, pitch, yaw = float(rpy[0]), float(rpy[1]), float(rpy[2])
    cr, sr = np.cos(roll), np.sin(roll)
    cp, sp = np.cos(pitch), np.sin(pitch)
    cy, sy = np.cos(yaw), np.sin(yaw)
    return np.array([
        [cp * cy, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
        [cp * sy, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
        [-sp,     cp * sr,                cp * cr],
    ])


def _build_car_space_transform(intrinsic, calib_rpy, canvas_w, canvas_h):
    """Build the 3x3 car-space → canvas-space transform matrix.

    Same algorithm as augmented_road_view.py _calc_frame_matrix:
      calib_transform = intrinsic @ view_from_device @ device_from_calib
      video_transform = zoom/offset to center on vanishing point
      final = video_transform @ calib_transform

    Output is in canvas pixel coordinates (0..canvas_w, 0..canvas_h).
    """
    device_from_calib = _rot_from_euler(calib_rpy)
    view_from_calib = _VIEW_FROM_DEVICE @ device_from_calib
    calib_transform = intrinsic @ view_from_calib

    # Vanishing point (limit of [x,0,0] as x→∞)
    kep = calib_transform @ np.array([1.0, 0.0, 0.0])

    cx, cy = intrinsic[0, 2], intrinsic[1, 2]
    zoom = 1.1  # fcam zoom (same as stock UI)

    # Clamp vanishing point offset
    margin = 5
    max_x_off = cx * zoom - canvas_w / 2 - margin
    max_y_off = cy * zoom - canvas_h / 2 - margin
    if abs(kep[2]) > 1e-6:
        x_off = np.clip((kep[0] / kep[2] - cx) * zoom, -max_x_off, max_x_off)
        y_off = np.clip((kep[1] / kep[2] - cy) * zoom, -max_y_off, max_y_off)
    else:
        x_off, y_off = 0.0, 0.0

    video_transform = np.array([
        [zoom, 0.0, (canvas_w / 2 - x_off) - (cx * zoom)],
        [0.0, zoom, (canvas_h / 2 - y_off) - (cy * zoom)],
        [0.0, 0.0, 1.0],
    ])

    return video_transform @ calib_transform


def _map_line_to_polygon(transform, line_3d, y_off, z_off, max_idx, max_distance, canvas_w, canvas_h):
    """Convert 3D line points to 2D polygon (same as ModelRenderer._map_line_to_polygon).

    Returns list of [x, y] in normalized coords (0-1).
    """
    if line_3d.shape[0] == 0:
        return []

    points = line_3d[:max_idx + 1]

    # Interpolate at max_distance for smooth endpoint
    if 0 < max_idx < line_3d.shape[0] - 1:
        p0, p1 = line_3d[max_idx], line_3d[max_idx + 1]
        x0, x1 = p0[0], p1[0]
        if x1 > x0:
            interp_y = np.interp(max_distance, [x0, x1], [p0[1], p1[1]])
            interp_z = np.interp(max_distance, [x0, x1], [p0[2], p1[2]])
            points = np.vstack([points, [max_distance, interp_y, interp_z]])

    points = points[points[:, 0] >= 0]
    if points.shape[0] == 0:
        return []

    N = points.shape[0]
    offsets = np.array([[0, -y_off, z_off], [0, y_off, z_off]], dtype=np.float32)
    points_3d = points[None, :, :] + offsets[:, None, :]  # 2 x N x 3
    points_3d = points_3d.reshape(2 * N, 3)

    proj = transform @ points_3d.T  # 3 x 2N
    proj = proj.reshape(3, 2, N)
    left_proj = proj[:, 0, :]
    right_proj = proj[:, 1, :]

    valid = (np.abs(left_proj[2]) >= 1e-6) & (np.abs(right_proj[2]) >= 1e-6)
    if not np.any(valid):
        return []

    left_screen = left_proj[:2, valid] / left_proj[2, valid][None, :]
    right_screen = right_proj[:2, valid] / right_proj[2, valid][None, :]

    # Clip to canvas bounds
    in_clip = (
        (left_screen[0] >= -100) & (left_screen[0] <= canvas_w + 100) &
        (left_screen[1] >= -100) & (left_screen[1] <= canvas_h + 100) &
        (right_screen[0] >= -100) & (right_screen[0] <= canvas_w + 100) &
        (right_screen[1] >= -100) & (right_screen[1] <= canvas_h + 100)
    )
    if not np.any(in_clip):
        return []

    left_screen = left_screen[:, in_clip]
    right_screen = right_screen[:, in_clip]

    # Polygon: left edge forward, right edge backward (same as stock)
    polygon = np.vstack((left_screen.T, right_screen[:, ::-1].T))

    # Normalize to 0-1
    polygon[:, 0] /= canvas_w
    polygon[:, 1] /= canvas_h

    return [[round(float(p[0]), 4), round(float(p[1]), 4)] for p in polygon]


def _map_point_to_screen(transform, x, y, z, canvas_w, canvas_h):
    """Project single 3D point to normalized screen coords (0-1)."""
    pt = transform @ np.array([x, y, z])
    if abs(pt[2]) < 1e-6:
        return None
    sx, sy = pt[0] / pt[2], pt[1] / pt[2]
    if sx < -100 or sx > canvas_w + 100 or sy < -100 or sy > canvas_h + 100:
        return None
    return [round(float(sx / canvas_w), 4), round(float(sy / canvas_h), 4)]


def _get_path_length_idx(pos_x, distance):
    """Get index corresponding to given path distance."""
    if len(pos_x) == 0:
        return 0
    idx = np.where(pos_x <= distance)[0]
    return idx[-1] if idx.size > 0 else 0


# ── Camera intrinsics (C3 AR0231 narrow cam) ──
# From common/transformations/camera.py: _ar_ox_config fcam
C3_FCAM_INTRINSIC = np.array([
    [2648.0, 0.0, 964.0],
    [0.0, 2648.0, 604.0],
    [0.0, 0.0, 1.0],
])

# Virtual canvas size for projection (matches stock UI aspect ratio)
CANVAS_W = 1928.0
CANVAS_H = 1208.0


def _driving_poller(latest, stop_event):
    """Background thread: poll cereal SubMaster for driving telemetry + model data.

    Uses stock catpilot projection algorithm (model_renderer.py) to produce
    pre-projected 2D polygons. Browser Canvas just draws them.
    """
    import cereal.messaging as messaging

    sm = messaging.SubMaster([
        "carState", "carControl", "selfdriveState",
        "modelV2", "radarState", "liveCalibration",
    ])

    car_space_transform = None
    path_offset_z = HEIGHT_INIT

    while not stop_event.is_set():
        sm.update(200)

        # Update calibration → rebuild transform matrix
        if sm.updated["liveCalibration"]:
            calib = sm["liveCalibration"]
            if len(calib.rpyCalib) == 3:
                car_space_transform = _build_car_space_transform(
                    C3_FCAM_INTRINSIC, calib.rpyCalib, CANVAS_W, CANVAS_H,
                )
                if calib.height:
                    path_offset_z = float(calib.height[0])

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
                    "steerCmd": round(float(cc.actuators.steeringAngleDeg), 4),
                    "accelCmd": round(float(cc.actuators.accel), 4),
                    "sdState": str(sd.state),
                    "sdEnabled": bool(sd.enabled),
                    "alertText1": str(sd.alertText1),
                    "alertText2": str(sd.alertText2),
                    "alertType": str(sd.alertType),
                },
            }

        # Model update — project to 2D polygons using stock algorithm
        if sm.updated["modelV2"] and car_space_transform is not None:
            model = sm["modelV2"]
            radar = sm["radarState"]

            try:
                # Extract raw 3D points (same as ModelRenderer._update_raw_points)
                path_3d = np.array([model.position.x, model.position.y, model.position.z], dtype=np.float32).T
                lane_lines_3d = []
                for ll in model.laneLines:
                    lane_lines_3d.append(np.array([ll.x, ll.y, ll.z], dtype=np.float32).T)

                lane_probs = [round(float(p), 3) for p in model.laneLineProbs]

                road_edges_3d = []
                for re in model.roadEdges:
                    road_edges_3d.append(np.array([re.x, re.y, re.z], dtype=np.float32).T)
                road_edge_stds = [round(float(s), 3) for s in model.roadEdgeStds]

                # Compute max draw distance
                if path_3d.shape[0] > 0:
                    max_distance = float(np.clip(path_3d[-1, 0], MIN_DRAW_DISTANCE, MAX_DRAW_DISTANCE))
                else:
                    max_distance = MAX_DRAW_DISTANCE

                max_idx = _get_path_length_idx(
                    lane_lines_3d[0][:, 0] if lane_lines_3d else np.array([]),
                    max_distance,
                )

                # Project lane lines to 2D polygons
                lane_polygons = []
                for i, ll in enumerate(lane_lines_3d):
                    poly = _map_line_to_polygon(
                        car_space_transform, ll,
                        0.025 * lane_probs[i] if i < len(lane_probs) else 0.0,
                        0.0, max_idx, max_distance, CANVAS_W, CANVAS_H,
                    )
                    lane_polygons.append(poly)

                # Project road edges
                edge_polygons = []
                for re in road_edges_3d:
                    poly = _map_line_to_polygon(
                        car_space_transform, re,
                        0.025, 0.0, max_idx, max_distance, CANVAS_W, CANVAS_H,
                    )
                    edge_polygons.append(poly)

                # Project path — shorten if lead car present
                path_max = max_distance
                lead_data = None
                if radar and radar.leadOne and radar.leadOne.status:
                    ld = radar.leadOne
                    lead_d = float(ld.dRel) * 2.0
                    path_max = float(np.clip(lead_d - min(lead_d * 0.35, 10.0), 0.0, max_distance))

                    # Project lead car position
                    idx = _get_path_length_idx(path_3d[:, 0], float(ld.dRel))
                    z = float(path_3d[idx, 2]) if idx < len(path_3d) else 0.0
                    lead_pt = _map_point_to_screen(
                        car_space_transform,
                        float(ld.dRel), -float(ld.yRel), z + path_offset_z,
                        CANVAS_W, CANVAS_H,
                    )
                    if lead_pt:
                        lead_data = {
                            "pt": lead_pt,
                            "dRel": round(float(ld.dRel), 1),
                            "vRel": round(float(ld.vRel), 1),
                        }

                path_idx = _get_path_length_idx(path_3d[:, 0], path_max)
                path_polygon = _map_line_to_polygon(
                    car_space_transform, path_3d,
                    0.9, path_offset_z, path_idx, path_max, CANVAS_W, CANVAS_H,
                )

                latest["model"] = {
                    "type": "model",
                    "data": {
                        "laneLines": lane_polygons,
                        "laneLineProbs": lane_probs,
                        "roadEdges": edge_polygons,
                        "roadEdgeStds": road_edge_stds,
                        "path": path_polygon,
                        "lead": lead_data,
                    },
                }
            except Exception as e:
                logger.debug("Model projection error: %s", e)


async def handle_driving_ws(request):
    """GET /ws/driving — WebSocket for driving page telemetry + model data.

    Streams JSON messages at ~10Hz:
    - {"type": "telemetry", "data": {vEgo, steeringAngleDeg, ...}}
    - {"type": "model", "data": {laneLines, path, lead}} — pre-projected 2D polygons
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
