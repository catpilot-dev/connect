import asyncio
import json
import logging
import os
import socket
import subprocess
import time
from pathlib import Path

from aiohttp import web

from handler_helpers import error_response, get_route_or_404, resolve_route_name
from hud_stream import HLS_DIR, HudStreamManager

logger = logging.getLogger("connect")

# ─── HUD video rendering state ───────────────────────────────────────
# fullname -> {proc, status_file, output, start, end}
_hud_prerender_tasks: dict = {}

from config import (COD_HUD_CACHE_DIR, COD_HLS_TMP_DIR,
                     OPENPILOT_DIR as _OPENPILOT_DIR_STR,
                     PYTHON_BIN, REPLAY_BIN as _REPLAY_BIN_STR)

HUD_CACHE_DIR = Path(COD_HUD_CACHE_DIR)
RENDER_HLS_DIR = Path(COD_HLS_TMP_DIR)
RENDER_SCRIPT_HEADLESS = Path(__file__).parent.parent / "render_clip_headless.py"
RENDER_SCRIPT_DRM = Path(__file__).parent.parent / "render_clip_drm.py"  # legacy fallback
OPENPILOT_DIR = Path(_OPENPILOT_DIR_STR)
REPLAY_BIN = Path(_REPLAY_BIN_STR)


def _is_manager_running() -> bool:
    """Check if openpilot manager is running."""
    result = subprocess.run(["pgrep", "-f", "manager.py"], capture_output=True)
    return result.returncode == 0


async def _ensure_manager():
    """Verify openpilot manager is running after HUD cleanup; restart if not."""
    from hud_stream import _start_manager
    loop = asyncio.get_event_loop()
    await asyncio.sleep(2)
    running = await loop.run_in_executor(None, _is_manager_running)
    if running:
        logger.info("Manager verified running")
        return
    logger.warning("Manager not running, restarting...")
    await loop.run_in_executor(None, _start_manager)


