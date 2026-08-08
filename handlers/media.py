import asyncio
import hashlib
import io
import json
import logging
import os
import subprocess
import tempfile
from bisect import bisect_left
from datetime import datetime, timezone
from math import floor
from pathlib import Path

from aiohttp import web

from handler_helpers import get_route_or_404, parse_json
from log_parser import _generate_coords_json
from route_helpers import _resolve_local_id

from config import FFMPEG_BIN

logger = logging.getLogger("connect")


def _decimal_to_dms(decimal):
    """Convert decimal degrees to EXIF DMS as IFDRational tuples."""
    from PIL.TiffImagePlugin import IFDRational
    d = int(decimal)
    m = int((decimal - d) * 60)
    s = round((decimal - d - m / 60) * 3600, 2)
    return (IFDRational(d), IFDRational(m), IFDRational(int(s * 100), 100))


from config import COD_CACHE_DIR, OPENPILOT_DIR as _OPENPILOT_DIR

FCAMERA_CACHE_DIR = COD_CACHE_DIR
HLS_CACHE_DIR = Path(COD_CACHE_DIR) / "qcamera_hls"


def _generate_hls_segments(store, fullname: str) -> Path | None:
    """Split all qcamera.ts into ~4s HLS segments via ffmpeg codec copy.

    Creates: {HLS_CACHE_DIR}/{local_id}/index.m3u8 + seg000.ts, seg001.ts, ...
    Uses ffmpeg concat demuxer -> HLS muxer with -c copy (~60ms/segment on C3).
    """
    local_id = store.get_local_id(fullname)
    if not local_id:
        return None

    out_dir = HLS_CACHE_DIR / local_id
    manifest = out_dir / "index.m3u8"
    if manifest.exists():
        return out_dir  # Already cached

    # Collect all existing qcamera.ts segment paths
    ts_paths = []
    seg = 0
    while True:
        p = store.data_dir / f"{local_id}--{seg}" / "qcamera.ts"
        if not p.exists():
            break
        ts_paths.append(str(p))
        seg += 1

    if not ts_paths:
        return None

    # Create output directory and concat list
    out_dir.mkdir(parents=True, exist_ok=True)
    list_path = out_dir / "concat.txt"
    with open(list_path, 'w') as f:
        for p in ts_paths:
            f.write(f"file '{p}'\n")

    # ffmpeg: concat -> HLS split (codec copy, ~4s segments)
    tmp_manifest = out_dir / "index.m3u8.tmp"
    try:
        subprocess.run([
            FFMPEG_BIN, '-y', '-hide_banner', '-loglevel', 'error',
            '-f', 'concat', '-safe', '0', '-i', str(list_path),
            '-c', 'copy',
            '-movflags', '+frag_keyframe+empty_moov+default_base_moof',
            '-hls_time', '30',
            '-hls_segment_type', 'fmp4',
            '-hls_playlist_type', 'vod',
            '-hls_segment_filename', str(out_dir / 'seg%03d.m4s'),
            '-hls_fmp4_init_filename', 'init.mp4',
            '-f', 'hls',
            str(tmp_manifest),
        ], check=True, timeout=300)
        tmp_manifest.rename(manifest)
    except Exception:
        # Clean up on failure
        if tmp_manifest.exists():
            tmp_manifest.unlink()
        import shutil
        if out_dir.exists() and not manifest.exists():
            shutil.rmtree(out_dir, ignore_errors=True)
        raise
    finally:
        if list_path.exists():
            list_path.unlink()

    return out_dir


async def handle_qcamera_hls_manifest(request: web.Request) -> web.Response:
    """GET /v1/route/{routeName}/qcamera.m3u8

    Generate proper HLS manifest with ~4s segments via ffmpeg codec copy.
    Rewrites segment URLs to include the route prefix for correct routing.
    """
    route_name, route, store = get_route_or_404(request)
    fullname = route["fullname"]

    loop = asyncio.get_event_loop()
    try:
        out_dir = await loop.run_in_executor(
            None, _generate_hls_segments, store, fullname
        )
    except Exception as e:
        logger.warning("HLS segment generation failed: %s", e)
        return web.Response(status=500, text=str(e))

    if not out_dir:
        return web.Response(status=404, text="No qcamera segments found")

    # Read the generated manifest and rewrite segment URLs
    manifest_text = (out_dir / "index.m3u8").read_text()
    lines = []
    for line in manifest_text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith('#'):
            # Rewrite bare filenames (seg000.m4s, init.mp4) to routed URLs
            lines.append(f"/v1/route/{route_name}/qcamera_hls/{stripped}")
        elif stripped.startswith('#EXT-X-TARGETDURATION'):
            lines.append('#EXT-X-START:TIME-OFFSET=0,PRECISE=YES')
            lines.append(stripped)
        elif stripped.startswith('#EXT-X-MAP'):
            # Rewrite init segment URI
            lines.append(f'#EXT-X-MAP:URI="/v1/route/{route_name}/qcamera_hls/init.mp4"')
        else:
            lines.append(stripped)

    return web.Response(
        text="\n".join(lines),
        content_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "public, max-age=86400"},
    )


