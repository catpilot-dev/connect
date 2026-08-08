import asyncio
import json
import logging
import subprocess
import time
from pathlib import Path

from aiohttp import web

from handler_helpers import get_route_or_404
from log_parser import _generate_coords_json, _generate_events_json
from route_helpers import _base_url, _clean_route, _resolve_local_id, _route_bookmarks, _route_engaged_distance, _route_timeline_summary, _set_route_url
from route_store import _route_counter
from storage_management import DOWNLOAD_FILES, build_download_tar

logger = logging.getLogger("connect")


async def handle_routes_list(request: web.Request) -> web.Response:
    """GET /v1/devices/{dongleId}/routes — filtered, paginated route list.

    Query params:
      filter: recent (default) | saved | all | recycled
      limit: max results per page (default 5)
      offset: skip this many results before the page (default 0) — used for
              classic Previous/Next pagination across all tabs
      before_counter: pagination cursor (all tab only)
      after_gps: Unix epoch — only routes with gps_time >= this (all tab)
      before_gps: Unix epoch — only routes with gps_time <= this (all tab)

    Returns known routes plus pending placeholders (pending=true) for
    routes not yet scanned. Frontend shows spinner cards for pending items.
    The full candidate list is built and sorted per tab, then sliced to
    [offset : offset+limit] — the expensive per-route cleaning runs only on
    the returned slice.
    """
    store = request.app["store"]
    routes = await store.async_scan()

    tab = request.query.get("filter", "recent")
    limit = int(request.query.get("limit", 5))
    offset = max(0, int(request.query.get("offset", 0)))
    before_counter = int(request.query.get("before_counter", 999999999))

    # GPS date range filter (applies to all tabs)
    after_gps = request.query.get("after_gps")
    before_gps = request.query.get("before_gps")
    after_gps = float(after_gps) if after_gps else None
    before_gps = float(before_gps) if before_gps else None

    def _gps_in_range(local_id: str) -> bool:
        """Check if route's gps_time falls within the requested date range."""
        meta = store._metadata.get(local_id, {})
        gps_time = meta.get("gps_time")
        if not gps_time:
            return False
        if after_gps and gps_time < after_gps:
            return False
        if before_gps and gps_time > before_gps:
            return False
        return True

    # ── Recycled tab: hidden + invalid routes ──
    if tab == "recycled":
        recycled = store.get_recycled_routes()
        filtered = []
        for r in recycled:
            lid = r.get("_local_id", "")
            if after_gps or before_gps:
                meta = store._metadata.get(lid, {})
                gps_time = meta.get("gps_time")
                if gps_time:
                    if after_gps and gps_time < after_gps:
                        continue
                    if before_gps and gps_time > before_gps:
                        continue
            filtered.append(r)
        filtered.sort(key=lambda r: _route_counter(r.get("_local_id", "")), reverse=True)

        route_list = []
        for r in filtered[offset:offset + limit]:
            lid = r.get("_local_id", "")
            r_with_url = _set_route_url(r, request)
            cleaned = _clean_route(r_with_url)
            cleaned["route_counter"] = _route_counter(lid)
            cleaned["recycled_reason"] = r["recycled_reason"]
            route_list.append(cleaned)
        return web.json_response(route_list)

    # ── Saved tab: preserved routes only ──
    if tab == "saved":
        sorted_routes = sorted(
            routes.values(),
            key=lambda r: _route_counter(r.get("_local_id", "")),
            reverse=True,
        )
        filtered = [
            r for r in sorted_routes
            if store.is_preserved(r["_local_id"]) and _gps_in_range(r["_local_id"])
        ]

        route_list = []
        for r in filtered[offset:offset + limit]:
            r_with_url = _set_route_url(r, request)
            cleaned = _clean_route(r_with_url)
            cleaned["route_counter"] = _route_counter(r.get("_local_id", ""))
            cleaned["is_preserved"] = True
            if cleaned.get("engagement_pct") is not None:
                timeline = _route_timeline_summary(r)
                if timeline is not None:
                    cleaned["timeline"] = timeline
            route_list.append(cleaned)
        return web.json_response(route_list)

    # ── Recent / All tabs: merged known + pending ──
    pending = store.get_pending_route_ids()

    items = []
    for r in routes.values():
        counter = _route_counter(r.get("_local_id", ""))
        if not _gps_in_range(r["_local_id"]):
            continue
        items.append(("route", counter, r))

    # Include pending items for recent and all tabs
    if tab in ("recent", "all"):
        for p in pending:
            items.append(("pending", p["counter"], p))

    items.sort(key=lambda x: x[1], reverse=True)

    # Legacy cursor filter (all tab only) — applied before the offset slice so
    # both before_counter and offset/limit pagination continue to work.
    if tab == "all":
        items = [it for it in items if it[1] < before_counter]

    route_list = []
    for kind, counter, data in items[offset:offset + limit]:
        if kind == "route":
            r_with_url = _set_route_url(data, request)
            cleaned = _clean_route(r_with_url)
            cleaned["route_counter"] = counter
            cleaned["is_preserved"] = store.is_preserved(data["_local_id"])
            if cleaned.get("engagement_pct") is not None:
                timeline = _route_timeline_summary(data)
                if timeline is not None:
                    cleaned["timeline"] = timeline
            route_list.append(cleaned)
        else:
            route_list.append({
                "local_id": data["local_id"],
                "route_counter": counter,
                "pending": True,
                "seg_count": data["seg_count"],
            })

    return web.json_response(route_list)


