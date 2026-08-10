"""HUD overlay data for route replay — extracts telemetry + model frames from qlogs.

Uses the same projection algorithm as catpilot's raylib UI (model_renderer.py):
  car_space_transform = video_transform @ intrinsic @ view_frame_from_device @ device_from_calib

All model points are projected server-side to normalized screen coordinates (0-1).
The browser Canvas just draws pre-projected polygons — no projection math in JS.
"""

import asyncio
import json
import logging
from collections import OrderedDict
from pathlib import Path

import numpy as np
from aiohttp import web

from config import COD_CACHE_DIR

logger = logging.getLogger("connect.hud_data")

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


def _build_car_space_transform(intrinsic, calib_rpy, canvas_w, canvas_h, zoom=1.1):
    """Build the 3x3 car-space → canvas-space transform matrix.

    Same algorithm as augmented_road_view.py _calc_frame_matrix:
      calib_transform = intrinsic @ view_from_device @ device_from_calib
      video_transform = zoom/offset to center on vanishing point
      final = video_transform @ calib_transform

    Output is in canvas pixel coordinates (0..canvas_w, 0..canvas_h).
    zoom: 1.1 for stock UI rendering, 1.0 for raw camera (qcamera overlay).
    """
    device_from_calib = _rot_from_euler(calib_rpy)
    view_from_calib = _VIEW_FROM_DEVICE @ device_from_calib
    calib_transform = intrinsic @ view_from_calib

    # Vanishing point (limit of [x,0,0] as x→∞)
    kep = calib_transform @ np.array([1.0, 0.0, 0.0])

    cx, cy = intrinsic[0, 2], intrinsic[1, 2]

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

    # One vectorized round — a per-vertex Python loop here cost ~0.5 s/segment
    return np.round(polygon, 4).tolist()


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


# (local_id, seg) → (frames, calib_rpy, height), LRU-evicted. Each cached
# segment holds ~300 pre-projected frames (1-2 MB), and the server shares RAM
# with the driving stack, so the cache must stay bounded. Calibration is kept
# per entry so later segments can start from the nearest earlier segment's
# calibration instead of dropping frames until liveCalibration reappears.
_HUD_DATA_CACHE = OrderedDict()
_HUD_CACHE_MAX_SEGMENTS = 32

# Extraction costs seconds per segment and rlogs are immutable, so results
# also persist to disk (same pattern as the frame_times cache) — the memory
# LRU only survives until the next server restart.
HUD_DATA_CACHE_DIR = Path(COD_CACHE_DIR) / "hud_data"

# Bump when the frame schema changes — mismatched disk entries re-extract.
_CACHE_VERSION = 4

# Plugin-bus topics carried into overlay frames (recorded by the bus_logger
# plugin as pluginBusLog events in the rlog).
_BUS_TOPICS = ("speedLimitState", "bmw_temps", "lane_keeping")


def _disk_cache_load(local_id: str, seg: int):
    cache = HUD_DATA_CACHE_DIR / f"{local_id}--{seg}.json"
    if cache.exists():
        try:
            d = json.loads(cache.read_text())
            if d.get("v") == _CACHE_VERSION:
                return d["frames"], d.get("calib"), d.get("height"), d.get("brand")
        except Exception:
            pass
    return None


def _disk_cache_store(local_id: str, seg: int, frames, calib_rpy, height, brand):
    try:
        HUD_DATA_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache = HUD_DATA_CACHE_DIR / f"{local_id}--{seg}.json"
        tmp = cache.with_suffix(".tmp")
        tmp.write_text(json.dumps({"v": _CACHE_VERSION, "frames": frames,
                                   "calib": calib_rpy, "height": height,
                                   "brand": brand}))
        tmp.rename(cache)
    except Exception as e:
        logger.debug("hud_data disk cache write failed for %s--%s: %s", local_id, seg, e)