_HLS_CONTENT_TYPES = {
    '.m4s': 'video/iso.segment',
    '.mp4': 'video/mp4',
    '.ts': 'video/mp2t',
}


async def handle_qcamera_hls_segment(request: web.Request) -> web.Response:
    """GET /v1/route/{routeName}/qcamera_hls/{filename}

    Serve individual cached HLS files (init.mp4, seg000.m4s, seg001.m4s, ...).
    """
    route_name, route, store = get_route_or_404(request)
    filename = request.match_info["filename"]

    # Validate filename to prevent path traversal
    ext = os.path.splitext(filename)[1]
    if '/' in filename or '..' in filename or ext not in _HLS_CONTENT_TYPES:
        return web.Response(status=400, text="Invalid segment filename")

    local_id = store.get_local_id(route["fullname"])
    if not local_id:
        return web.Response(status=404, text="Route not found")

    seg_path = HLS_CACHE_DIR / local_id / filename
    if not seg_path.exists():
        return web.Response(status=404, text=f"Segment {filename} not found")

    return web.FileResponse(seg_path, headers={
        "Content-Type": _HLS_CONTENT_TYPES[ext],
        "Cache-Control": "public, max-age=604800",
    })


def _mux_fcamera(hevc_path: str) -> str:
    """Mux raw HEVC bitstream to MP4 container (codec copy, ~1.5s). Returns cached mp4 path."""
    os.makedirs(FCAMERA_CACHE_DIR, exist_ok=True)
    path_hash = hashlib.md5(hevc_path.encode()).hexdigest()[:12]
    mp4_path = os.path.join(FCAMERA_CACHE_DIR, f"fcamera_{path_hash}.mp4")

    if os.path.exists(mp4_path):
        return mp4_path

    fd, tmp_path = tempfile.mkstemp(suffix='.mp4', dir=FCAMERA_CACHE_DIR)
    os.close(fd)
    try:
        subprocess.run([
            FFMPEG_BIN, '-y', '-hide_banner', '-loglevel', 'error',
            '-framerate', '20', '-i', hevc_path,
            '-c', 'copy', '-movflags', '+faststart', tmp_path,
        ], check=True, timeout=60)
        os.rename(tmp_path, mp4_path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

    return mp4_path


def _extract_frame(hevc_path: str, offset: float) -> bytes:
    """Extract a single JPEG frame from fcamera.hevc at the given offset.

    Raw HEVC lacks container timestamps so cv2 seeking is broken.
    Strategy: mux to mp4 (codec copy, ~1.5s one-time) then cv2 seeks in the
    container.  Cached mp4 is stored under FCAMERA_CACHE_DIR.
    Runs in executor thread.
    """
    import cv2

    mp4_path = _mux_fcamera(hevc_path)

    cap = cv2.VideoCapture(mp4_path)
    try:
        cap.set(cv2.CAP_PROP_POS_MSEC, offset * 1000)
        ret, frame = cap.read()
        if not ret or frame is None:
            raise RuntimeError(f"cv2 failed to read frame at {offset:.1f}s")
        ok, buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
        if not ok:
            raise RuntimeError("cv2 JPEG encoding failed")
        return buf.tobytes()
    finally:
        cap.release()


def _lookup_gps(coords: list, t: float) -> dict:
    """Find GPS point closest to time t from coords list. Returns dict with lat, lng, speed, bearing."""
    if not coords:
        return {}
    times = [c["t"] for c in coords]
    idx = bisect_left(times, t)
    if idx == 0:
        best_idx = 0
    elif idx >= len(coords):
        best_idx = len(coords) - 1
    elif (t - times[idx - 1]) <= (times[idx] - t):
        best_idx = idx - 1
    else:
        best_idx = idx
    best = coords[best_idx]
    result = {k: best.get(k) for k in ("lat", "lng", "speed")}

    # Compute bearing from consecutive GPS points
    if best_idx + 1 < len(coords):
        nxt = coords[best_idx + 1]
    elif best_idx > 0:
        nxt = best
        best = coords[best_idx - 1]
    else:
        return result

    import math
    lat1, lon1 = math.radians(best["lat"]), math.radians(best["lng"])
    lat2, lon2 = math.radians(nxt["lat"]), math.radians(nxt["lng"])
    dlon = lon2 - lon1
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    bearing = math.degrees(math.atan2(x, y)) % 360
    result["bearing"] = bearing
    return result


def _load_calibration(seg_dir: Path) -> dict | None:
    """Load cached calibration.json, or extract from rlog and cache it."""
    calib_file = seg_dir / "calibration.json"
    if calib_file.exists():
        try:
            return json.loads(calib_file.read_text())
        except Exception:
            pass

    # Extract from rlog
    rlog = seg_dir / "rlog.zst"
    if not rlog.exists():
        rlog = seg_dir / "rlog"
    if not rlog.exists():
        return None

    try:
        # Lazy import — only needed on C3 where openpilot is installed
        import sys
        if _OPENPILOT_DIR not in sys.path:
            sys.path.insert(0, _OPENPILOT_DIR)
        from tools.lib.logreader import LogReader

        calib = None
        for msg in LogReader(str(rlog)):
            if msg.which() == "liveCalibration":
                c = msg.liveCalibration
                rpy = list(c.rpyCalib)
                try:
                    height = list(c.height)
                except Exception:
                    height = [1.22]
                calib = {"rpyCalib": rpy, "height": height}
                break

        if calib:
            calib_file.write_text(json.dumps(calib))
            return calib
    except Exception:
        pass
    return None


# C3 fcamera (tici AR0231) intrinsics — from openpilot common/transformations/camera.py
_FCAM_WIDTH = 1928
_FCAM_HEIGHT = 1208
_FCAM_FOCAL_LENGTH = 2648.0  # pixels
import math as _math
_FCAM_HFOV = 2 * _math.degrees(_math.atan(_FCAM_WIDTH / 2 / _FCAM_FOCAL_LENGTH))  # ~40°
_FCAM_VFOV = 2 * _math.degrees(_math.atan(_FCAM_HEIGHT / 2 / _FCAM_FOCAL_LENGTH))  # ~25.6°


def _add_exif(frame_bytes: bytes, gps: dict, calibration: dict | None,
              timestamp: float, route_ref: str) -> bytes:
    """Embed rich EXIF metadata into a JPEG frame.

    EXIF = immutable capture-time facts:
    - GPS (lat, lon, heading, speed)
    - Timestamp (UTC)
    - Route reference (dongle/route/segment/frame)
    - Camera intrinsics (focal length, resolution, FOV)
    - Camera pose (height, pitch angle from calibration)
    """
    from PIL import Image
    from PIL.ExifTags import IFD, GPS as GPSTags, Base
    from PIL.TiffImagePlugin import IFDRational

    img = Image.open(io.BytesIO(frame_bytes))
    exif = img.getexif()

    # --- GPS IFD ---
    lat, lng = gps.get("lat"), gps.get("lng")
    if lat is not None and lng is not None:
        gps_ifd = exif.get_ifd(IFD.GPSInfo)
        gps_ifd[GPSTags.GPSLatitudeRef] = 'N' if lat >= 0 else 'S'
        gps_ifd[GPSTags.GPSLatitude] = _decimal_to_dms(abs(lat))
        gps_ifd[GPSTags.GPSLongitudeRef] = 'E' if lng >= 0 else 'W'
        gps_ifd[GPSTags.GPSLongitude] = _decimal_to_dms(abs(lng))
        if gps.get("bearing") is not None:
            gps_ifd[GPSTags.GPSImgDirectionRef] = 'T'  # True north
            gps_ifd[GPSTags.GPSImgDirection] = IFDRational(round(gps["bearing"] * 100), 100)
        if gps.get("speed") is not None:
            gps_ifd[GPSTags.GPSSpeedRef] = 'K'  # km/h
            gps_ifd[GPSTags.GPSSpeed] = IFDRational(round(gps["speed"] * 3.6 * 100), 100)

    # --- Timestamp ---
    dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    exif[Base.DateTimeOriginal] = dt.strftime("%Y:%m:%d %H:%M:%S")

    # --- UserComment: structured JSON with all metadata ---
    meta = {
        "route": route_ref,
        "camera": {
            "model": "AR0231",
            "width": _FCAM_WIDTH,
            "height": _FCAM_HEIGHT,
            "focal_length_px": _FCAM_FOCAL_LENGTH,
            "hfov_deg": round(_FCAM_HFOV, 1),
            "vfov_deg": round(_FCAM_VFOV, 1),
        },
    }
    if calibration:
        rpy = calibration.get("rpyCalib", [0, 0, 0])
        height = calibration.get("height", [1.22])
        meta["pose"] = {
            "height_m": round(height[0], 4),
            "pitch_deg": round(_math.degrees(rpy[1]), 3),
            "yaw_deg": round(_math.degrees(rpy[2]), 3),
            "roll_deg": round(_math.degrees(rpy[0]), 3),
        }
    if gps.get("speed") is not None:
        meta["speed_ms"] = round(gps["speed"], 2)
    if gps.get("bearing") is not None:
        meta["bearing_deg"] = round(gps["bearing"], 1)

    # EXIF UserComment: JSON-encoded metadata for programmatic access
    exif[Base.UserComment] = json.dumps(meta)
    # ImageDescription: human-readable summary
    exif[Base.ImageDescription] = route_ref

    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=95, exif=exif.tobytes())
    return buf.getvalue()