async def handle_routes_segments(request: web.Request) -> web.Response:
    """GET /v1/devices/{dongleId}/routes_segments — route with segment data"""
    store = request.app["store"]
    routes = await store.async_scan()

    route_str = request.query.get("route_str", "")
    limit = int(request.query.get("limit", 100))

    # On-demand enrichment when viewing a specific route
    if route_str:
        local_id = store.get_local_id(route_str)
        if local_id:
            loop = asyncio.get_event_loop()
            enriched = await loop.run_in_executor(None, store.ensure_enriched, local_id)
            if enriched:
                routes = await store.async_scan()

    sorted_routes = sorted(routes.values(),
                           key=lambda r: _route_counter(r.get("_local_id", "")),
                           reverse=True)

    results = []
    for r in sorted_routes:
        fullname = r["fullname"]
        if route_str and fullname != route_str:
            continue
        r_with_url = _set_route_url(r, request)
        seg = _clean_route(r_with_url)
        seg.update({
            "end_time_utc_millis": int((r["create_time"] + (r["maxqlog"] + 1) * 60) * 1000),
            "is_preserved": store.is_preserved(r["_local_id"]),
            "segment_end_times": [int(t) for t in r["_seg_end_times"]],
            "segment_numbers": r["_seg_numbers"],
            "segment_start_times": [int(t) for t in r["_seg_start_times"]],
            "share_exp": "9999999999",
            "share_sig": "local",
            "start_time_utc_millis": int(r["create_time"] * 1000),
        })
        results.append(seg)
        if len(results) >= limit:
            break

    return web.json_response(results)


async def handle_preserved_routes(request: web.Request) -> web.Response:
    """GET /v1/devices/{dongleId}/routes/preserved — return preserved routes"""
    store = request.app["store"]
    routes = await store.async_scan()
    preserved = []
    for r in sorted(routes.values(),
                    key=lambda r: _route_counter(r.get("_local_id", "")),
                    reverse=True):
        if store.is_preserved(r["_local_id"]):
            r_with_url = _set_route_url(r, request)
            cleaned = _clean_route(r_with_url)
            cleaned["is_preserved"] = True
            preserved.append(cleaned)
    return web.json_response(preserved)


# ─── Route detail handlers ───────────────────────────────────────────