def _extract_hud_segment(qlog_path: str, seg_num: int, calib_rpy=None, height=None, brand=None):
    """Extract model + telemetry frames from a single qlog segment.

    Returns (frames, calib_rpy, height) where frames is a list of dicts
    with time-indexed telemetry and pre-projected model polygons.
    """
    from log_parser import _iter_log

    car_space_transform = None
    path_offset_z = height or HEIGHT_INIT
    if calib_rpy is not None:
        car_space_transform = _build_car_space_transform(
            C3_FCAM_INTRINSIC, calib_rpy, CANVAS_W, CANVAS_H, zoom=1.0,
        )

    frames = []
    base_mono = None
    last_cs = None
    last_sd = None
    last_cc = None
    last_radar = None
    last_bus = {}
    frame_count = 0

    try:
        for ev in _iter_log(qlog_path):
            if base_mono is None and ev.which() != "initData":
                base_mono = ev.logMonoTime

            w = ev.which()

            if w == "liveCalibration":
                calib = ev.liveCalibration
                if len(calib.rpyCalib) == 3:
                    calib_rpy = list(calib.rpyCalib)
                    car_space_transform = _build_car_space_transform(
                        C3_FCAM_INTRINSIC, calib_rpy, CANVAS_W, CANVAS_H, zoom=1.0,
                    )
                    if calib.height:
                        path_offset_z = float(calib.height[0])
                        height = path_offset_z

            # Telemetry arrives at ~100 Hz but is only read at the 5 Hz model
            # sample points — keep the raw (lazily decoded) event and build
            # the dict on sampling. Building it per message cost ~2 s/segment.
            elif w == "carState":
                last_cs = ev

            elif w == "carControl":
                last_cc = ev

            elif w == "selfdriveState":
                last_sd = ev

            elif w == "radarState":
                last_radar = ev

            elif w == "pluginBusLog":
                # Latest raw JSON per topic of interest; parsed at sample time
                for e in ev.pluginBusLog.entries:
                    topic = str(e.topic)
                    if topic in _BUS_TOPICS:
                        last_bus[topic] = str(e.json)

            elif w == "carParams" and brand is None:
                brand = str(ev.carParams.brand).lower() or None

            elif w == "modelV2" and car_space_transform is not None:
                if base_mono is None:
                    continue

                # Downsample to ~5Hz (every 4th modelV2 at 20Hz)
                frame_count += 1
                if frame_count % 4 != 0:
                    continue

                t = seg_num * 60.0 + (ev.logMonoTime - base_mono) / 1e9
                model = ev.modelV2

                try:
                    path_3d = np.array([model.position.x, model.position.y, model.position.z], dtype=np.float32).T
                    lane_lines_3d = [np.array([ll.x, ll.y, ll.z], dtype=np.float32).T for ll in model.laneLines]
                    lane_probs = [round(float(p), 3) for p in model.laneLineProbs]
                    road_edges_3d = [np.array([re.x, re.y, re.z], dtype=np.float32).T for re in model.roadEdges]
                    road_edge_stds = [round(float(s), 3) for s in model.roadEdgeStds]

                    max_distance = float(np.clip(path_3d[-1, 0], MIN_DRAW_DISTANCE, MAX_DRAW_DISTANCE)) if path_3d.shape[0] > 0 else MAX_DRAW_DISTANCE
                    max_idx = _get_path_length_idx(lane_lines_3d[0][:, 0] if lane_lines_3d else np.array([]), max_distance)

                    lane_polygons = []
                    for i, ll in enumerate(lane_lines_3d):
                        poly = _map_line_to_polygon(car_space_transform, ll, 0.025 * (lane_probs[i] if i < len(lane_probs) else 0), 0.0, max_idx, max_distance, CANVAS_W, CANVAS_H)
                        lane_polygons.append(poly)

                    edge_polygons = [_map_line_to_polygon(car_space_transform, re, 0.025, 0.0, max_idx, max_distance, CANVAS_W, CANVAS_H) for re in road_edges_3d]

                    radar = None
                    if last_radar is not None:
                        lead = last_radar.radarState.leadOne
                        if lead.status:
                            radar = {"dRel": float(lead.dRel), "yRel": float(lead.yRel), "vRel": float(lead.vRel)}

                    path_max = max_distance
                    lead_data = None
                    if radar:
                        lead_d = radar["dRel"] * 2.0
                        path_max = float(np.clip(lead_d - min(lead_d * 0.35, 10.0), 0.0, max_distance))
                        idx = _get_path_length_idx(path_3d[:, 0], radar["dRel"])
                        z = float(path_3d[idx, 2]) if idx < len(path_3d) else 0.0
                        lead_pt = _map_point_to_screen(car_space_transform, radar["dRel"], -radar["yRel"], z + path_offset_z, CANVAS_W, CANVAS_H)
                        if lead_pt:
                            lead_data = {"pt": lead_pt, "dRel": round(radar["dRel"], 1), "vRel": round(radar["vRel"], 1)}

                    path_idx = _get_path_length_idx(path_3d[:, 0], path_max)
                    path_polygon = _map_line_to_polygon(car_space_transform, path_3d, 0.9, path_offset_z, path_idx, path_max, CANVAS_W, CANVAS_H)

                    frame = {"t": round(t, 2)}

                    if last_cs is not None:
                        cs = last_cs.carState
                        cruise = cs.cruiseState
                        frame.update({
                            "vEgo": round(float(cs.vEgo), 3),
                            "vEgoCluster": round(float(cs.vEgoCluster), 3),
                            "steeringAngleDeg": round(float(cs.steeringAngleDeg), 2),
                            "gasPressed": bool(cs.gasPressed),
                            "brakePressed": bool(cs.brakePressed),
                            "cruiseSpeed": round(float(cruise.speed), 2),
                            "cruiseEnabled": bool(cruise.enabled),
                            # vCruiseCluster is the cluster display value (km/h);
                            # cruiseState.speed is internal m/s — not what the driver sees
                            "vCruiseCluster": round(float(cs.vCruiseCluster), 1),
                        })
                    if last_cc is not None:
                        cc = last_cc.carControl
                        frame.update({
                            "steerCmd": round(float(cc.actuators.steeringAngleDeg), 4),
                            "accelCmd": round(float(cc.actuators.accel), 4),
                        })
                    if last_sd is not None:
                        sd = last_sd.selfdriveState
                        frame.update({
                            "sdState": str(sd.state),
                            "sdEnabled": bool(sd.enabled),
                            "alertText1": str(sd.alertText1),
                            "alertText2": str(sd.alertText2),
                            "alertType": str(sd.alertType),
                            "alertStatus": sd.alertStatus.raw if hasattr(sd.alertStatus, 'raw') else int(sd.alertStatus),
                            "alertSize": sd.alertSize.raw if hasattr(sd.alertSize, 'raw') else int(sd.alertSize),
                            "exp": bool(sd.experimentalMode),
                        })

                    # Plugin HUD elements from the bus_logger rlog record
                    sl_raw = last_bus.get("speedLimitState")
                    if sl_raw:
                        try:
                            sl = json.loads(sl_raw)
                            frame["sl"] = {
                                "limit": int(sl.get("speedLimit") or 0),
                                "confirmed": bool(sl.get("confirmed")),
                                "wayRef": str(sl.get("wayRef") or ""),
                                "roadName": str(sl.get("roadName") or ""),
                            }
                        except Exception:
                            pass
                    temps_raw = last_bus.get("bmw_temps")
                    if temps_raw:
                        try:
                            tp = json.loads(temps_raw)
                            frame["temps"] = {"coolant": int(tp.get("coolant", 0)),
                                              "oil": int(tp.get("oil", 0))}
                        except Exception:
                            pass
                    lk_raw = last_bus.get("lane_keeping")
                    if lk_raw:
                        try:
                            # Green emblem ring = lane_keeping anchored (ui_mod)
                            frame["lk"] = json.loads(lk_raw).get("state") == "anchor"
                        except Exception:
                            pass
                    if brand:
                        frame["brand"] = brand

                    frame["model"] = {
                        "laneLines": lane_polygons,
                        "laneLineProbs": lane_probs,
                        "roadEdges": edge_polygons,
                        "roadEdgeStds": road_edge_stds,
                        "path": path_polygon,
                        "lead": lead_data,
                    }
                    frames.append(frame)

                except Exception:
                    pass

    except Exception as e:
        logger.warning("HUD segment extraction error for %s: %s", qlog_path, e)

    # carParams can appear mid-segment (observed ~30 s in) — the brand is
    # constant for the drive, so backfill every frame once known.
    if brand:
        for f in frames:
            f.setdefault("brand", brand)

    return frames, calib_rpy, height, brand


