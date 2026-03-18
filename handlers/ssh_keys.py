"""SSH key management and WebRTC proxy handlers."""

import asyncio
import json
import logging
import os
import re

from aiohttp import web

from handler_helpers import PARAMS_DIR, error_response, read_param, write_param

logger = logging.getLogger("connect")

# iOS Safari 14+ anonymizes ICE host candidates with mDNS UUID.local hostnames
# (e.g. "e3f4a5b6-c7d8-90ab-cdef-1234.local"). aioice cannot resolve these on
# hotspot/LAN because multicast mDNS packets are not reliably forwarded by the
# iPhone hotspot. The result: all host candidates are silently dropped and only
# STUN-reflexive (public) candidates remain, which are unreachable on LAN → ICE
# fails after ~60s timeout.
#
# Fix: replace .local hostnames in the offer SDP with the phone's actual LAN IP,
# which is already known from the HTTP request source address.
_MDNS_RE = re.compile(r'[0-9a-fA-F-]{8,}\.local')


def _resolve_mdns_in_sdp(sdp: str, peer_ip: str) -> str:
  """Replace mDNS .local hostnames in ICE candidates with the peer's real LAN IP."""
  patched_lines = []
  count = 0
  for line in sdp.splitlines(keepends=True):
    if line.startswith('a=candidate:') and '.local' in line:
      line, n = _MDNS_RE.subn(peer_ip, line)
      count += n
    patched_lines.append(line)
  if count:
    logger.info("webrtc: patched %d mDNS candidate(s) → %s", count, peer_ip)
  return ''.join(patched_lines)


async def handle_ssh_keys_get(request: web.Request) -> web.Response:
    """GET /v1/ssh-keys — read GithubUsername."""
    username = read_param("GithubUsername")
    keys = read_param("GithubSshKeys")
    has_keys = len(keys) > 0
    return web.json_response({"username": username, "has_keys": has_keys})


async def handle_ssh_keys_set(request: web.Request) -> web.Response:
    """POST /v1/ssh-keys — fetch GitHub keys for username and store them."""
    import aiohttp
    body = await request.json()
    username = body.get("username", "").strip()
    if not username:
        raise web.HTTPBadRequest(text=json.dumps({"error": "username required"}))
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://github.com/{username}.keys", timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return error_response(f"GitHub user '{username}' not found", 404)
                keys = await resp.text()
    except asyncio.TimeoutError:
        return error_response("Request timed out", 504)
    except Exception as e:
        return error_response(str(e), 502)
    if not keys.strip():
        return error_response(f"User '{username}' has no keys on GitHub", 404)
    write_param("GithubUsername", username)
    write_param("GithubSshKeys", keys)
    return web.json_response({"status": "ok", "username": username, "has_keys": True})


async def handle_ssh_keys_delete(request: web.Request) -> web.Response:
    """DELETE /v1/ssh-keys — remove stored SSH keys."""
    for param in ("GithubUsername", "GithubSshKeys"):
        path = f"{PARAMS_DIR}/{param}"
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
    return web.json_response({"status": "ok", "username": "", "has_keys": False})


async def handle_webrtc(request: web.Request) -> web.Response:
    """POST /api/webrtc — proxy WebRTC signaling to local webrtcd."""
    import aiohttp
    body = await request.json()

    # Patch mDNS candidates in the offer SDP before forwarding to webrtcd
    peer_ip = request.remote
    if peer_ip and isinstance(body.get('sdp'), str):
        patched_sdp = _resolve_mdns_in_sdp(body['sdp'], peer_ip)
        if patched_sdp != body['sdp']:
            body = dict(body)
            body['sdp'] = patched_sdp

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "http://localhost:5001/stream", json=body, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.error("webrtcd returned %d: %s", resp.status, text[:200])
                    return error_response(f"webrtcd error {resp.status}: {text[:200]}", resp.status)
                data = await resp.json()
                return web.json_response(data)
    except aiohttp.ClientConnectorError:
        logger.error("webrtcd not reachable on localhost:5001 — is it running?")
        return error_response("webrtcd not running (port 5001 unreachable)", 502)
    except asyncio.TimeoutError:
        logger.error("webrtcd signaling timeout after 10s")
        return error_response("webrtcd signaling timeout", 504)
    except Exception as e:
        logger.error("WebRTC proxy error: %s", e)
        return error_response(f"webrtcd error: {e}", 502)


async def handle_webrtc_health(request: web.Request) -> web.Response:
    """GET /api/webrtc/health — check if webrtcd is reachable."""
    import aiohttp
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "http://localhost:5001/schema", timeout=aiohttp.ClientTimeout(total=2)
            ) as resp:
                await resp.read()
                return web.json_response({"status": "ok"})
    except Exception:
        return web.json_response({"status": "unavailable"}, status=503)