async def _verify_replay_binary():
    """Test-run replay binary to verify it's functional.

    Returns True if healthy, False if broken (crash, missing libs, ABI mismatch).
    Checks that the binary can run without crashing — a normal exit (code >= 0)
    is OK; only signal kills (code < 0, e.g. SIGABRT=-6) indicate breakage.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            str(REPLAY_BIN), "--help",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        if proc.returncode >= 0:
            return True
        # Negative return code = killed by signal (e.g. -6 = SIGABRT)
        logger.warning("Replay binary crashed (signal %d): %s",
                       -proc.returncode, (stdout.decode()[-200:] if stdout else ""))
        return False
    except asyncio.TimeoutError:
        logger.warning("Replay binary health check timed out")
        return False
    except Exception as e:
        logger.warning("Replay binary health check error: %s", e)
        return False


async def _rebuild_replay_binary():
    """Rebuild replay binary from scons cache.

    Returns True on success, False on failure.
    """
    logger.info("Rebuilding replay binary from scons cache...")
    proc = await asyncio.create_subprocess_exec(
        "scons", "tools/replay/replay", "-j2",
        cwd=str(OPENPILOT_DIR),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, "VIRTUAL_ENV": "/usr/local/venv",
             "PATH": "/usr/local/venv/bin:" + os.environ.get("PATH", "")},
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
    if proc.returncode == 0 and REPLAY_BIN.is_file():
        logger.info("Replay binary rebuilt successfully")
        return True
    else:
        logger.error("Failed to rebuild replay binary: %s",
                     stdout.decode()[-500:] if stdout else "no output")
        return False


_REPLAY_BACKUP = Path(__file__).parent.parent / "lib" / "replay"


async def _restore_replay_from_backup() -> bool:
    """Copy replay binary from COD backup to openpilot tools dir."""
    if not _REPLAY_BACKUP.is_file():
        return False
    try:
        import shutil
        REPLAY_BIN.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(_REPLAY_BACKUP), str(REPLAY_BIN))
        REPLAY_BIN.chmod(0o755)
        logger.info("Restored replay binary from backup")
        return True
    except Exception as e:
        logger.warning("Failed to restore replay from backup: %s", e)
        return False


async def _ensure_replay_binary():
    """Check replay binary exists and is functional; restore/rebuild if needed.

    Returns True if binary is available, False on failure.
    """
    if not REPLAY_BIN.is_file():
        logger.info("Replay binary missing, restoring from backup...")
        if not await _restore_replay_from_backup():
            logger.info("Backup not available, trying scons rebuild...")
            return await _rebuild_replay_binary()

    if not await _verify_replay_binary():
        logger.info("Replay binary broken, restoring from backup...")
        if not await _restore_replay_from_backup():
            return await _rebuild_replay_binary()
        if not await _verify_replay_binary():
            logger.info("Backup binary also broken, trying scons rebuild...")
            return await _rebuild_replay_binary()

    return True

# Quality presets for DRM mode — 0.2x replay, 2fps capture → 10 unique frames/route-sec
# Post-processed: 5x speedup + frame-dup to target fps. wall_duration ≈ 5x duration
QUALITY_PRESETS_DRM = {
    "high":   {"fps": 20, "bitrate_mbps": 3.0},
    "medium": {"fps": 20, "bitrate_mbps": 1.5},
    "low":    {"fps": 10, "bitrate_mbps": 0.8},
}


def _get_render_script() -> Path | None:
    """Get the best available render script.

    Prefers headless (no DRM master needed), falls back to DRM.
    Requires raylib package and the UI script.
    """
    try:
        import raylib
    except ImportError:
        return None
    if not (OPENPILOT_DIR / "selfdrive/ui/ui.py").is_file():
        return None
    if RENDER_SCRIPT_HEADLESS.exists():
        return RENDER_SCRIPT_HEADLESS
    if RENDER_SCRIPT_DRM.exists():
        return RENDER_SCRIPT_DRM
    return None


def _is_drm_available() -> bool:
    """Check if any render backend is available."""
    return _get_render_script() is not None


# ─── HUD pre-render endpoints ────────────────────────────────────────

def _hud_cache_path(fullname: str, start: float, end: float, quality: str = "high") -> Path:
    """Build the cache path for a rendered HUD video."""
    safe = fullname.replace("/", "_")
    return HUD_CACHE_DIR / f"{safe}_{int(start)}_{int(end)}_{quality}.mp4"


def _read_status_file(path: str) -> dict | None:
    """Read a render status JSON file."""
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return None


async def handle_hud_prerender(request: web.Request) -> web.Response:
    """POST /v1/route/{routeName}/hud/prerender — start HUD video rendering via openpilot UI."""
    import sys

    route_name, route, store = get_route_or_404(request)

    fullname = route["fullname"]
    max_seg = route["maxqlog"]

    # Parse range from body
    try:
        body = await request.json()
    except Exception:
        body = {}

    start_sec = body.get("start", 0)
    end_sec = body.get("end", (max_seg + 1) * 60)
    duration = end_sec - start_sec
    if duration <= 0:
        raise web.HTTPBadRequest(text=json.dumps({"error": "Invalid time range"}))

    if not _is_drm_available():
        raise web.HTTPServiceUnavailable(
            text=json.dumps({"error": "DRM rendering not available (missing raylib or ui.py)"}))

    # Accept explicit fps, or fall back to quality preset
    quality = body.get("quality")
    if quality and quality in QUALITY_PRESETS_DRM:
        preset = QUALITY_PRESETS_DRM[quality]
    else:
        preset = QUALITY_PRESETS_DRM["high"]
    render_fps = preset["fps"]
    render_bitrate = preset["bitrate_mbps"]

    estimated_mb = round(duration * render_bitrate / 8)
    wall_duration = round(duration / 0.2 + 15)

    cache_tag = f"drm_f{render_fps}"

    # Check cache — already rendered?
    cache_mp4 = _hud_cache_path(fullname, start_sec, end_sec, cache_tag)
    if cache_mp4.exists() and cache_mp4.stat().st_size > 1000:
        return web.json_response({
            "status": "complete",
            "elapsed_sec": duration,
            "total_sec": duration,
            "estimated_mb": estimated_mb,
            "wall_duration": wall_duration,
        })

    # Check running task for same route+range
    existing = _hud_prerender_tasks.get(fullname)
    if existing:
        ex_start = existing.get("start", 0)
        ex_end = existing.get("end", 0)
        proc = existing.get("proc")
        if ex_start == start_sec and ex_end == end_sec and proc and proc.returncode is None:
            # Same render in progress — read status file for progress
            status_data = _read_status_file(existing["status_file"])
            if status_data:
                return web.json_response(status_data)
            return web.json_response({"status": "rendering", "elapsed_sec": 0, "total_sec": duration})

        # Different range or finished process — kill old one
        if proc and proc.returncode is None:
            try:
                proc.terminate()
            except Exception:
                pass

    # Ensure replay binary is available (auto-rebuild from scons cache if needed)
    if not await _ensure_replay_binary():
        raise web.HTTPServiceUnavailable(
            text=json.dumps({"error": "Replay binary not available and rebuild failed"}))

    # Prepare paths
    HUD_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    local_id = route["_local_id"]
    dongle_id = route["dongle_id"]
    status_file = str(HUD_CACHE_DIR / f"{local_id}_{int(start_sec)}_{int(end_sec)}.status.json")
    output = str(cache_mp4)

    # Determine python and script paths
    python_bin = PYTHON_BIN if os.path.isfile(PYTHON_BIN) else sys.executable
    render_script = _get_render_script()
    script = str(render_script)

    # Extract route metadata for MP4 embedding
    route_date = ""
    parts = fullname.split("/")
    if len(parts) == 2:
        route_date = parts[1]  # e.g. "2026-02-23--12-38-40"
    op_version = route.get("version", "")
    op_branch = route.get("git_branch", "")
    op_commit = route.get("git_commit", "")
    car_fingerprint = route.get("platform", "")

    cmd = [
        python_bin, script,
        "--route-name", fullname.replace("/", "|"),
        "--local-id", local_id,
        "--dongle-id", dongle_id,
        "--data-dir", str(store.data_dir),
        "--start", str(start_sec),
        "--end", str(end_sec),
        "--fps", str(render_fps),
        "--route-date", route_date,
        "--op-version", op_version,
        "--op-branch", op_branch,
        "--op-commit", op_commit,
        "--car-fingerprint", car_fingerprint,
        "--output", output,
        "--status-file", status_file,
    ]
    logger.info("Launching HUD render (DRM): %s (%.0fs-%.0fs, fps=%d)",
                fullname, start_sec, end_sec, render_fps)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )

    _hud_prerender_tasks[fullname] = {
        "proc": proc,
        "status_file": status_file,
        "output": output,
        "start": start_sec,
        "end": end_sec,
    }

    return web.json_response({
        "status": "rendering",
        "elapsed_sec": 0,
        "total_sec": duration,
        "estimated_mb": estimated_mb,
        "wall_duration": wall_duration,
    })


async def handle_hud_progress(request: web.Request) -> web.Response:
    """GET /v1/route/{routeName}/hud/progress — check HUD video render progress."""
    route_name = resolve_route_name(request)
    store = request.app["store"]

    # Resolve to fullname (tasks are keyed by fullname)
    route = store.get_route(route_name)
    fullname = route["fullname"] if route else route_name
    task = _hud_prerender_tasks.get(fullname)

    if not task:
        return web.json_response({"status": "idle", "elapsed_sec": 0, "total_sec": 0})

    # Read status from the status file written by the render script
    status_data = _read_status_file(task["status_file"])
    if status_data:
        # If subprocess says complete, verify the file exists
        if status_data.get("status") == "complete":
            output_path = Path(task["output"])
            if output_path.exists() and output_path.stat().st_size > 1000:
                return web.json_response(status_data)
            else:
                status_data["status"] = "error"
                status_data["error"] = "Output file missing after render"
        return web.json_response(status_data)

    # No status file yet — check if process is still running
    proc = task.get("proc")
    if proc and proc.returncode is not None:
        return web.json_response({"status": "error", "error": "Render process exited unexpectedly"})

    duration = task["end"] - task["start"]
    return web.json_response({"status": "rendering", "elapsed_sec": 0, "total_sec": duration})


async def handle_hud_render_preview(request: web.Request) -> web.StreamResponse:
    """GET /v1/route/{routeName}/hud/preview — MJPEG stream of live render progress.

    Streams the latest rendered frame as MJPEG while the headless renderer runs.
    The renderer writes preview.jpg to the hud_cache dir every ~250ms.
    """
    import asyncio

    route_name = resolve_route_name(request)
    store = request.app["store"]
    route = store.get_route(route_name)
    fullname = route["fullname"] if route else route_name
    task = _hud_prerender_tasks.get(fullname)

    if not task:
        raise web.HTTPNotFound(text="No render in progress")

    preview_path = Path(task["status_file"]).parent / "preview.jpg"

    response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "multipart/x-mixed-replace; boundary=renderframe",
            "Cache-Control": "no-cache, no-store",
            "Connection": "close",
        },
    )
    await response.prepare(request)

    boundary = b"--renderframe"
    last_data = b""

    try:
        while True:
            # Check if render is still running
            proc = task.get("proc")
            if proc and proc.returncode is not None:
                break

            # Check for new preview frame (compare content, not mtime — C3 clock unreliable)
            if preview_path.exists():
                try:
                    frame_data = preview_path.read_bytes()
                    if frame_data and frame_data != last_data:
                        last_data = frame_data
                        part = (
                            boundary + b"\r\n"
                            b"Content-Type: image/jpeg\r\n"
                            b"Content-Length: " + str(len(frame_data)).encode() + b"\r\n"
                            b"\r\n"
                        )
                        await response.write(part + frame_data + b"\r\n")
                except Exception:
                    pass

            await asyncio.sleep(0.3)
    except (ConnectionResetError, ConnectionAbortedError, asyncio.CancelledError):
        pass

    return response


async def handle_hud_cancel(request: web.Request) -> web.Response:
    """POST /v1/route/{routeName}/hud/cancel — abort a running HUD video render."""
    route_name = resolve_route_name(request)
    store = request.app["store"]
    route = store.get_route(route_name)
    fullname = route["fullname"] if route else route_name
    task = _hud_prerender_tasks.get(fullname)
    if not task:
        # No active task — still verify UI is running (may have been orphaned)
        asyncio.create_task(_ensure_manager())
        return web.json_response({"status": "idle"})

    proc = task.get("proc")
    if proc and proc.returncode is None:
        try:
            proc.terminate()
        except Exception:
            pass
        # Wait for process to exit so its finally block can run
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
        logger.info("HUD render cancelled for %s", fullname)

    # Clean up status file, cached output, and render HLS temp dir
    try:
        sf = Path(task["status_file"])
        if sf.exists():
            sf.unlink()
        out = Path(task["output"])
        if out.exists():
            out.unlink()
        if RENDER_HLS_DIR.exists():
            import shutil
            shutil.rmtree(RENDER_HLS_DIR, ignore_errors=True)
    except Exception:
        pass

    del _hud_prerender_tasks[fullname]

    # Verify production UI is restored (non-blocking — runs in background)
    asyncio.create_task(_ensure_manager())

    return web.json_response({"status": "cancelled"})


async def handle_hud_video(request: web.Request) -> web.Response:
    """GET /v1/route/{routeName}/hud/video — serve the rendered HUD MP4."""
    route_name = resolve_route_name(request)
    store = request.app["store"]
    route = store.get_route(route_name)
    fullname = route["fullname"] if route else route_name

    task = _hud_prerender_tasks.get(fullname)
    if not task:
        raise web.HTTPNotFound(text=json.dumps({"error": "No HUD render for this route"}))

    output_path = Path(task["output"])
    if not output_path.exists() or output_path.stat().st_size < 1000:
        raise web.HTTPNotFound(text=json.dumps({"error": "HUD video not ready"}))

    return web.FileResponse(
        output_path,
        headers={
            "Content-Type": "video/mp4",
            "Cache-Control": "public, max-age=86400",
        },
    )


# ─── HUD live streaming ──────────────────────────────────────────────

async def handle_hud_stream_start(request: web.Request) -> web.Response:
    """POST /v1/hud/stream/start — start HUD live streaming pipeline."""
    mgr: HudStreamManager = request.app.get("stream_manager")
    if not mgr:
        raise web.HTTPServiceUnavailable(
            text=json.dumps({"error": "Streaming not available on this device"}))

    try:
        body = await request.json()
    except Exception:
        body = {}

    route_name = body.get("route", "")
    if not route_name:
        raise web.HTTPBadRequest(text=json.dumps({"error": "Missing route name"}))

    route_name = route_name.replace("|", "/")
    store = request.app["store"]
    route = store.get_route(route_name)
    if not route:
        raise web.HTTPNotFound(
            text=json.dumps({"error": f"Route {route_name} not found"}))

    start_sec = body.get("start", 0)
    hd = bool(body.get("hd", False))

    # Ensure replay binary is available (auto-rebuild from scons cache if needed)
    if not await _ensure_replay_binary():
        raise web.HTTPServiceUnavailable(
            text=json.dumps({"error": "Replay binary not available and rebuild failed"}))

    mode = body.get("mode", "webrtc")  # "webrtc" (default), "ws" (WebSocket fMP4), or "hls" (legacy)
    if mode not in ("ws", "hls", "webrtc"):
        mode = "webrtc"

    await mgr.start(
        route_name=route["fullname"],
        local_id=route["_local_id"],
        dongle_id=route["dongle_id"],
        data_dir=str(store.data_dir),
        start_sec=start_sec,
        max_seg=route.get("maxqlog", -1),
        hd=hd,
        mode=mode,
    )

    return web.json_response(mgr.status)


async def handle_hud_stream_stop(request: web.Request) -> web.Response:
    """POST /v1/hud/stream/stop — stop HUD live streaming, restore compositor."""
    mgr: HudStreamManager = request.app.get("stream_manager")
    if mgr:
        await mgr.stop()
        # Verify production UI is restored after stream cleanup
        asyncio.create_task(_ensure_manager())
    return web.json_response({"status": "idle"})


async def handle_hud_stream_status(request: web.Request) -> web.Response:
    """GET /v1/hud/stream/status — check streaming pipeline status."""
    mgr: HudStreamManager = request.app.get("stream_manager")
    if not mgr:
        return web.json_response({"status": "idle"})
    return web.json_response(mgr.status)


async def handle_hud_stream_serve(request: web.Request) -> web.Response:
    """GET /v1/hud/stream/{filename} — serve HLS .m3u8 and .ts files."""
    filename = request.match_info["filename"]

    if not (filename.endswith(".m3u8") or filename.endswith(".ts")):
        raise web.HTTPForbidden(text="Only .m3u8 and .ts files allowed")

    filepath = HLS_DIR / filename
    if not filepath.exists():
        raise web.HTTPNotFound()

    if filename.endswith(".m3u8"):
        return web.Response(
            body=filepath.read_bytes(),
            content_type="application/vnd.apple.mpegurl",
            headers={"Cache-Control": "no-cache, no-store"},
        )
    else:
        return web.FileResponse(
            filepath,
            headers={
                "Content-Type": "video/mp2t",
                "Cache-Control": "public, max-age=60",
            },
        )


async def handle_hud_stream_offer(request: web.Request) -> web.Response:
    """POST /v1/hud/stream/offer — WebRTC SDP offer/answer exchange."""
    mgr: HudStreamManager = request.app.get("stream_manager")
    if not mgr or mgr._mode != "webrtc":
        return web.json_response(
            {"error": "No active WebRTC stream"}, status=400)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    offer_sdp = body.get("sdp", "")
    if not offer_sdp:
        return web.json_response({"error": "Missing SDP"}, status=400)

    answer_sdp = await mgr.create_webrtc_answer(offer_sdp)
    return web.json_response({"sdp": answer_sdp, "type": "answer"})


async def handle_hud_stream_ws(request: web.Request) -> web.WebSocketResponse:
    """GET /v1/hud/stream/ws — WebSocket endpoint for fMP4 HUD stream.

    The client connects after POST /v1/hud/stream/start (mode=ws).
    Server sends binary fMP4 chunks; client appends to MSE SourceBuffer.
    Client can send "stop" text message to terminate the stream.
    """
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    mgr: HudStreamManager = request.app.get("stream_manager")
    if not mgr or mgr._mode != "ws":
        await ws.send_json({"error": "No active WebSocket stream"})
        await ws.close()
        return ws

    logger.info("HUD WebSocket client connected")

    try:
        # Send fMP4 chunks until stream ends or client disconnects
        while not ws.closed and mgr.is_active and mgr._mode == "ws":
            chunk = await mgr.ws_get_chunk(timeout=2.0)
            if chunk is None:
                # Timeout — check if stream is still alive
                if not mgr.is_active:
                    break
                continue
            try:
                await ws.send_bytes(chunk)
            except ConnectionResetError:
                break
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error("HUD WebSocket error: %s", e)
    finally:
        logger.info("HUD WebSocket client disconnected")
        if not ws.closed:
            await ws.close()

    return ws


# ─── Screencast: play fcamera on C3 screen ───────────────────────────

SCREENCAST_SCRIPT = Path(__file__).parent.parent / "screencast.py"
SCREENCAST_CONTROL_PORT = 8090

# Module-level state for the screencast subprocess
_screencast_proc: subprocess.Popen | None = None


def _send_screencast_cmd(cmd: str):
    """Send a UDP control command to the screencast process."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.sendto(cmd.encode(), ("127.0.0.1", SCREENCAST_CONTROL_PORT))
    finally:
        sock.close()