async def handle_screenshot(request: web.Request) -> web.Response:
    """POST /v1/route/{routeName}/screenshot — extract fcamera frame with EXIF metadata."""
    route_name, route, store = get_route_or_404(request)
    body = await parse_json(request)

    t = body.get("time", 0)
    camera = body.get("camera", "fcamera")
    segment = int(t // 60)
    offset = t % 60

    fullname = route["fullname"]
    local_id = route["_local_id"]

    # Resolve camera file (fcamera/ecamera/dcamera)
    cam_filename = CAMERA_TYPES.get(camera, "fcamera.hevc")
    fcamera = store.resolve_segment_path(fullname, segment, cam_filename)
    if not fcamera:
        raise web.HTTPNotFound(text=json.dumps({"error": f"No {cam_filename} for segment {segment}"}))

    # Extract frame via cached mp4 mux + cv2 seek in thread executor
    loop = asyncio.get_event_loop()
    try:
        frame_bytes = await loop.run_in_executor(
            store._executor, _extract_frame, str(fcamera), offset
        )
    except Exception as e:
        raise web.HTTPInternalServerError(text=json.dumps({"error": f"Frame extraction failed: {e}"}))

    # Look up GPS (lat, lng, speed, bearing) from coords.json
    seg_dir = store.data_dir / f"{local_id}--{segment}"
    gps = {}
    coords_file = seg_dir / "coords.json"
    if coords_file.exists():
        try:
            coords = json.loads(coords_file.read_text())
            gps = _lookup_gps(coords, t)
        except Exception:
            pass

    # Load calibration (camera pose: height, pitch, yaw, roll)
    calibration = await loop.run_in_executor(store._executor, _load_calibration, seg_dir)

    # Build EXIF metadata
    create_time = route.get("create_time", 0)
    timestamp = create_time + t
    route_ref = f"{fullname}/{local_id}/{segment}/{offset:.2f}"

    try:
        jpeg_bytes = await loop.run_in_executor(
            None, _add_exif, frame_bytes, gps, calibration, timestamp, route_ref
        )
    except Exception as e:
        logging.getLogger("connect").warning("EXIF embedding failed: %s", e)
        jpeg_bytes = frame_bytes

    # Build filename: {route_date}_{MM}m{SS}s.jpg
    route_date = fullname.split("/")[-1]  # e.g. "2026-02-20--10-47-46"
    mm = int(t // 60)
    ss = int(t % 60)
    filename = f"{route_date}_{mm:02d}m{ss:02d}s.jpg"

    return web.Response(
        body=jpeg_bytes,
        content_type="image/jpeg",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


async def handle_frame(request: web.Request) -> web.Response:
    """GET /v1/route/{routeName}/frame?t=123.45 — return fcamera JPEG for the given time.

    URL-friendly: open in browser or use in <img> tags.
    """
    route_name, route, store = get_route_or_404(request)

    try:
        t = float(request.query.get("t", 0))
    except (ValueError, TypeError):
        raise web.HTTPBadRequest(text=json.dumps({"error": "Invalid t parameter"}))

    segment = int(t // 60)
    offset = t % 60

    fcamera = store.resolve_segment_path(route["fullname"], segment, "fcamera.hevc")
    if not fcamera:
        raise web.HTTPNotFound(text=json.dumps({"error": f"No fcamera.hevc for segment {segment}"}))

    fullname = route["fullname"]
    local_id = route["_local_id"]

    loop = asyncio.get_event_loop()
    try:
        frame_bytes = await loop.run_in_executor(
            store._executor, _extract_frame, str(fcamera), offset
        )
    except Exception as e:
        raise web.HTTPInternalServerError(text=json.dumps({"error": f"Frame extraction failed: {e}"}))

    # Enrich with EXIF (GPS, calibration, camera intrinsics)
    seg_dir = store.data_dir / f"{local_id}--{segment}"
    gps = {}
    coords_file = seg_dir / "coords.json"
    if coords_file.exists():
        try:
            gps = _lookup_gps(json.loads(coords_file.read_text()), t)
        except Exception:
            pass

    calibration = await loop.run_in_executor(store._executor, _load_calibration, seg_dir)

    create_time = route.get("create_time", 0)
    timestamp = create_time + t
    route_ref = f"{fullname}/{local_id}/{segment}/{offset:.2f}"

    try:
        jpeg_bytes = await loop.run_in_executor(
            None, _add_exif, frame_bytes, gps, calibration, timestamp, route_ref
        )
    except Exception as e:
        logging.getLogger("connect").warning("EXIF embedding failed: %s", e)
        jpeg_bytes = frame_bytes

    return web.Response(
        body=jpeg_bytes,
        content_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


CAMERA_TYPES = {"fcamera": "fcamera.hevc", "ecamera": "ecamera.hevc", "dcamera": "dcamera.hevc"}
CAMERA_FPS = {"fcamera": 20, "ecamera": 20, "dcamera": 20}


def _mux_hevc(hevc_path: str, fps: int = 20) -> str:
    """Mux raw HEVC bitstream to MP4 container (codec copy). Returns cached mp4 path."""
    os.makedirs(FCAMERA_CACHE_DIR, exist_ok=True)
    path_hash = hashlib.md5(hevc_path.encode()).hexdigest()[:12]
    mp4_path = os.path.join(FCAMERA_CACHE_DIR, f"cam_{path_hash}.mp4")

    if os.path.exists(mp4_path):
        return mp4_path

    fd, tmp_path = tempfile.mkstemp(suffix='.mp4', dir=FCAMERA_CACHE_DIR)
    os.close(fd)
    try:
        subprocess.run([
            FFMPEG_BIN, '-y', '-hide_banner', '-loglevel', 'error',
            '-framerate', str(fps), '-i', hevc_path,
            '-c', 'copy', '-movflags', '+faststart', tmp_path,
        ], check=True, timeout=60)
        os.rename(tmp_path, mp4_path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

    return mp4_path


async def handle_camera_segment(request: web.Request) -> web.Response:
    """GET /v1/route/{routeName}/camera/{camera_type}/{segment} — serve HEVC camera as MP4.

    Muxes the raw .hevc bitstream into an MP4 container (codec copy, no
    re-encoding, ~1.5s one-time cost).  The result is cached on disk.
    Supports: fcamera (road), ecamera (wide), dcamera (driver).
    """
    route_name, route, store = get_route_or_404(request)
    camera_type = request.match_info["camera_type"]
    segment = int(request.match_info["segment"])

    if camera_type not in CAMERA_TYPES:
        raise web.HTTPBadRequest(text=json.dumps({"error": f"Unknown camera: {camera_type}"}))

    filename = CAMERA_TYPES[camera_type]
    hevc_path = store.resolve_segment_path(route["fullname"], segment, filename)
    if not hevc_path:
        raise web.HTTPNotFound(text=json.dumps({"error": f"No {filename} for segment {segment}"}))

    fps = CAMERA_FPS.get(camera_type, 20)
    loop = asyncio.get_event_loop()
    try:
        mp4_path = await loop.run_in_executor(store._executor, _mux_hevc, str(hevc_path), fps)
    except Exception as e:
        raise web.HTTPInternalServerError(text=json.dumps({"error": f"Muxing failed: {e}"}))

    return web.FileResponse(mp4_path, headers={
        "Content-Type": "video/mp4",
        "Cache-Control": "public, max-age=86400",
    })


# ─── MJPEG streaming ────────────────────────────────────────────────

def _collect_qcamera_paths(store, fullname: str) -> list[str]:
    """Collect all qcamera.ts paths for a route, ordered by segment."""
    local_id = store.get_local_id(fullname)
    if not local_id:
        return []
    paths = []
    seg = 0
    while True:
        p = store.data_dir / f"{local_id}--{seg}" / "qcamera.ts"
        if not p.exists():
            break
        paths.append(str(p))
        seg += 1
    return paths


def _write_concat_file(ts_paths: list[str]) -> str:
    """Write a temporary ffmpeg concat file. Caller must delete it."""
    fd, path = tempfile.mkstemp(suffix=".txt", prefix="mjpeg_concat_", dir=COD_CACHE_DIR)
    with os.fdopen(fd, "w") as f:
        for p in ts_paths:
            f.write(f"file '{p}'\n")
    return path


MJPEG_BOUNDARY = b"--mjpegframe"


async def handle_mjpeg_stream(request: web.Request) -> web.StreamResponse:
    """GET /v1/route/{routeName}/mjpeg?t=0&fps=20&q=5

    Stream qcamera as MJPEG (multipart/x-mixed-replace).
    Works in every browser — no codec dependencies.

    Query params:
      t     — start time in seconds (default 0)
      fps   — output framerate (default 20)
      q     — JPEG quality 2-31, lower=better (default 5)
      speed — playback speed multiplier (default 1.0, e.g. 0.5, 2.0)
    """
    route_name, route, store = get_route_or_404(request)
    fullname = route["fullname"]

    start_time = float(request.query.get("t", "0"))
    fps = int(request.query.get("fps", "20"))
    quality = int(request.query.get("q", "5"))
    speed = float(request.query.get("speed", "1.0"))
    fps = max(1, min(30, fps))
    quality = max(2, min(31, quality))
    speed = max(0.25, min(4.0, speed))

    ts_paths = _collect_qcamera_paths(store, fullname)
    if not ts_paths:
        raise web.HTTPNotFound(text="No qcamera segments found")

    # Skip to the right segment for fast seeking (each segment = 60s)
    # Round down to segment boundary — no -ss needed, instant start
    start_seg = int(start_time // 60)
    if start_seg >= len(ts_paths):
        start_seg = max(0, len(ts_paths) - 1)
    actual_start = start_seg * 60
    ts_paths = ts_paths[start_seg:]

    concat_path = _write_concat_file(ts_paths)

    cmd = [
        FFMPEG_BIN, '-hide_banner', '-loglevel', 'error',
        '-f', 'concat', '-safe', '0', '-i', concat_path,
        '-vf', f'fps={fps}',
        '-q:v', str(quality),
        '-f', 'image2pipe', '-vcodec', 'mjpeg',
        'pipe:1',
    ]
    frame_interval = 1.0 / (fps * speed)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "multipart/x-mixed-replace; boundary=mjpegframe",
            "Cache-Control": "no-cache, no-store",
            "Connection": "close",
            "X-Start-Time": str(actual_start),
        },
    )
    try:
        await response.prepare(request)
    except Exception:
        proc.kill()
        await proc.wait()
        os.unlink(concat_path)
        raise

    try:
        import time as _time
        buf = b""
        SOI = b"\xff\xd8"
        EOI = b"\xff\xd9"
        next_frame_time = _time.monotonic()

        while True:
            chunk = await proc.stdout.read(65536)
            if not chunk:
                break
            buf += chunk

            # Extract complete JPEG frames from the pipe
            while True:
                soi_pos = buf.find(SOI)
                if soi_pos < 0:
                    buf = b""
                    break
                eoi_pos = buf.find(EOI, soi_pos + 2)
                if eoi_pos < 0:
                    buf = buf[soi_pos:]
                    break

                frame = buf[soi_pos:eoi_pos + 2]
                buf = buf[eoi_pos + 2:]

                # Throttle to target fps
                now = _time.monotonic()
                delay = next_frame_time - now
                if delay > 0:
                    await asyncio.sleep(delay)
                next_frame_time = max(_time.monotonic(), next_frame_time + frame_interval)

                part = (
                    MJPEG_BOUNDARY + b"\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    b"Content-Length: " + str(len(frame)).encode() + b"\r\n"
                    b"\r\n"
                )
                await response.write(part + frame + b"\r\n")
    except (ConnectionResetError, ConnectionAbortedError, asyncio.CancelledError):
        pass
    finally:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        try:
            os.unlink(concat_path)
        except OSError:
            pass

    return response
