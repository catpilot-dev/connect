"""Shared route utility functions for Connect on Device handlers.

Small helper functions used across multiple handler modules.
No internal imports — only uses stdlib + aiohttp.
"""

import json
from bisect import bisect_left
from pathlib import Path

from aiohttp import web


def _clean_route(route: dict) -> dict:
    """Remove internal fields from route for API response."""
    return {k: v for k, v in route.items() if not k.startswith("_")}


def _base_url(request: web.Request) -> str:
    """Get base URL for constructing file URLs."""
    scheme = request.headers.get("X-Forwarded-Proto", request.scheme)
    host = request.headers.get("X-Forwarded-Host", request.host)
    return f"{scheme}://{host}"


def _set_route_url(route: dict, request: web.Request) -> dict:
    """Set the url field dynamically based on request host."""
    r = dict(route)
    base = _base_url(request)
    r["url"] = f"{base}/connectdata/{route['dongle_id']}/{route['fullname'].split('/')[-1]}"
    return r


def _resolve_local_id(store, request: web.Request) -> str:
    """Resolve routeName from URL to local_id. Raises 404 if not found."""
    route_name = request.match_info["routeName"].replace("|", "/")
    # Try direct lookup first
    local_id = store.get_local_id(route_name)
    if not local_id:
        # Try matching by local_id directly (for hidden routes not in _routes)
        if route_name in store._raw:
            local_id = route_name
    if not local_id:
        raise web.HTTPNotFound(text=json.dumps({"error": f"Route {route_name} not found"}))
    return local_id


def _route_engaged_distance(store, route: dict) -> tuple[float, float]:
    """Compute engaged_m and total_m for a route by integrating GPS distance
    across engagement intervals.

    Walks each segment's coords.json + events.json once, building route-wide
    (t_ms, cum_m) arrays and collecting raw state events. Sums the cumulative-
    distance delta across each engagement on/off pair from those events; open
    engagements close at the last coord's t_ms.

    Returns (0.0, 0.0) if coords.json or events.json is missing or unreadable
    for any segment, or if fewer than two GPS samples exist.
    """
    segments = sorted(route.get("_segments", []), key=lambda s: s["number"])
    if not segments:
        return (0.0, 0.0)

    t_ms: list[float] = []
    cum_m: list[float] = []
    seg_offset = 0.0
    all_events: list[list[dict]] = []

    for seg in segments:
        seg_dir = Path(seg["path"])
        coords_path = seg_dir / "coords.json"
        events_path = seg_dir / "events.json"
        if not coords_path.exists() or not events_path.exists():
            return (0.0, 0.0)
        try:
            coords = json.loads(coords_path.read_text())
            events = json.loads(events_path.read_text())
        except Exception:
            return (0.0, 0.0)

        if coords:
            seg_last = 0.0
            for c in coords:
                t_ms.append(float(c["t"]) * 1000.0)
                cum_m.append(seg_offset + float(c["dist"]))
                seg_last = float(c["dist"])
            seg_offset += seg_last

        all_events.append(events)

    if len(t_ms) < 2:
        return (0.0, 0.0)

    intervals: list[tuple[float, float]] = []
    open_on: float | None = None
    for events in all_events:
        for ev in events:
            if ev.get("type") != "state":
                continue
            enabled = ev.get("data", {}).get("enabled", False)
            offset = float(ev.get("route_offset_millis", 0))
            if enabled and open_on is None:
                open_on = offset
            elif not enabled and open_on is not None:
                intervals.append((open_on, offset))
                open_on = None
    if open_on is not None:
        intervals.append((open_on, t_ms[-1]))

    def _interp(target_ms: float) -> float:
        if target_ms <= t_ms[0]:
            return cum_m[0]
        if target_ms >= t_ms[-1]:
            return cum_m[-1]
        i = bisect_left(t_ms, target_ms)
        t0, t1 = t_ms[i - 1], t_ms[i]
        d0, d1 = cum_m[i - 1], cum_m[i]
        if t1 == t0:
            return d0
        return d0 + (d1 - d0) * (target_ms - t0) / (t1 - t0)

    engaged_m = 0.0
    for on_ms, off_ms in intervals:
        engaged_m += _interp(off_ms) - _interp(on_ms)

    return (engaged_m, cum_m[-1])