async def handle_route_get(request: web.Request) -> web.Response:
    """GET /v1/route/{routeName}/ — single route detail (no auto-enrichment)"""
    route_name, route, store = get_route_or_404(request)

    local_id = route["_local_id"]
    meta = store._metadata.get(local_id, {})

    # Only report events as cached if route is fully enriched — stale events.json
    # from a previous session shouldn't bypass the Enrich button for re-scanned routes
    max_seg = route.get("maxqlog", 0)
    events_cached = meta.get("enriched", False) and all(
        (store.data_dir / f"{local_id}--{i}" / "events.json").exists()
        for i in range(max_seg + 1)
    )

    # Opportunistic: compute distance-based engagement % if coords + events are
    # cached. Migration: legacy time-based values (no engagement_metric_version)
    # are recomputed. Version is stamped even when no GPS is available so that
    # routes without coords don't re-trigger compute on every view. When migrating
    # without coords, clear the stale time-based pct so the API hides the value.
    if events_cached and meta:
        is_legacy = meta.get("engagement_metric_version") != 2
        needs_compute = is_legacy or meta.get("engagement_pct") is None
        if needs_compute:
            engaged_m, total_m = _route_engaged_distance(store, route)
            if total_m > 0:
                meta["engagement_pct"] = round(engaged_m / total_m * 100)
            elif is_legacy:
                meta["engagement_pct"] = None
            meta["engagement_metric_version"] = 2
            store._rebuild_routes()
            store._save_metadata()
            route = store.get_route(route_name)

        # Geocode in background — don't block the response (Nominatim rate limit = 2+ sec)
        needs_geocode = meta.get("start_address") is None or meta.get("end_address") is None
        if needs_geocode and meta.get("gps_coordinates"):
            loop = asyncio.get_event_loop()
            loop.run_in_executor(None, store.geocode_route, local_id)

    # Auto-import drive bookmarks from events.json into metadata bookmarks
    if events_cached and not meta.get("drive_bookmarks_imported"):
        drive_bm = _route_bookmarks(route)
        if drive_bm:
            existing = meta.get("bookmarks", [])
            # Normalize old-format bookmarks (plain numbers → dicts)
            existing = [b if isinstance(b, dict) else {"time_sec": b, "label": ""} for b in existing]
            existing_times = {b["time_sec"] for b in existing}
            state_bms = (meta.get("hud_capture_state") or {}).get("bookmarks", {})
            for ms in drive_bm:
                t = round(ms / 1000, 1)
                if t not in existing_times:
                    bm = {"time_sec": t, "label": "Drive bookmark"}
                    # Exact tap wall-time from the screenshot worker's qlog scan —
                    # used by the frontend to match the capture PNG by name.
                    epoch = state_bms.get(str(ms), {}).get("epoch")
                    if epoch is not None:
                        bm["epoch"] = epoch
                    existing.append(bm)
            existing.sort(key=lambda b: b["time_sec"])
            meta["bookmarks"] = existing
        meta["drive_bookmarks_imported"] = True
        store._rebuild_routes()
        store._save_metadata()
        route = store.get_route(route_name) or route

    r_with_url = _set_route_url(route, request)
    cleaned = _clean_route(r_with_url)
    cleaned["is_preserved"] = store.is_preserved(local_id)
    cleaned["events_cached"] = events_cached
    cleaned["enriched"] = meta.get("enriched", False)
    return web.json_response(cleaned)


async def handle_route_enrich(request: web.Request) -> web.Response:
    """POST /v1/route/{routeName}/enrich — enrich or re-enrich a route.

    Clears cached events.json/coords.json so they regenerate with latest
    parser code. Re-runs metadata enrichment from rlog.

    Note: engagement % and geocoding happen after the frontend fetches
    events/coords (which regenerates the derived files from rlogs).
    """
    store = request.app["store"]
    local_id = _resolve_local_id(store, request)

    loop = asyncio.get_event_loop()
    cleared = await loop.run_in_executor(None, store.clear_derived, local_id)
    await loop.run_in_executor(None, store.ensure_enriched, local_id)

    route_name, route, store = get_route_or_404(request)
    meta = store._metadata.get(local_id, {})

    r_with_url = _set_route_url(route, request)
    cleaned = _clean_route(r_with_url)
    cleaned["is_preserved"] = store.is_preserved(local_id)
    cleaned["enriched"] = meta.get("enriched", False)
    cleaned["cleared_files"] = cleared
    return web.json_response(cleaned)


