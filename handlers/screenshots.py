"""Screenshot browsing handlers.

List, serve, and delete screen captures from /data/media/screenshots/.

HUD screenshots are saved by the screen_capture plugin with filename
capture_YYYYMMDD_HHMMSS.png. For bookmark export, we match by parsing
the filename timestamp (C3 clock may be unreliable, but the plugin
and rlog use the same clock, so relative matching works).
"""
import logging
import os
from datetime import datetime, timezone

from aiohttp import web

logger = logging.getLogger("connect")

SCREENSHOTS_DIR = os.getenv("SCREENSHOTS_DIR", "/data/media/screenshots")


async def handle_screenshots_list(request: web.Request) -> web.Response:
    """GET /v1/screenshots — list all screenshots, newest first."""
    if not os.path.isdir(SCREENSHOTS_DIR):
        return web.json_response([])

    files = []
    for name in os.listdir(SCREENSHOTS_DIR):
        if not name.lower().endswith('.png'):
            continue
        path = os.path.join(SCREENSHOTS_DIR, name)
        try:
            stat = os.stat(path)
            files.append({
                'filename': name,
                'size': stat.st_size,
                'mtime': stat.st_mtime,
            })
        except OSError:
            continue

    files.sort(key=lambda f: f['mtime'], reverse=True)
    return web.json_response(files)


async def handle_screenshot_serve(request: web.Request) -> web.Response:
    """GET /v1/screenshots/{filename} — serve a screenshot file."""
    filename = request.match_info['filename']

    # Prevent path traversal
    if '/' in filename or '\\' in filename or '..' in filename:
        return web.json_response({'error': 'invalid filename'}, status=400)

    path = os.path.join(SCREENSHOTS_DIR, filename)
    if not os.path.isfile(path):
        return web.json_response({'error': 'not found'}, status=404)

    return web.FileResponse(path, headers={
        'Content-Type': 'image/png',
        'Cache-Control': 'public, max-age=86400',
    })


async def handle_screenshot_delete(request: web.Request) -> web.Response:
    """DELETE /v1/screenshots/{filename} — delete a screenshot."""
    filename = request.match_info['filename']

    if '/' in filename or '\\' in filename or '..' in filename:
        return web.json_response({'error': 'invalid filename'}, status=400)

    path = os.path.join(SCREENSHOTS_DIR, filename)
    if not os.path.isfile(path):
        return web.json_response({'error': 'not found'}, status=404)

    try:
        os.remove(path)
        return web.json_response({'status': 'ok', 'filename': filename})
    except OSError as e:
        logger.error("Failed to delete screenshot %s: %s", filename, e)
        return web.json_response({'error': str(e)}, status=500)


def _parse_capture_epoch(filename: str) -> float | None:
    """Parse epoch from capture_YYYYMMDD_HHMMSS.png filename."""
    try:
        stem = filename.rsplit('.', 1)[0]  # capture_20260315_035151
        ts_str = stem.split('_', 1)[1]  # 20260315_035151
        dt = datetime.strptime(ts_str, '%Y%m%d_%H%M%S')
        return dt.timestamp()  # local time (same clock as rlog on device)
    except (ValueError, IndexError):
        return None


async def handle_screenshot_by_time(request: web.Request) -> web.Response:
    """GET /v1/screenshots/at/{epoch} — find and serve HUD PNG closest to epoch timestamp.

    Used by bookmark export: route.create_time + bookmark.time_sec = target epoch.
    Matches within 2-second tolerance (bookmark + capture happen in same second).
    """
    try:
        target = float(request.match_info['epoch'])
    except (ValueError, KeyError):
        return web.json_response({'error': 'invalid epoch'}, status=400)

    if not os.path.isdir(SCREENSHOTS_DIR):
        return web.json_response({'error': 'no screenshots'}, status=404)

    best_file = None
    best_delta = float('inf')

    for name in os.listdir(SCREENSHOTS_DIR):
        if not name.lower().endswith('.png'):
            continue
        epoch = _parse_capture_epoch(name)
        if epoch is None:
            continue
        delta = abs(epoch - target)
        if delta < best_delta:
            best_delta = delta
            best_file = name

    if best_file is None or best_delta > 2.0:
        return web.json_response({'error': 'no matching screenshot', 'closest_delta': best_delta}, status=404)

    path = os.path.join(SCREENSHOTS_DIR, best_file)
    return web.FileResponse(path, headers={
        'Content-Type': 'image/png',
        'Content-Disposition': f'attachment; filename="{best_file}"',
        'Cache-Control': 'public, max-age=86400',
    })