def _extract_hud_data(store, fullname: str, start_seg: int, end_seg: int) -> list:
    """Extract HUD frames for a range of segments. Uses cache."""
    local_id = store.get_local_id(fullname)
    if not local_id:
        return []

    all_frames = []
    calib_rpy = None
    height = None
    brand = None

    # Seed calibration (and brand) from the nearest earlier cached segment of
    # this route, so a cold seek into segment N doesn't drop frames until
    # liveCalibration/carParams reappear.
    best_seg = -1
    for (lid, s), (_frames, c, h, b) in _HUD_DATA_CACHE.items():
        if lid == local_id and s < start_seg and c is not None and s > best_seg:
            best_seg, calib_rpy, height, brand = s, c, h, b

    for seg in range(start_seg, end_seg + 1):
        key = (local_id, seg)
        cached = _HUD_DATA_CACHE.get(key)
        if cached is not None:
            _HUD_DATA_CACHE.move_to_end(key)
            frames, seg_calib, seg_height, seg_brand = cached
            all_frames.extend(frames)
            if seg_calib is not None:
                calib_rpy, height = seg_calib, seg_height
            brand = seg_brand or brand
            continue

        disk = _disk_cache_load(local_id, seg)
        if disk is not None:
            frames, seg_calib, seg_height, seg_brand = disk
            if seg_calib is not None:
                calib_rpy, height = seg_calib, seg_height
            brand = seg_brand or brand
        else:
            # Use rlog (not qlog) — modelV2 at full rate is only in rlogs
            qlog = store.resolve_segment_path(fullname, seg, "rlog.zst")
            if not qlog:
                qlog = store.resolve_segment_path(fullname, seg, "rlog")
            if qlog:
                frames, calib_rpy, height, brand = _extract_hud_segment(
                    str(qlog), seg, calib_rpy, height, brand)
                if frames:
                    _disk_cache_store(local_id, seg, frames, calib_rpy, height, brand)
            else:
                frames = []

        _HUD_DATA_CACHE[key] = (frames, calib_rpy, height, brand)
        _HUD_DATA_CACHE.move_to_end(key)
        while len(_HUD_DATA_CACHE) > _HUD_CACHE_MAX_SEGMENTS:
            _HUD_DATA_CACHE.popitem(last=False)
        all_frames.extend(frames)

    return all_frames


