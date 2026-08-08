"""Signal browser handlers — catalog and data extraction from logs."""

import asyncio
import logging

from aiohttp import web

from handler_helpers import error_response, resolve_route_name
from log_parser import extract_signal_catalog, extract_signal_data, extract_all_signals

logger = logging.getLogger("connect.signals")


def _parse_segments(seg_str: str) -> list[int]:
    """Parse segment specification: "3", "0-5", "0,1,3" → sorted list of ints."""
    seg_nums = []
    for part in seg_str.split(","):
        part = part.strip()
        if "-" in part:
            lo, hi = part.split("-", 1)
            seg_nums.extend(range(int(lo), int(hi) + 1))
        else:
            seg_nums.append(int(part))
    return sorted(seg_nums)


def _resolve_log_pairs(store, route_name, seg_nums):
    """Resolve (seg_num, log_path) pairs for given segments. Prefer qlog for speed."""
    local_id = store.get_local_id(route_name)
    if not local_id:
        return None
    data_dir = store.data_dir
    pairs = []
    for seg in seg_nums:
        seg_dir = data_dir / f"{local_id}--{seg}"
        for name in ("qlog.zst", "qlog", "rlog.zst", "rlog"):
            p = seg_dir / name
            if p.is_file():
                pairs.append((seg, str(p)))
                break
    return pairs


async def handle_signal_catalog(request):
    """GET /v1/route/{routeName}/signals/catalog?segments=0-5

    Returns inventory of all message types in the specified segments.
    """
    route_name = resolve_route_name(request)
    store = request.app["store"]

    route = store.get_route(route_name)
    if not route:
        return error_response(f"Route {route_name} not found", 404)

    seg_str = request.query.get("segments", "0")
    seg_nums = _parse_segments(seg_str)

    pairs = _resolve_log_pairs(store, route_name, seg_nums)
    if not pairs:
        return error_response("No log files found for requested segments", 404)

    loop = asyncio.get_event_loop()
    catalog = await loop.run_in_executor(None, extract_signal_catalog, pairs)

    return web.json_response({"msgTypes": catalog})


async def handle_signal_data(request):
    """GET /v1/route/{routeName}/signals/data/{msgType}/{segments}

    Extract all fields for a specific message type from the given segments.
    """
    route_name = resolve_route_name(request)
    msg_type = request.match_info["msgType"]
    seg_str = request.match_info["segments"]
    store = request.app["store"]

    route = store.get_route(route_name)
    if not route:
        return error_response(f"Route {route_name} not found", 404)

    seg_nums = _parse_segments(seg_str)
    pairs = _resolve_log_pairs(store, route_name, seg_nums)
    if not pairs:
        return error_response("No log files found for requested segments", 404)

    loop = asyncio.get_event_loop()
    samples = await loop.run_in_executor(None, extract_signal_data, pairs, msg_type)

    return web.json_response(samples)


async def handle_signal_all(request):
    """GET /v1/route/{routeName}/signals/all/{segments}

    Single-pass extraction of catalog + all signal data for the given segments.
    Returns {catalog: {...}, data: {msgType: [...], ...}}.
    """
    route_name = resolve_route_name(request)
    seg_str = request.match_info["segments"]
    store = request.app["store"]

    route = store.get_route(route_name)
    if not route:
        return error_response(f"Route {route_name} not found", 404)

    seg_nums = _parse_segments(seg_str)
    pairs = _resolve_log_pairs(store, route_name, seg_nums)
    if not pairs:
        return error_response("No log files found for requested segments", 404)

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, extract_all_signals, pairs)

    return web.json_response(result)