async def handle_route_scan(request: web.Request) -> web.Response:
    """POST /v1/route/{routeName}/scan — scan a single pending route from qlog.

    Called by the frontend for each pending card spinner. Parses qlog for
    metadata + geocodes start address. Returns the enriched route object.
    """
    store = request.app["store"]
    local_id = request.match_info["routeName"]

    loop = asyncio.get_event_loop()
    route = await loop.run_in_executor(None, store.enrich_single_new, local_id)

    if not route:
        raise web.HTTPNotFound(text=json.dumps({"error": "Route not found or not scannable"}))

    r_with_url = _set_route_url(route, request)
    cleaned = _clean_route(r_with_url)
    cleaned["route_counter"] = _route_counter(local_id)
    cleaned["is_preserved"] = store.is_preserved(local_id)
    return web.json_response(cleaned)


async def handle_route_files(request: web.Request) -> web.Response:
    """GET /v1/route/{routeName}/files — list available files per segment"""
    route_name, route, store = get_route_or_404(request)

    base = _base_url(request)
    dongle_id = route["dongle_id"]
    route_date = route["fullname"].split("/")[-1]
    local_id = route["_local_id"]

    # Build file lists — one entry per segment index (0 to maxqlog)
    max_seg = route["maxqlog"]
    cameras = []
    dcameras = []
    ecameras = []
    logs = []
    qcameras = []
    qlogs = []

    seg_set = {s["number"]: s for s in route["_segments"]}

    for i in range(max_seg + 1):
        seg = seg_set.get(i)
        if not seg:
            cameras.append("")
            dcameras.append("")
            ecameras.append("")
            logs.append("")
            qcameras.append("")
            qlogs.append("")
            continue

        files = seg["files"]
        prefix = f"{base}/connectdata/{dongle_id}/{route_date}/{i}"

        cameras.append(f"{prefix}/fcamera.hevc" if "fcamera.hevc" in files else "")
        ecameras.append(f"{prefix}/ecamera.hevc" if "ecamera.hevc" in files else "")
        dcameras.append(f"{prefix}/dcamera.hevc" if "dcamera.hevc" in files else "")
        logs.append(f"{prefix}/rlog.zst" if "rlog.zst" in files else "")
        qcameras.append(f"{prefix}/qcamera.ts" if "qcamera.ts" in files else "")
        qlogs.append(f"{prefix}/qlog.zst" if "qlog.zst" in files else "")

    return web.json_response({
        "cameras": cameras,
        "dcameras": dcameras,
        "ecameras": ecameras,
        "logs": logs,
        "qcameras": qcameras,
        "qlogs": qlogs,
    })


async def handle_route_manifest(request: web.Request) -> web.Response:
    """GET /v1/route/{routeName}/manifest.m3u8 — redirect to proper HLS manifest.

    Backward compatibility: redirects to the new qcamera.m3u8 endpoint which
    generates proper ~4s HLS segments for smooth playback.
    """
    route_name = request.match_info["routeName"]
    raise web.HTTPFound(f"/v1/route/{route_name}/qcamera.m3u8")


async def handle_share_signature(request: web.Request) -> web.Response:
    """GET /v1/route/{routeName}/share_signature — dummy signature"""
    return web.json_response({
        "exp": "9999999999",
        "sig": "local",
    })


# ─── Storage management handlers ─────────────────────────────────────

async def handle_route_delete(request: web.Request) -> web.Response:
    """DELETE /v1/route/{routeName}/ — soft-delete (hide) a route"""
    store = request.app["store"]
    local_id = _resolve_local_id(store, request)
    store.hide_route(local_id)
    logger.info("Route hidden: %s", local_id)
    return web.json_response({"success": 1})


async def handle_route_note(request: web.Request) -> web.Response:
    """POST /v1/route/{routeName}/note — set or update a route note"""
    store = request.app["store"]
    local_id = _resolve_local_id(store, request)
    body = await request.json()
    note = body.get("note", "")
    store.set_note(local_id, note)
    return web.json_response({"status": "ok"})