def _screencast_alive() -> bool:
    global _screencast_proc
    return _screencast_proc is not None and _screencast_proc.poll() is None


async def handle_screencast_start(request: web.Request) -> web.Response:
    """POST /v1/screencast/start — play fcamera on C3 screen.

    Body: {"route": "local_id", "time": 123.4}
    """
    global _screencast_proc

    try:
        body = await request.json()
    except Exception:
        body = {}

    route_name = body.get("route", "")
    if not route_name:
        raise web.HTTPBadRequest(text=json.dumps({"error": "Missing route"}))

    route_name = route_name.replace("|", "/")
    store = request.app["store"]
    route = store.get_route(route_name)
    if not route:
        raise web.HTTPNotFound(text=json.dumps({"error": f"Route {route_name} not found"}))

    t = float(body.get("time", 0))
    segment = int(t // 60)
    offset = t % 60
    local_id = route["_local_id"]

    if _screencast_alive():
        # Already running — just send seek command
        _send_screencast_cmd(f"PLAY {local_id} {segment} {offset:.2f}")
        return web.json_response({"status": "playing", "route": local_id, "time": t})

    # Launch screencast process
    python_bin = PYTHON_BIN if os.path.isfile(PYTHON_BIN) else "python3"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(OPENPILOT_DIR)
    env["PATH"] = "/usr/local/venv/bin:/usr/local/bin:/usr/bin:/bin"

    _screencast_proc = subprocess.Popen(
        [python_bin, str(SCREENCAST_SCRIPT)],
        env=env,
        stdout=open("/tmp/screencast.log", "w"),
        stderr=subprocess.STDOUT,
    )

    # Wait for screencast to stop production UI and start listening (~4s)
    await asyncio.sleep(4)

    if not _screencast_alive():
        return web.json_response({"status": "error", "error": "Screencast process failed to start"}, status=500)

    # Send the initial PLAY command
    _send_screencast_cmd(f"PLAY {local_id} {segment} {offset:.2f}")

    return web.json_response({"status": "playing", "route": local_id, "time": t})


async def handle_screencast_seek(request: web.Request) -> web.Response:
    """POST /v1/screencast/seek — seek to time in current screencast.

    Body: {"route": "local_id", "time": 123.4}
    """
    if not _screencast_alive():
        return web.json_response({"status": "idle"})

    try:
        body = await request.json()
    except Exception:
        body = {}

    route_name = body.get("route", "")
    t = float(body.get("time", 0))
    segment = int(t // 60)
    offset = t % 60

    # Resolve local_id
    if route_name:
        route_name = route_name.replace("|", "/")
        store = request.app["store"]
        route = store.get_route(route_name)
        local_id = route["_local_id"] if route else route_name
    else:
        local_id = ""

    if local_id:
        _send_screencast_cmd(f"PLAY {local_id} {segment} {offset:.2f}")
    else:
        _send_screencast_cmd(f"PLAY _ {segment} {offset:.2f}")

    return web.json_response({"status": "playing", "time": t})


async def handle_screencast_pause(request: web.Request) -> web.Response:
    """POST /v1/screencast/pause"""
    if _screencast_alive():
        _send_screencast_cmd("PAUSE")
    return web.json_response({"status": "paused"})


async def handle_screencast_resume(request: web.Request) -> web.Response:
    """POST /v1/screencast/resume"""
    if _screencast_alive():
        _send_screencast_cmd("RESUME")
    return web.json_response({"status": "playing"})


async def handle_screencast_stop(request: web.Request) -> web.Response:
    """POST /v1/screencast/stop — stop screencast and restore production UI."""
    global _screencast_proc

    if _screencast_alive():
        _send_screencast_cmd("STOP")
        try:
            _screencast_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _screencast_proc.kill()
        _screencast_proc = None
    else:
        # Process already dead — make sure production UI is restored
        _screencast_proc = None
        asyncio.create_task(_ensure_manager())

    return web.json_response({"status": "idle"})


async def handle_screencast_status(request: web.Request) -> web.Response:
    """GET /v1/screencast/status"""
    if _screencast_alive():
        return web.json_response({"status": "playing"})
    return web.json_response({"status": "idle"})
