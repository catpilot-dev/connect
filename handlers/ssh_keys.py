"""SSH key management and WebRTC proxy handlers."""

import asyncio
import ipaddress
import json
import logging
import os
import re
import subprocess

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


def _ipv6_to_ipv4(ipv6_addr: str) -> str | None:
  """Map any IPv6 address to its IPv4 equivalent via the neighbor table.

  iOS Safari may connect to COD via link-local (fe80::) or global unicast IPv6
  (e.g. carrier IPv6 on a hotspot).  ICE candidates patched with an IPv6 address
  won't pair with webrtcd's IPv4 answer candidates → ICE fails.  Looking up the
  phone's IPv4 by MAC via the neighbor/ARP tables fixes the family mismatch.
  """
  try:
    ipv6 = ipv6_addr.split('%')[0]  # strip zone ID
    # Find MAC for the IPv6 address
    out = subprocess.run(['ip', '-6', 'neigh', 'show'], capture_output=True, text=True, timeout=1).stdout
    mac = None
    for line in out.splitlines():
      if ipv6 in line and 'lladdr' in line:
        parts = line.split()
        mac = parts[parts.index('lladdr') + 1]
        break
    if not mac:
      return None
    # Find IPv4 for that MAC in ARP table
    out = subprocess.run(['ip', '-4', 'neigh', 'show'], capture_output=True, text=True, timeout=1).stdout
    for line in out.splitlines():
      if mac in line:
        try:
          ipaddress.IPv4Address(line.split()[0])
          return line.split()[0]
        except ValueError:
          pass
  except Exception:
    pass
  return None


def _resolve_mdns_in_sdp(sdp: str, peer_ip: str) -> str:
  """Replace mDNS .local hostnames anywhere in SDP with the peer's real LAN IP.

  Apply globally (not just a=candidate: lines) because some browsers also put
  mDNS hostnames in the c= connection line or a=remote-candidates: attributes.
  """
  patched, count = _MDNS_RE.subn(peer_ip, sdp)
  if count:
    logger.info("webrtc: patched %d mDNS token(s) → %s", count, peer_ip)
  return patched


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


def _same_subnet_24(ip_a: str, ip_b: str) -> bool:
  """Return True if two IPv4 addresses are in the same /24 subnet."""
  try:
    return ip_a.rsplit('.', 1)[0] == ip_b.rsplit('.', 1)[0]
  except Exception:
    return False


def _filter_answer_candidates(data: dict, peer_ip: str) -> dict:
  """Strip srflx/relay candidates from webrtcd's answer when client and device
  share the same /24 subnet.  Same subnet means direct host-host UDP works;
  keeping srflx only creates lower-priority pairs that ICE wastes time on.
  Different subnet (hotspot via carrier NAT, remote access) → return unchanged
  so the public-IP fallback path is preserved.
  """
  sdp = data['sdp']
  # Extract device host candidate IP from the answer
  host_ip = None
  for line in sdp.splitlines():
    if line.startswith('a=candidate:') and ' typ host' in line:
      parts = line.split()
      try:
        host_ip = parts[4]  # a=candidate:foundation component protocol priority IP port ...
      except IndexError:
        pass
      break

  if not host_ip or not _same_subnet_24(peer_ip, host_ip):
    logger.info("webrtc: different subnet (%s vs %s), keeping srflx candidates", peer_ip, host_ip)
    return data

  # Same subnet — strip non-host candidates.
  # Preserve original line endings: WebRTC SDP must use CRLF (RFC 4566) and
  # Safari/WebKit will reject the answer with "invalid SDP line" if we emit LF only.
  line_end = '\r\n' if '\r\n' in sdp else '\n'
  lines = sdp.splitlines()
  filtered_lines = [l for l in lines if not (l.startswith('a=candidate:') and ' typ host' not in l)]
  filtered = line_end.join(filtered_lines)
  if sdp.endswith(line_end):
    filtered += line_end
  logger.info("webrtc: same subnet (%s/%s), stripped srflx — host-only ICE", peer_ip, host_ip)
  return {**data, 'sdp': filtered}


