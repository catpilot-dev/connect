"""UI stream endpoints for STREAM_UI mode (phone display via MJPEG).

application.py writes JPEG frames atomically to /tmp/ui_stream.jpg at ~10 fps.

  GET /stream/ui        — multipart/x-mixed-replace MJPEG (Chrome/Firefox)
  GET /stream/ui/frame  — single JPEG frame (iOS Safari polling via canvas)

Works entirely over local network — no WebRTC, no TURN server needed.
"""

import asyncio
import os

from aiohttp import web

STREAM_UI_JPG = "/tmp/ui_stream.jpg"
_BOUNDARY = "frame"
_POLL_INTERVAL = 0.05  # 50ms poll (20Hz max, actual rate limited by encoder ~10fps)
_FRAME_TIMEOUT = 5.0   # Stop MJPEG if no new frame for this many seconds


async def handle_ui_stream(request: web.Request) -> web.StreamResponse:
    """Serve /tmp/ui_stream.jpg as MJPEG multipart/x-mixed-replace."""
    response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": f"multipart/x-mixed-replace; boundary={_BOUNDARY}",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering if proxied
        },
    )
    await response.prepare(request)

    last_mtime: float = 0.0
    idle_since: float = asyncio.get_event_loop().time()

    while not request.transport.is_closing():
        try:
            stat = os.stat(STREAM_UI_JPG)
        except FileNotFoundError:
            await asyncio.sleep(_POLL_INTERVAL)
            if asyncio.get_event_loop().time() - idle_since > _FRAME_TIMEOUT:
                break
            continue

        if stat.st_mtime == last_mtime:
            await asyncio.sleep(_POLL_INTERVAL)
            if asyncio.get_event_loop().time() - idle_since > _FRAME_TIMEOUT:
                break
            continue

        try:
            with open(STREAM_UI_JPG, "rb") as f:
                jpeg = f.read()
        except OSError:
            # Race with atomic rename — skip and retry
            await asyncio.sleep(_POLL_INTERVAL)
            continue

        last_mtime = stat.st_mtime
        idle_since = asyncio.get_event_loop().time()

        header = (
            f"--{_BOUNDARY}\r\n"
            f"Content-Type: image/jpeg\r\n"
            f"Content-Length: {len(jpeg)}\r\n"
            "\r\n"
        ).encode()
        try:
            await response.write(header + jpeg + b"\r\n")
        except Exception:
            break

    return response


async def handle_ui_stream_frame(request: web.Request) -> web.Response:
    """Return the latest UI JPEG frame (for iOS Safari canvas polling)."""
    try:
        with open(STREAM_UI_JPG, "rb") as f:
            jpeg = f.read()
    except FileNotFoundError:
        raise web.HTTPNotFound(reason="STREAM_UI not active")
    return web.Response(
        body=jpeg,
        content_type="image/jpeg",
        headers={"Cache-Control": "no-cache, no-store"},
    )