async def handle_route_drive_stats(request: web.Request) -> web.Response:
    """POST /v1/route/{routeName}/drive_stats — store brief per-drive stats
    (distance_m, duration_s, engaged_m, engaged_s) computed on-device at offroad.
    """
    store = request.app["store"]
    local_id = _resolve_local_id(store, request)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)
    required = ("distance_m", "duration_s", "engaged_m", "engaged_s")
    if not all(k in body for k in required):
        return web.json_response({"error": f"missing fields; require {required}"}, status=400)
    try:
        stats = {k: float(body[k]) for k in required}
    except (TypeError, ValueError):
        return web.json_response({"error": "fields must be numbers"}, status=400)
    gps_time = body.get("gps_time")
    if gps_time is not None:
        try:
            gps_time = float(gps_time)
        except (TypeError, ValueError):
            return web.json_response({"error": "gps_time must be a number"}, status=400)
    store.set_drive_stats(local_id, stats, gps_time=gps_time)
    return web.json_response({"status": "ok"})


async def handle_route_bookmark_add(request: web.Request) -> web.Response:
    """POST /v1/route/{routeName}/bookmark — add a bookmark"""
    store = request.app["store"]
    local_id = _resolve_local_id(store, request)
    body = await request.json()
    time_sec = body.get("time_sec", 0)
    label = body.get("label", "").strip()
    if not label:
        raise web.HTTPBadRequest(text=json.dumps({"error": "label is required"}))
    bookmarks = store.add_bookmark(local_id, float(time_sec), label)
    return web.json_response({"bookmarks": bookmarks})


async def handle_route_bookmark_update(request: web.Request) -> web.Response:
    """PUT /v1/route/{routeName}/bookmark/{index} — update a bookmark label"""
    store = request.app["store"]
    local_id = _resolve_local_id(store, request)
    index = int(request.match_info["index"])
    body = await request.json()
    label = body.get("label", "").strip()
    if not label:
        raise web.HTTPBadRequest(text=json.dumps({"error": "label is required"}))
    bookmarks = store.update_bookmark(local_id, index, label)
    return web.json_response({"bookmarks": bookmarks})


async def handle_route_bookmark_delete(request: web.Request) -> web.Response:
    """DELETE /v1/route/{routeName}/bookmark/{index} — delete a bookmark"""
    store = request.app["store"]
    local_id = _resolve_local_id(store, request)
    index = int(request.match_info["index"])
    bookmarks = store.delete_bookmark(local_id, index)
    return web.json_response({"bookmarks": bookmarks})


async def handle_route_preserve(request: web.Request) -> web.Response:
    """POST /v1/route/{routeName}/preserve — mark route as preserved"""
    store = request.app["store"]
    local_id = _resolve_local_id(store, request)
    store.preserve_route(local_id)
    logger.info("Route preserved: %s", local_id)
    return web.json_response({"success": 1})


async def handle_route_unpreserve(request: web.Request) -> web.Response:
    """DELETE /v1/route/{routeName}/preserve — remove preservation"""
    store = request.app["store"]
    local_id = _resolve_local_id(store, request)
    store.unpreserve_route(local_id)
    logger.info("Route unpreserved: %s", local_id)
    return web.json_response({"success": 1})


async def handle_route_download(request: web.Request) -> web.Response:
    """GET /v1/route/{routeName}/download?files=rlog,qcamera&segments=0,1,2 — stream tar.gz"""
    store = request.app["store"]
    local_id = _resolve_local_id(store, request)

    files_param = request.query.get("files", "rlog")
    file_types = [f.strip() for f in files_param.split(",") if f.strip() in DOWNLOAD_FILES]
    if not file_types:
        raise web.HTTPBadRequest(text=json.dumps({"error": f"No valid file types. Choose from: {', '.join(DOWNLOAD_FILES)}"}))

    # Optional segment filter: ?segments=0,1,2 (default: all)
    seg_param = request.query.get("segments")
    segments = None
    if seg_param:
        try:
            segments = [int(s.strip()) for s in seg_param.split(",")]
        except ValueError:
            raise web.HTTPBadRequest(text=json.dumps({"error": "Invalid segments parameter"}))

    loop = asyncio.get_event_loop()
    buf = await loop.run_in_executor(None, build_download_tar, store, local_id, file_types, segments)

    if not buf:
        raise web.HTTPNotFound(text=json.dumps({"error": "No matching files found"}))

    safe_name = local_id.replace("/", "_")
    return web.Response(
        body=buf.read(),
        content_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}.tar.gz"'},
    )