def _route_bookmarks(route: dict) -> list[int]:
    """Collect user_flag timestamps from cached events.json files.

    Returns sorted list of route_offset_millis for bookmark events.
    Only reads already-cached events.json (no rlog parsing).
    """
    bookmarks = []
    for seg in route.get("_segments", []):
        events_path = Path(seg["path"]) / "events.json"
        if not events_path.exists():
            continue
        try:
            for ev in json.loads(events_path.read_text()):
                if ev.get("type") == "user_flag":
                    bookmarks.append(ev["route_offset_millis"])
        except Exception:
            pass
    return sorted(bookmarks)


_OVERRIDING_STATES = {"overriding", "preEnabled"}


def _route_timeline_summary(route: dict) -> list[dict] | None:
    """Build compact timeline spans from cached events.json files.

    Mirrors the buildTimelineEvents() state machine in derived.js.
    Returns list of timeline span dicts ready for EventTimeline component,
    or None if no cached events exist (route not yet enriched).

    Each span: {type, route_offset_millis, end_route_offset_millis, [alertStatus]}
    """
    all_events = []
    has_any = False
    for seg in route.get("_segments", []):
        events_path = Path(seg["path"]) / "events.json"
        if not events_path.exists():
            continue
        try:
            all_events.extend(json.loads(events_path.read_text()))
            has_any = True
        except Exception:
            pass

    if not has_any:
        return None

    all_events.sort(key=lambda e: e.get("route_offset_millis", 0))

    result = []
    last_engaged = None
    last_alert = None
    last_override = None

    for ev in all_events:
        if ev.get("type") == "user_flag":
            result.append({"type": "user_flag", "route_offset_millis": ev["route_offset_millis"]})
            continue

        if ev.get("type") != "state":
            continue

        data = ev.get("data", {})
        enabled = data.get("enabled", False)
        alert_status = data.get("alertStatus", 0)
        state = data.get("state", "")
        offset = ev.get("route_offset_millis", 0)

        # Engaged spans
        if last_engaged and not enabled:
            result.append({
                "type": "engaged",
                "route_offset_millis": last_engaged["route_offset_millis"],
                "end_route_offset_millis": offset,
            })
            last_engaged = None
        if not last_engaged and enabled:
            last_engaged = ev

        # Alert spans
        if last_alert and last_alert.get("data", {}).get("alertStatus") != alert_status:
            result.append({
                "type": "alert",
                "route_offset_millis": last_alert["route_offset_millis"],
                "end_route_offset_millis": offset,
                "alertStatus": last_alert["data"]["alertStatus"],
            })
            last_alert = None
        if not last_alert and alert_status != 0:
            last_alert = ev

        # Override spans
        if last_override and state not in _OVERRIDING_STATES:
            result.append({
                "type": "overriding",
                "route_offset_millis": last_override["route_offset_millis"],
                "end_route_offset_millis": offset,
            })
            last_override = None
        if not last_override and state in _OVERRIDING_STATES:
            last_override = ev

    # Close trailing spans at route end
    end_ms = (route.get("maxqlog", 0) + 1) * 60_000
    if last_engaged:
        result.append({"type": "engaged", "route_offset_millis": last_engaged["route_offset_millis"], "end_route_offset_millis": end_ms})
    if last_alert:
        result.append({"type": "alert", "route_offset_millis": last_alert["route_offset_millis"], "end_route_offset_millis": end_ms, "alertStatus": last_alert["data"]["alertStatus"]})
    if last_override:
        result.append({"type": "overriding", "route_offset_millis": last_override["route_offset_millis"], "end_route_offset_millis": end_ms})

    return result
