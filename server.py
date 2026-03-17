#!/usr/bin/env python3
"""
Connect on Device - comma-compatible API server for local route browsing.

Implements the same REST API as api.comma.ai so the asiusai/connect React
frontend works unchanged. Serves route data from /data/media/0/realdata/.

Usage:
  On C3:  /usr/local/venv/bin/python /data/connect-on-device/server.py
  Local:  python server.py --data-dir ~/driving_data/data
"""

import argparse
import asyncio
import logging
from pathlib import Path

from aiohttp import web

from handlers import (
    cors_middleware,
    handle_auth,
    handle_connectdata,
    handle_dashboard_telemetry,
    handle_dashboard_ws,
    handle_signal_catalog,
    handle_signal_data,
    handle_signal_all,
    handle_device_get,
    handle_device_location,
    handle_device_stats,
    handle_devices,
    handle_hud_cancel,
    handle_hud_prerender,
    handle_hud_progress,
    handle_hud_stream_serve,
    handle_hud_stream_offer,
    handle_hud_stream_ws,
    handle_screencast_start,
    handle_screencast_seek,
    handle_screencast_pause,
    handle_screencast_resume,
    handle_screencast_stop,
    handle_screencast_status,
    handle_hud_stream_start,
    handle_hud_stream_status,
    handle_hud_stream_stop,
    handle_hud_video,
    handle_me,
    handle_models_active,
    handle_models_check_updates,
    handle_models_download,
    handle_models_list,
    handle_models_swap,
    handle_params_get,
    handle_params_set,
    handle_lateral_delay,
    handle_device_info,
    handle_device_is_onroad,
    handle_device_reboot,
    handle_device_poweroff,
    handle_device_language,
    handle_toggles_get,
    handle_toggles_set,
    handle_software_get,
    handle_software_check,
    handle_software_download,
    handle_software_install,
    handle_software_branch,
    handle_software_uninstall,
    handle_software_prepare_plugins,
    handle_venv_sync,
    handle_preserved_routes,
    handle_route_delete,
    handle_route_download,
    handle_route_enrich,
    handle_route_files,
    handle_route_scan,
    handle_route_get,
    handle_route_manifest,
    handle_route_note,
    handle_route_bookmark_add,
    handle_route_bookmark_update,
    handle_route_bookmark_delete,
    handle_route_preserve,
    handle_route_unpreserve,
    handle_routes_list,
    handle_routes_segments,
    handle_camera_segment,
    handle_qcamera_hls_manifest,
    handle_qcamera_hls_segment,
    handle_frame,
    handle_screenshot,
    handle_share_signature,
    handle_spa,
    handle_storage,
    handle_stub_empty_array,
    handle_stub_error,
    handle_tile_cancel,
    handle_tile_delete,
    handle_tile_download,
    handle_tile_list,
    handle_tile_progress,
    handle_mapd_check_update,
    handle_mapd_update,
    handle_plugins_get,
    handle_plugin_toggle,
    handle_plugin_param,
    handle_plugin_repo_get,
    handle_plugin_repo_set,
    handle_plugin_repo_install,
    handle_screenshots_list,
    handle_screenshot_serve,
    handle_screenshot_delete,
    handle_screenshot_by_time,
    handle_updates_check,
    handle_updates_apply,
    handle_ssh_keys_get,
    handle_ssh_keys_set,
    handle_ssh_keys_delete,
    handle_webrtc,
    handle_webrtc_health,
    handle_driving_ws,
    handle_home_ws,
)
from hud_stream import HudStreamManager, is_available as hud_stream_available
from route_store import DEFAULT_DATA_DIR, DEFAULT_PORT, RouteStore
from storage_management import run_cleanup

logger = logging.getLogger("connect")


# ─── Lifecycle hooks ──────────────────────────────────────────────────

async def _cleanup_worker(app: web.Application):
    """Periodic storage cleanup — runs every 5 minutes."""
    store: RouteStore = app["store"]
    while True:
        await asyncio.sleep(300)
        try:
            result = run_cleanup(store)
            if result["deleted"]:
                logger.info("Cleanup: %s", result)
        except Exception:
            logger.exception("Cleanup error")