async def handle_connectdata(request: web.Request) -> web.Response:
    """GET /connectdata/{dongleId}/{routeDate}/{segment}/{filename}
    Serves actual media files from /data/media/0/realdata/."""
    path_parts = request.match_info["path"].split("/")

    # Expected: dongleId/routeDate/segment/filename
    if len(path_parts) < 4:
        raise web.HTTPNotFound()

    dongle_id = path_parts[0]
    route_date = path_parts[1]
    segment = path_parts[2]
    filename = path_parts[3]

    # Security: only allow known filenames
    allowed = {"qcamera.ts", "fcamera.hevc", "ecamera.hevc", "dcamera.hevc",
               "rlog.zst", "rlog", "qlog.zst", "qlog", "sprite.jpg"}
    derived = {"coords.json", "events.json"}
    if filename not in allowed and filename not in derived:
        raise web.HTTPForbidden(text="File not allowed")

    fullname = f"{dongle_id}/{route_date}"
    store = request.app["store"]
    seg_int = int(segment)

    # Derived files: generate from qlog (preferred, ~400KB) or rlog (fallback, ~8MB)
    if filename in derived:
        local_id = store.get_local_id(fullname)
        if not local_id:
            raise web.HTTPNotFound()

        cache_path = store.data_dir / f"{local_id}--{seg_int}" / filename
        if cache_path.exists():
            try:
                return web.json_response(json.loads(cache_path.read_text()))
            except Exception:
                pass

        # Prefer qlog (tiny, has all state/GPS messages) over rlog (huge)
        log_file = (store.resolve_segment_path(fullname, seg_int, "qlog.zst")
                    or store.resolve_segment_path(fullname, seg_int, "qlog")
                    or store.resolve_segment_path(fullname, seg_int, "rlog.zst")
                    or store.resolve_segment_path(fullname, seg_int, "rlog"))
        if not log_file:
            return web.json_response([])

        gen_fn = _generate_coords_json if filename == "coords.json" else _generate_events_json
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(store._executor, gen_fn, str(log_file), seg_int)

        try:
            cache_path.write_text(json.dumps(data))
        except Exception:
            pass

        return web.json_response(data)

    file_path = store.resolve_segment_path(fullname, seg_int, filename)

    # Generate sprite on demand from qcamera.ts
    # ?t=N extracts frame at N seconds into the segment (default: 5s)
    # Cached as sprite.jpg (default) or sprite_{t}.jpg (custom time)
    if filename == "sprite.jpg":
        seek_t = request.query.get("t", "5")
        try:
            seek_val = max(0, min(59, int(seek_t)))
        except ValueError:
            seek_val = 5
        cache_name = "sprite.jpg" if seek_val == 5 else f"sprite_{seek_val}.jpg"
        # For non-default time, check the specific cache file instead
        if seek_val != 5 or not file_path:
            qcam = store.resolve_segment_path(fullname, seg_int, "qcamera.ts")
            if qcam:
                sprite_path = qcam.parent / cache_name
                if sprite_path.exists() and sprite_path.stat().st_size > 1000:
                    file_path = sprite_path
                else:
                    loop = asyncio.get_event_loop()
                    seeks = [str(seek_val)] if seek_val > 0 else ["0"]
                    if seek_val > 1:
                        seeks.append("1")
                    seeks.append("0")
                    for s in seeks:
                        try:
                            proc = await loop.run_in_executor(
                                None,
                                lambda s=s: subprocess.run(
                                    ["ffmpeg", "-y", "-ss", s, "-i", str(qcam),
                                     "-vframes", "1", "-q:v", "5", "-vf", "scale=480:-1",
                                     "-f", "image2", str(sprite_path)],
                                    capture_output=True, timeout=10,
                                ),
                            )
                            if proc.returncode == 0 and sprite_path.exists() and sprite_path.stat().st_size > 1000:
                                file_path = sprite_path
                                break
                        except Exception:
                            pass

    if not file_path:
        raise web.HTTPNotFound()

    content_types = {
        ".ts": "video/mp2t",
        ".hevc": "video/hevc",
        ".zst": "application/zstd",
        ".jpg": "image/jpeg",
    }
    ct = content_types.get(file_path.suffix, "application/octet-stream")
    return web.FileResponse(file_path, headers={"Content-Type": ct})