async def handle_hud_emblem(request):
    """GET /v1/hud/emblem/{brand}?style=color|white — brand emblem PNG.

    Served from the plugins repo's logos/ (the same assets ui_mod draws
    onroad), so the overlay emblem is pixel-identical to the real HUD.
    """
    import re as _re
    from config import PLUGINS_REPO_DIR

    brand = request.match_info["brand"]
    if not _re.fullmatch(r"[a-z0-9_-]{1,32}", brand):
        return web.json_response({"error": "invalid brand"}, status=400)
    subdir = "emblems" if request.query.get("style", "color") == "color" else "icons"
    path = Path(PLUGINS_REPO_DIR) / "logos" / subdir / f"{brand}.png"
    if not path.is_file():
        return web.json_response({"error": "not found"}, status=404)
    return web.FileResponse(path, headers={
        "Content-Type": "image/png",
        "Cache-Control": "public, max-age=86400",
    })


async def handle_hud_data(request):
    """GET /v1/route/{routeName}/hud_data?start=0&end=60

    Extract pre-projected HUD overlay data from qlogs for a time range.
    Returns JSON array of time-indexed frames with telemetry + model polygons.
    Cached per segment for fast subsequent requests.
    """
    from handler_helpers import get_route_or_404

    route_name, route, store = get_route_or_404(request)
    fullname = route["fullname"]

    start = float(request.query.get("start", "0"))
    end = float(request.query.get("end", "60"))

    start_seg = int(start // 60)
    end_seg = int(end // 60)
    max_seg = route.get("maxqlog", 0)
    end_seg = min(end_seg, max_seg)

    loop = asyncio.get_event_loop()
    frames = await loop.run_in_executor(
        None, _extract_hud_data, store, fullname, start_seg, end_seg,
    )

    # Filter to requested time range
    frames = [f for f in frames if start <= f["t"] <= end]

    return web.json_response(frames, headers={
        "Cache-Control": "public, max-age=86400",
    })