async def _startup(app: web.Application):
    """Initial route scan on server startup."""
    store: RouteStore = app["store"]
    store.scan(force=True)
    logger.info("Route scan complete, %d routes found", len(store._routes))

    # Clean up stale render HLS temp dir (persists on /data across reboots)
    from config import COD_HLS_TMP_DIR
    hls_tmp = Path(COD_HLS_TMP_DIR)
    if hls_tmp.exists():
        import shutil
        shutil.rmtree(hls_tmp, ignore_errors=True)
        logger.info("Cleaned up stale HLS temp dir")

    # Start periodic storage cleanup task
    app["cleanup_task"] = asyncio.create_task(_cleanup_worker(app))

    # Initialize HUD stream manager if C3 binaries are available
    if hud_stream_available():
        app["stream_manager"] = HudStreamManager()
        logger.info("HUD live streaming available")
    else:
        logger.info("HUD live streaming not available (missing C3 binaries)")


async def _shutdown(app: web.Application):
    """Clean shutdown — stop any active HUD stream and cleanup task."""
    task = app.get("cleanup_task")
    if task:
        task.cancel()

    mgr = app.get("stream_manager")
    if mgr and mgr.is_active:
        logger.info("Stopping active HUD stream on shutdown...")
        await mgr.stop()


# ─── App factory ──────────────────────────────────────────────────────