async def handle_webrtc(request: web.Request) -> web.Response:
    """POST /api/webrtc — proxy WebRTC signaling to local webrtcd."""
    import aiohttp
    body = await request.json()

    # Patch mDNS candidates in the offer SDP before forwarding to webrtcd.
    # If the phone connected via IPv6 link-local, map to IPv4 so ICE candidates
    # match webrtcd's IPv4 answer (mismatched families cause ~20s ICE delay).
    peer_ip = request.remote
    if peer_ip and ':' in peer_ip:  # any IPv6 (link-local or global unicast)
      ipv4 = _ipv6_to_ipv4(peer_ip)
      if ipv4:
        logger.info("webrtc: IPv6 %s → IPv4 %s for ICE candidates", peer_ip, ipv4)
        peer_ip = ipv4
    if peer_ip and isinstance(body.get('sdp'), str):
        patched_sdp = _resolve_mdns_in_sdp(body['sdp'], peer_ip)
        if patched_sdp != body['sdp']:
            body = dict(body)
            body['sdp'] = patched_sdp
        # DEBUG: log offer candidates to diagnose ICE failures
        for line in body['sdp'].splitlines():
            if line.startswith('a=candidate:'):
                logger.info("webrtc offer cand: %s", line)

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
                # DEBUG: log answer candidates
                for line in data.get('sdp', '').splitlines():
                    if line.startswith('a=candidate:'):
                        logger.info("webrtc answer cand: %s", line)
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


def _find_local_ipv4_for_peer(peer_ipv4: str) -> str | None:
  """Find the device's IPv4 address in the same /24 subnet as peer_ipv4.

  Used when the client connects via IPv6 and sockname returns an IPv6 address.
  We look up the peer's IPv4 via the neighbor table, then find the device's
  IPv4 on the same /24 so the TURN URL uses a valid IPv4 address.
  """
  try:
    peer_prefix = peer_ipv4.rsplit('.', 1)[0]
    out = subprocess.run(['ip', '-4', 'addr', 'show'], capture_output=True, text=True, timeout=1).stdout
    for line in out.splitlines():
      line = line.strip()
      if not line.startswith('inet '):
        continue
      ip = line.split()[1].split('/')[0]
      try:
        ipaddress.IPv4Address(ip)
        if ip.rsplit('.', 1)[0] == peer_prefix:
          return ip
      except ValueError:
        pass
  except Exception:
    pass
  return None


async def handle_ice_servers(request: web.Request) -> web.Response:
    """GET /api/ice-servers — return TURN config using the device's actual IPv4.

    Safari's ICE engine cannot resolve .local mDNS hostnames for TURN servers,
    so we return the device's actual IP (taken from the incoming socket) rather
    than relying on location.hostname which may be 'cateye.local'.

    When the client connects via IPv6 (e.g. iPhone on hotspot using link-local
    or global unicast), sockname returns the device's IPv6 address.  TURN URLs
    with bare IPv6 (no brackets) are invalid and cause RTCPeerConnection to
    throw a SyntaxError in Safari.  In that case we map the peer's IPv6 → IPv4
    via the neighbor table and then find the device's IPv4 in the same /24.
    """
    # The IP the client connected to — this is the device's interface IP on
    # whatever network (hotspot, home WiFi, etc.) the phone is using right now.
    sockname = request.transport.get_extra_info("sockname")
    device_ip = sockname[0] if sockname else "127.0.0.1"
    # Normalize IPv6-mapped IPv4 (::ffff:a.b.c.d → a.b.c.d)
    if device_ip.startswith("::ffff:"):
        device_ip = device_ip[7:]
    # If still IPv6, the client connected via a native IPv6 address.
    # TURN URLs need brackets for IPv6 (turn:[::1]:3478) but many browsers
    # misbehave with IPv6 TURN.  Better: resolve the peer's IPv4 via the
    # neighbor/ARP table and find our matching IPv4 on the same /24.
    if ':' in device_ip:
        peer_ip = (request.remote or '').split('%')[0]
        if ':' in peer_ip:
            peer_ipv4 = _ipv6_to_ipv4(peer_ip)
            if peer_ipv4:
                found = _find_local_ipv4_for_peer(peer_ipv4)
                if found:
                    logger.info("ice-servers: IPv6 device addr → IPv4 %s (peer %s → %s)",
                                found, peer_ip, peer_ipv4)
                    device_ip = found
        # Last resort: format as bracketed IPv6 so the URL is at least valid
        if ':' in device_ip:
            device_ip = f'[{device_ip}]'
    return web.json_response({
        "iceServers": [
            {
                "urls": f"turn:{device_ip}:3478",
                "username": "catpilot",
                "credential": "catpilot",
            },
            {"urls": "stun:stun.chat.bilibili.com:3478"},
            {"urls": "stun:stun.l.google.com:19302"},
        ]
    })


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