def create_app(data_dir: str, static_dir: str) -> web.Application:
    app = web.Application(middlewares=[cors_middleware])

    store = RouteStore(data_dir)
    app["store"] = store
    app["static_dir"] = Path(static_dir)

    app.on_startup.append(_startup)
    app.on_shutdown.append(_shutdown)

    # ── comma-compatible API ──
    # Auth
    app.router.add_get("/v1/me/", handle_me)
    app.router.add_post("/v2/auth/", handle_auth)

    # Devices
    app.router.add_get("/v1/me/devices/", handle_devices)
    app.router.add_get("/v1.1/devices/{dongleId}/", handle_device_get)
    app.router.add_get("/v1.1/devices/{dongleId}/stats", handle_device_stats)
    app.router.add_get("/v1/devices/{dongleId}/location", handle_device_location)
    app.router.add_get("/v1/devices/{dongleId}/routes/preserved", handle_preserved_routes)

    # Routes
    app.router.add_get("/v1/devices/{dongleId}/routes", handle_routes_list)
    app.router.add_get("/v1/devices/{dongleId}/routes_segments", handle_routes_segments)

    # Route detail
    app.router.add_get("/v1/route/{routeName}/", handle_route_get)
    app.router.add_delete("/v1/route/{routeName}/", handle_route_delete)
    app.router.add_get("/v1/route/{routeName}/files", handle_route_files)
    app.router.add_get("/v1/route/{routeName}/manifest.m3u8", handle_route_manifest)
    app.router.add_get("/v1/route/{routeName}/share_signature", handle_share_signature)
    app.router.add_post("/v1/route/{routeName}/note", handle_route_note)
    app.router.add_post("/v1/route/{routeName}/bookmark", handle_route_bookmark_add)
    app.router.add_put("/v1/route/{routeName}/bookmark/{index}", handle_route_bookmark_update)
    app.router.add_delete("/v1/route/{routeName}/bookmark/{index}", handle_route_bookmark_delete)
    app.router.add_post("/v1/route/{routeName}/preserve", handle_route_preserve)
    app.router.add_delete("/v1/route/{routeName}/preserve", handle_route_unpreserve)
    app.router.add_get("/v1/route/{routeName}/download", handle_route_download)
    app.router.add_post("/v1/route/{routeName}/enrich", handle_route_enrich)
    app.router.add_post("/v1/route/{routeName}/scan", handle_route_scan)
    app.router.add_post("/v1/route/{routeName}/screenshot", handle_screenshot)
    app.router.add_get("/v1/route/{routeName}/frame", handle_frame)
    app.router.add_get("/v1/route/{routeName}/camera/{camera_type}/{segment}", handle_camera_segment)
    app.router.add_get("/v1/route/{routeName}/qcamera.m3u8", handle_qcamera_hls_manifest)
    app.router.add_get("/v1/route/{routeName}/qcamera_hls/{filename}", handle_qcamera_hls_segment)

    # HUD video rendering (pre-render to MP4)
    app.router.add_post("/v1/route/{routeName}/hud/prerender", handle_hud_prerender)
    app.router.add_post("/v1/route/{routeName}/hud/cancel", handle_hud_cancel)
    app.router.add_get("/v1/route/{routeName}/hud/progress", handle_hud_progress)
    app.router.add_get("/v1/route/{routeName}/hud/video", handle_hud_video)

    # HUD live streaming (DRM replay → HLS)
    app.router.add_post("/v1/hud/stream/start", handle_hud_stream_start)
    app.router.add_post("/v1/hud/stream/stop", handle_hud_stream_stop)
    app.router.add_get("/v1/hud/stream/status", handle_hud_stream_status)
    app.router.add_post("/v1/hud/stream/offer", handle_hud_stream_offer)
    app.router.add_get("/v1/hud/stream/ws", handle_hud_stream_ws)
    app.router.add_get("/v1/hud/stream/{filename}", handle_hud_stream_serve)

    # Screencast: play fcamera on C3 screen, controlled from mobile
    app.router.add_post("/v1/screencast/start", handle_screencast_start)
    app.router.add_post("/v1/screencast/seek", handle_screencast_seek)
    app.router.add_post("/v1/screencast/pause", handle_screencast_pause)
    app.router.add_post("/v1/screencast/resume", handle_screencast_resume)
    app.router.add_post("/v1/screencast/stop", handle_screencast_stop)
    app.router.add_get("/v1/screencast/status", handle_screencast_status)

    # Stubs for endpoints the frontend may query
    app.router.add_get("/v1/devices/{dongleId}/athena_offline_queue", handle_stub_empty_array)
    app.router.add_get("/v1/devices/{dongleId}/bootlogs", handle_stub_empty_array)
    app.router.add_get("/v1/devices/{dongleId}/crashlogs", handle_stub_empty_array)
    app.router.add_get("/v1/devices/{dongleId}/users", handle_stub_empty_array)
    app.router.add_get("/v1/devices/{dongleId}/firehose_stats", handle_stub_error)
    app.router.add_post("/v1/devices/{dongleId}/unpair", handle_stub_error)
    app.router.add_get("/v1/devices/{dongleId}/", handle_device_get)
    app.router.add_get("/v1/prime/subscription", handle_stub_error)
    app.router.add_get("/v1/prime/subscribe_info", handle_stub_error)
    app.router.add_get("/v1/storage", handle_storage)
    app.router.add_get("/health", lambda r: web.json_response({"status": "ok"}))

    # BMW params
    app.router.add_get("/v1/params", handle_params_get)
    app.router.add_post("/v1/params", handle_params_set)
    app.router.add_get("/v1/lateral-delay", handle_lateral_delay)

    # Device panel
    app.router.add_get("/v1/device", handle_device_info)
    app.router.add_get("/v1/device/isOnroad", handle_device_is_onroad)
    app.router.add_post("/v1/device/reboot", handle_device_reboot)
    app.router.add_post("/v1/device/poweroff", handle_device_poweroff)
    app.router.add_post("/v1/device/language", handle_device_language)

    # Toggles panel
    app.router.add_get("/v1/toggles", handle_toggles_get)
    app.router.add_post("/v1/toggles", handle_toggles_set)

    # Software update management
    app.router.add_get("/v1/software", handle_software_get)
    app.router.add_post("/v1/software/check", handle_software_check)
    app.router.add_post("/v1/software/download", handle_software_download)
    app.router.add_post("/v1/software/install", handle_software_install)
    app.router.add_post("/v1/software/branch", handle_software_branch)
    app.router.add_post("/v1/software/uninstall", handle_software_uninstall)
    app.router.add_post("/v1/software/prepare-plugins", handle_software_prepare_plugins)
    app.router.add_post("/v1/software/venv-sync", handle_venv_sync)

    # Model management
    app.router.add_get("/v1/models/active", handle_models_active)
    app.router.add_get("/v1/models", handle_models_list)
    app.router.add_post("/v1/models/swap", handle_models_swap)
    app.router.add_post("/v1/models/check-updates", handle_models_check_updates)
    app.router.add_post("/v1/models/download", handle_models_download)

    # OSM tile management (mapd offline data)
    app.router.add_get("/v1/mapd/tiles", handle_tile_list)
    app.router.add_post("/v1/mapd/tiles/download", handle_tile_download)
    app.router.add_get("/v1/mapd/tiles/progress", handle_tile_progress)
    app.router.add_post("/v1/mapd/tiles/cancel", handle_tile_cancel)
    app.router.add_delete("/v1/mapd/tiles/{lat}/{lon}", handle_tile_delete)

    # Mapd binary update
    app.router.add_post("/v1/mapd/check-update", handle_mapd_check_update)
    app.router.add_post("/v1/mapd/update", handle_mapd_update)

    # Plugins — repo routes MUST come before {plugin_id} catch-all
    app.router.add_get("/v1/plugins/repo", handle_plugin_repo_get)
    app.router.add_post("/v1/plugins/repo", handle_plugin_repo_set)
    app.router.add_post("/v1/plugins/repo/install", handle_plugin_repo_install)
    app.router.add_get("/v1/plugins", handle_plugins_get)
    app.router.add_post("/v1/plugins/{plugin_id}/toggle", handle_plugin_toggle)
    app.router.add_post("/v1/plugins/{plugin_id}/param", handle_plugin_param)

    # COD + plugin update notifications
    app.router.add_get("/v1/updates/check", handle_updates_check)
    app.router.add_post("/v1/updates/apply", handle_updates_apply)

    # WebRTC signaling proxy (to local webrtcd on port 5001)
    # SSH keys
    app.router.add_get("/v1/ssh-keys", handle_ssh_keys_get)
    app.router.add_post("/v1/ssh-keys", handle_ssh_keys_set)
    app.router.add_delete("/v1/ssh-keys", handle_ssh_keys_delete)

    app.router.add_post("/api/webrtc", handle_webrtc)
    app.router.add_get("/api/webrtc/health", handle_webrtc_health)

    # Dashboard telemetry (replay REST + live WebSocket)
    app.router.add_get("/v1/dashboard/telemetry/{routeName}/{segments}", handle_dashboard_telemetry)
    app.router.add_get("/ws/dashboard", handle_dashboard_ws)

    # Driving page (live camera + HUD overlay WebSocket)
    app.router.add_get("/ws/driving", handle_driving_ws)

    # Home page (offroad device status WebSocket)
    app.router.add_get("/ws/home", handle_home_ws)

    # Signal browser (catalog + data extraction)
    app.router.add_get("/v1/route/{routeName}/signals/catalog", handle_signal_catalog)
    app.router.add_get("/v1/route/{routeName}/signals/data/{msgType}/{segments}", handle_signal_data)
    app.router.add_get("/v1/route/{routeName}/signals/all/{segments}", handle_signal_all)

    # Screenshots (screen capture plugin images)
    app.router.add_get("/v1/screenshots", handle_screenshots_list)
    app.router.add_get("/v1/screenshots/at/{epoch}", handle_screenshot_by_time)
    app.router.add_get("/v1/screenshots/{filename}", handle_screenshot_serve)
    app.router.add_delete("/v1/screenshots/{filename}", handle_screenshot_delete)

    # Media file serving
    app.router.add_get("/connectdata/{path:.*}", handle_connectdata)

    # SPA fallback — serves static files if they exist, otherwise index.html
    app.router.add_get("/{path:.*}", handle_spa)

    return app


def main():
    parser = argparse.ArgumentParser(description="Connect on Device - comma-compatible local server")
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR,
                        help=f"Route data directory (default: {DEFAULT_DATA_DIR})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"Server port (default: {DEFAULT_PORT})")
    parser.add_argument("--host", default="0.0.0.0",
                        help="Bind address (default: 0.0.0.0)")
    parser.add_argument("--static-dir", default=None,
                        help="Static files directory (default: ./static next to server.py)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    static_dir = args.static_dir or str(Path(__file__).parent / "static")

    logger.info("Starting Connect on Device (comma-compatible API)")
    logger.info("  Data dir:   %s", args.data_dir)
    logger.info("  Static dir: %s", static_dir)
    logger.info("  Listening:  http://%s:%d", args.host, args.port)

    app = create_app(args.data_dir, static_dir)
    web.run_app(app, host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
