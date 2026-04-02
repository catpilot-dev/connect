"""Route storage and metadata management for Connect on Device.

Contains the RouteStore class that scans route directories, manages metadata,
and provides comma-compatible route objects. Lazy enrichment: new routes are
only parsed when the list page is visited, not during background scans.
"""

import json
import logging
import re
import sys
import time
import urllib.request
import urllib.error
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from log_parser import _find_first_gps, _find_first_gps_time, _find_last_gps, _parse_log_metadata

logger = logging.getLogger("connect")

# Route directory pattern: {count}--{uid}--{segment}
ROUTE_DIR_RE = re.compile(r"^(\w+--\w+)--(\d+)$")

from config import OPENPILOT_DIR, PARAMS_DIR, REALDATA_DIR

DEFAULT_DATA_DIR = REALDATA_DIR
DEFAULT_PORT = 8082
CACHE_TTL = 120  # seconds — route scan is expensive with metadata
METADATA_FILE = ".route_metadata.json"

# Nominatim reverse geocoding (OSM)
_NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
_NOMINATIM_HEADERS = {"User-Agent": "connect-on-device/1.0"}
_last_geocode_time = 0.0


def _reverse_geocode(lat: float, lng: float) -> tuple[str | None, bool]:
    """Reverse geocode lat/lng to a short road/place name via Nominatim.

    Returns (name, is_road) where is_road is True if the result is a
    road/neighbourhood-level name (not just a city).
    Respects Nominatim rate limit (1 req/sec).
    """
    global _last_geocode_time
    # Rate limit: 1 request per second
    elapsed = time.time() - _last_geocode_time
    if elapsed < 1.1:
        time.sleep(1.1 - elapsed)

    url = f"{_NOMINATIM_URL}?lat={lat}&lon={lng}&format=json&zoom=16&accept-language=zh,en"
    req = urllib.request.Request(url, headers=_NOMINATIM_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            _last_geocode_time = time.time()
            data = json.loads(resp.read())
            addr = data.get("address", {})
            # Road-level names (specific enough for a route address)
            road = (addr.get("road")
                    or addr.get("neighbourhood")
                    or addr.get("suburb"))
            if road:
                return road, True
            # Vague fallbacks (town/city level — only used as last resort)
            vague = (addr.get("town")
                     or addr.get("city")
                     or addr.get("locality")
                     or addr.get("village"))
            return vague, False
    except Exception as e:
        _last_geocode_time = time.time()
        logger.debug("Reverse geocode failed for %.5f,%.5f: %s", lat, lng, e)
        return None, False


def _route_counter(local_id: str) -> int:
    """Extract monotonic counter from route local_id (e.g. '0000011d--abc' -> 285).

    Openpilot uses hex counters (0-9a-f), so parse as base 16.
    The counter is always monotonically increasing per-device, making it a
    reliable proxy for route recency — unlike directory mtime which may
    reflect the AGNOS build date instead of actual drive time.
    """
    try:
        return int(local_id.split("--")[0], 16)
    except (ValueError, IndexError):
        return 0

# Add openpilot to path for LogReader
if OPENPILOT_DIR not in sys.path:
    sys.path.insert(0, OPENPILOT_DIR)


class RouteStore:
    """Scans route directories and builds comma-compatible route objects.

    Uses metadata.json (compatible with route_metadata.py) for persistent
    route metadata. Two-phase approach for fast startup:
    1. Fast scan: directory listing only (instant) — uses cached metadata or route counter
    2. On-demand enrichment: parse qlog/rlog for uncached routes (user-triggered)

    metadata.json format (shared with route_metadata.py):
    {
      "version": "1.0",
      "last_updated": "ISO 8601",
      "routes": {
        "00000100--c9f14ae705": {
          "route_id": "...",
          "creation_time": "ISO 8601",       # from wallTimeNanos
          "gps_coordinates": [lat, lng],
          "dongle_id": "...",
          "git_commit": "...",
          "git_branch": "...",
          "openpilot_version": "...",
          ...
        }
      }
    }
    """

    def __init__(self, data_dir: str):
        self.data_dir = Path(data_dir)
        self._routes: dict = {}           # fullname -> route dict
        self._fullname_map: dict = {}     # fullname -> local_id
        self._local_id_map: dict = {}     # local_id -> fullname
        self._raw: dict = {}              # local_id -> raw scan data
        self._dongle_id: str = ""
        self._last_scan: float = 0
        self._metadata: dict = {}         # route_id -> route_metadata.py format
        self._hidden: dict = {}            # local_id -> hide_time (Unix epoch)
        self._preserved: set = set()      # local_ids protected from cleanup
        # Dot-prefixed filename so uploader.py clear_locks() skips it
        self._metadata_path = Path(data_dir) / METADATA_FILE
        self._agnos_version: str | None = None
        self._executor = ThreadPoolExecutor(max_workers=1)
        self._bg_scanning = False

        self._load_metadata()
        self._detect_dongle_id()
        self._detect_agnos_version()

    def _detect_dongle_id(self):
        """Read dongle_id from params or metadata."""
        for p in [f"{PARAMS_DIR}/DongleId", "/home/oxygen/driving_data/data/DongleId"]:
            try:
                self._dongle_id = Path(p).read_text().strip()
                return
            except Exception:
                pass
        for meta in self._metadata.values():
            did = meta.get("dongle_id")
            if did and did != "Unknown":
                self._dongle_id = did
                return
        self._dongle_id = "local"

    def _detect_agnos_version(self):
        """Read AGNOS version from /VERSION."""
        try:
            self._agnos_version = Path("/VERSION").read_text().strip()
        except Exception:
            pass

    def _load_metadata(self):
        """Load metadata.json (route_metadata.py format) including hidden/preserved sets."""
        if self._metadata_path.exists():
            try:
                data = json.loads(self._metadata_path.read_text())
                self._metadata = data.get("routes", {})
                # hidden_routes: dict (new) or list (old format, backward compat)
                raw_hidden = data.get("hidden_routes", {})
                if isinstance(raw_hidden, list):
                    # Old format: convert list to dict with current time as fallback
                    self._hidden = {lid: time.time() for lid in raw_hidden}
                elif isinstance(raw_hidden, dict):
                    self._hidden = raw_hidden
                else:
                    self._hidden = {}
                self._preserved = set(data.get("preserved_routes", []))
                logger.info("Loaded metadata.json: %d routes, %d hidden, %d preserved",
                            len(self._metadata), len(self._hidden), len(self._preserved))
            except Exception as e:
                logger.warning("Failed to load metadata.json: %s", e)
                self._metadata = {}

    def _save_metadata(self):
        """Save metadata.json in route_metadata.py-compatible format."""
        data = {
            "version": "1.0",
            "last_updated": datetime.now(tz=timezone.utc).isoformat(),
            "hidden_routes": dict(sorted(self._hidden.items())),
            "preserved_routes": sorted(self._preserved),
            "routes": self._metadata,
        }
        try:
            tmp = self._metadata_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2))
            tmp.rename(self._metadata_path)
        except Exception as e:
            logger.debug("Failed to save metadata.json: %s", e)

    def _wall_time_to_route_date(self, wall_time_nanos: int, lng: float | None = None) -> str:
        """Convert wallTimeNanos to comma route date format: YYYY-MM-DD--HH-MM-SS in local time."""
        dt = datetime.fromtimestamp(wall_time_nanos / 1e9, tz=timezone.utc)
        if lng is not None:
            offset_hours = round(lng / 15)
            dt = dt.astimezone(timezone(timedelta(hours=offset_hours)))
        return dt.strftime("%Y-%m-%d--%H-%M-%S")

    def _meta_to_internal(self, route_id: str) -> dict:
        """Convert route_metadata.py format to internal fields for route building."""
        meta = self._metadata.get(route_id, {})
        result = {}

        # dongle_id
        did = meta.get("dongle_id")
        if did and did != "Unknown":
            result["dongle_id"] = did

        # GPS time → wall_time_nanos (used for route_date in fullname)
        # Prefer GPS time (accurate), fall back to initData wallTimeNanos
        # (may be AGNOS build date on old firmware, but corrected by background enrichment)
        gps_t = meta.get("gps_time")
        if gps_t:
            result["wall_time_nanos"] = int(gps_t * 1e9)
            result["create_time"] = gps_t
        else:
            ct = meta.get("creation_time")
            if ct and isinstance(ct, str) and not ct.startswith("GPS"):
                try:
                    dt = datetime.fromisoformat(ct)
                    ts = dt.timestamp()
                    result["wall_time_nanos"] = int(ts * 1e9)
                    result["create_time"] = ts
                except (ValueError, OSError):
                    pass

        # GPS
        gps = meta.get("gps_coordinates")
        if gps and isinstance(gps, list) and len(gps) == 2:
            result["start_lat"] = gps[0]
            result["start_lng"] = gps[1]

        # Git info
        gc = meta.get("git_commit")
        if gc and gc != "Unknown":
            result["git_commit"] = gc
        gb = meta.get("git_branch")
        if gb:
            result["git_branch"] = gb
        elif meta.get("software_environment", {}).get("openpilot_branch"):
            result["git_branch"] = meta["software_environment"]["openpilot_branch"]

        # Version
        ver = meta.get("openpilot_version")
        if ver and ver != "Unknown":
            result["version"] = ver

        # Car fingerprint
        cf = meta.get("car_fingerprint")
        if cf:
            result["car_fingerprint"] = cf

        # Git remote
        gr = meta.get("git_remote")
        if gr:
            result["git_remote"] = gr

        # Device type
        dt = meta.get("device_type")
        if dt:
            result["device_type"] = dt

        # Engagement
        ep = meta.get("engagement_pct")
        if ep is not None:
            result["engagement_pct"] = ep

        # Addresses (reverse geocoded)
        sa = meta.get("start_address")
        if sa:
            result["start_address"] = sa
        ea = meta.get("end_address")
        if ea:
            result["end_address"] = ea

        # Notes
        notes = meta.get("notes")
        if notes:
            result["notes"] = notes

        # Bookmarks
        bookmarks = meta.get("bookmarks")
        if bookmarks:
            result["bookmarks"] = bookmarks

        return result

    def _needs_enrich(self, lid: str) -> bool:
        """Check if a route needs background enrichment."""
        meta = self._metadata.get(lid)
        if not meta:
            return True
        # Re-enrich routes missing gps_time (enriched before GPS time extraction)
        if meta.get("enriched") and meta.get("gps_time") is None:
            meta.pop("enriched", None)
            return True
        # Fully enriched — done (multi-segment GPS fallback + geocoding complete)
        if meta.get("enriched"):
            return False
        # Quick-enriched or partially-enriched — needs full enrichment
        return True

    @staticmethod
    def _find_rlog(seg_path: str) -> str | None:
        """Find rlog file in a segment directory, preferring .zst."""
        rlog = Path(seg_path) / "rlog.zst"
        if rlog.exists():
            return str(rlog)
        rlog = Path(seg_path) / "rlog"
        if rlog.exists():
            return str(rlog)
        return None

    @staticmethod
    def _find_qlog(seg_path: str) -> str | None:
        """Find qlog file in a segment directory, preferring .zst.

        qlog is much smaller than rlog (~2-5MB vs 50-200MB) and contains
        initData, carParams, and gpsLocationExternal — sufficient for
        quick metadata extraction without full rlog decompression.
        """
        qlog = Path(seg_path) / "qlog.zst"
        if qlog.exists():
            return str(qlog)
        qlog = Path(seg_path) / "qlog"
        if qlog.exists():
            return str(qlog)
        return None

    def _calc_route_distance(self, local_id: str, segments: list) -> float | None:
        """Calculate total route distance in miles.

        Tier 1: Sum cached coords.json last-point dist (accurate, from visited routes)
        Tier 2: Use total_distance_m from metadata (computed during enrichment)
        """
        # Tier 1: cached coords.json
        total_m = 0.0
        found = 0
        for seg in segments:
            coords_path = Path(seg["path"]) / "coords.json"
            if not coords_path.exists():
                continue
            try:
                coords = json.loads(coords_path.read_text())
                if coords:
                    total_m += coords[-1].get("dist", 0)
                    found += 1
            except Exception:
                pass
        if found > 0:
            return round(total_m / 1609.344, 1)

        # Tier 2: enrichment-computed total distance
        meta = self._metadata.get(local_id, {})
        total_dist = meta.get("total_distance_m")
        if total_dist and total_dist > 0:
            return round(total_dist / 1609.344, 1)

        return None

    def _build_route(self, local_id: str, info: dict, internal: dict) -> dict:
        """Build a comma-compatible route dict from raw scan data + metadata."""
        dongle_id = internal.get("dongle_id", self._dongle_id)

        lng = internal.get("start_lng")
        wtn = internal.get("wall_time_nanos", 0)
        if wtn:
            route_date = self._wall_time_to_route_date(wtn, lng)
        else:
            # No wallTimeNanos yet (unenriched) — use local_id as route_date
            # placeholder. Avoids using inaccurate directory mtime.
            route_date = local_id

        fullname = f"{dongle_id}/{route_date}"
        # Use route counter for ordering when enriched create_time unavailable.
        # Counter is monotonically increasing and always reliable.
        create_time = internal.get("create_time", _route_counter(local_id))

        seg_numbers = [s["number"] for s in info["segments"]]
        max_seg = max(seg_numbers) if seg_numbers else 0

        seg_start_times = []
        seg_end_times = []
        for s in info["segments"]:
            t = create_time + s["number"] * 60
            seg_start_times.append(t)
            seg_end_times.append(t + 60)

        tz_info = timezone(timedelta(hours=round(lng / 15))) if lng is not None else timezone.utc
        start_time_iso = datetime.fromtimestamp(create_time, tz=tz_info).isoformat()
        end_time_epoch = create_time + (max_seg + 1) * 60
        end_time_iso = datetime.fromtimestamp(end_time_epoch, tz=tz_info).isoformat()

        return {
            "create_time": create_time,
            "dongle_id": dongle_id,
            "end_lat": internal.get("start_lat"),
            "end_lng": internal.get("start_lng"),
            "end_time": end_time_iso,
            "fullname": fullname,
            "git_branch": internal.get("git_branch"),
            "git_commit": internal.get("git_commit"),
            "git_commit_date": None,
            "git_dirty": None,
            "git_remote": internal.get("git_remote"),
            "is_public": True,
            "distance": self._calc_route_distance(local_id, info["segments"]),
            "maxqlog": max_seg,
            "engagement_pct": internal.get("engagement_pct"),
            "platform": internal.get("car_fingerprint"),
            "procqlog": len(seg_numbers),
            "start_lat": internal.get("start_lat"),
            "start_lng": internal.get("start_lng"),
            "start_time": start_time_iso,
            "url": None,
            "user_id": "local",
            "version": internal.get("version"),
            "vin": None,
            "make": None,
            "id": None,
            "car_id": None,
            "version_id": None,
            "device_type": internal.get("device_type"),
            "agnos_version": self._agnos_version,
            "start_address": internal.get("start_address"),
            "end_address": internal.get("end_address"),
            "notes": internal.get("notes"),
            "bookmarks": internal.get("bookmarks"),
            "local_id": local_id,
            "_local_id": local_id,
            "_segments": info["segments"],
            "_seg_numbers": seg_numbers,
            "_seg_start_times": seg_start_times,
            "_seg_end_times": seg_end_times,
        }

    def _rebuild_routes(self):
        """Rebuild route dicts from raw data + current metadata. Skips hidden routes."""
        routes = {}
        fullname_map = {}
        local_id_map = {}

        for local_id, info in self._raw.items():
            if local_id in self._hidden:
                continue
            internal = self._meta_to_internal(local_id)
            dongle_id = internal.get("dongle_id", self._dongle_id)
            if not self._dongle_id or self._dongle_id == "local":
                self._dongle_id = dongle_id

            route = self._build_route(local_id, info, internal)

            # Hide stub routes: < 2 minutes (maxqlog < 1) and no distance
            if route["maxqlog"] < 1 and not route.get("distance"):
                continue

            # Hide routes with no GPS time (would show "Invalid Date" in UI)
            if not internal.get("wall_time_nanos"):
                continue

            fullname = route["fullname"]
            routes[fullname] = route
            fullname_map[fullname] = local_id
            local_id_map[local_id] = fullname

        self._routes = routes
        self._fullname_map = fullname_map
        self._local_id_map = local_id_map

    def _find_log(self, seg_path: str) -> str | None:
        """Find best log file in a segment directory: qlog first, rlog fallback."""
        return self._find_qlog(seg_path) or self._find_rlog(seg_path)

    def _enrich_one(self, local_id: str, segments: list) -> dict | None:
        """Parse log metadata for a single route. Runs in thread pool.

        Prefers qlog (~400KB) over rlog (~8MB). Tries segment 0 first for
        initData + GPS + partial distance, then checks later segments for
        GPS only (cold starts can delay GPS lock).

        Per-segment distance accumulation is intentionally omitted — the UI
        computes accurate distance from coords.json on first route view.
        The partial seg-0 distance serves as a non-zero fallback estimate.
        """
        sorted_segs = sorted(segments, key=lambda s: s["number"])

        # Parse segment 0 for full metadata (initData + GPS + partial distance)
        result = None
        seg0_log = self._find_log(sorted_segs[0]["path"]) if sorted_segs else None
        if seg0_log:
            result = _parse_log_metadata(seg0_log)

        if not result:
            result = {}

        # If no GPS coords or time, try later segments (GPS lock can take 5+ min)
        need_coords = not result.get("start_lat")
        need_time = not result.get("gps_time")
        if need_coords or need_time:
            for seg in sorted_segs[1:]:
                log = self._find_log(seg["path"])
                if not log:
                    continue
                if need_coords:
                    gps = _find_first_gps(log)
                    if gps:
                        result["start_lat"] = gps[0]
                        result["start_lng"] = gps[1]
                        need_coords = False
                if need_time:
                    gps_t = _find_first_gps_time(log)
                    if gps_t:
                        # Adjust back to segment 0 start time
                        result["gps_time"] = gps_t - seg["number"] * 60
                        need_time = False
                if not need_coords and not need_time:
                    break

        # Use seg 0's partial distance as fallback estimate
        seg_dist = result.pop("segment_distance_m", 0.0)
        if seg_dist > 0:
            result["total_distance_m"] = seg_dist

        return result if result else None

    def _log_to_metadata_entry(self, local_id: str, log_meta: dict) -> dict:
        """Convert log parse result to metadata entry."""
        entry = {"route_id": local_id}

        wtn = log_meta.get("wall_time_nanos", 0)
        if wtn:
            dt = datetime.fromtimestamp(wtn / 1e9, tz=timezone.utc)
            entry["creation_time"] = dt.isoformat()
        else:
            entry["creation_time"] = None

        lat = log_meta.get("start_lat")
        lng = log_meta.get("start_lng")
        entry["gps_coordinates"] = [lat, lng] if lat and lng else None

        entry["dongle_id"] = log_meta.get("dongle_id", "Unknown")
        entry["git_commit"] = log_meta.get("git_commit", "Unknown")
        entry["git_branch"] = log_meta.get("git_branch")
        entry["openpilot_version"] = log_meta.get("version", "Unknown")
        entry["total_distance_m"] = log_meta.get("total_distance_m")
        entry["car_fingerprint"] = log_meta.get("car_fingerprint")
        entry["git_remote"] = log_meta.get("git_remote")
        entry["device_type"] = log_meta.get("device_type")
        entry["gps_time"] = log_meta.get("gps_time")
        entry["source"] = "connect_server"
        entry["enriched"] = True

        return entry

    @staticmethod
    def _load_coords(seg_path: str) -> list:
        """Load cached coords.json for a segment, or empty list."""
        coords_path = Path(seg_path) / "coords.json"
        if coords_path.exists():
            try:
                with open(coords_path) as f:
                    data = json.load(f)
                if data and len(data) > 0:
                    return data
            except Exception:
                pass
        return []

    def _geocode_start(self, info: dict) -> str | None:
        """Find the first road-level address for a route.

        Scans early segments' coords.json for GPS positions that resolve
        to an actual road name in Nominatim. Skips vague city-level results.
        """
        sorted_segs = sorted(info["segments"], key=lambda s: s["number"])
        for seg in sorted_segs[:5]:
            coords = self._load_coords(seg["path"])
            if not coords:
                continue
            # Try first coord, then a point ~10s in (more likely on a road)
            candidates = [coords[0]]
            if len(coords) > 100:
                candidates.append(coords[100])  # ~10s at 10Hz
            for c in candidates:
                name, is_road = _reverse_geocode(c["lat"], c["lng"])
                if name and is_road:
                    return name
            # First segment with coords but no road — keep trying next segment
        return None

    def _geocode_end(self, info: dict) -> str | None:
        """Find the last road-level address for a route.

        Scans late segments' coords.json backwards for GPS positions that
        resolve to an actual road name. Skips vague results from parking areas.
        """
        sorted_segs = sorted(info["segments"], key=lambda s: s["number"], reverse=True)
        for seg in sorted_segs[:5]:
            coords = self._load_coords(seg["path"])
            if not coords:
                continue
            # Try last coord, then a point ~10s before end
            candidates = [coords[-1]]
            if len(coords) > 100:
                candidates.append(coords[-100])  # ~10s before end
            for c in candidates:
                name, is_road = _reverse_geocode(c["lat"], c["lng"])
                if name and is_road:
                    return name
            # This segment's GPS doesn't resolve to a road — try earlier segment
        return None

    def geocode_route(self, local_id: str) -> bool:
        """Reverse-geocode start and end locations for a route.

        Scans coords.json for GPS positions that resolve to actual road names,
        skipping vague city-level results from parking lots or unmapped areas.
        Returns True if metadata was updated.
        """
        meta = self._metadata.get(local_id)
        if not meta:
            return False

        # Skip if fully geocoded (both start and end)
        if meta.get("start_address") is not None and meta.get("end_address") is not None:
            return False

        updated = False
        info = self._raw.get(local_id)
        if not info:
            return False

        if meta.get("start_address") is None:
            addr = self._geocode_start(info)
            if addr:
                meta["start_address"] = addr
                updated = True

        if meta.get("end_address") is None:
            addr = self._geocode_end(info)
            if addr:
                meta["end_address"] = addr
                updated = True

        if updated:
            self._rebuild_routes()
            self._save_metadata()
            logger.info("Geocoded route %s: %s → %s",
                        local_id, meta.get("start_address"), meta.get("end_address"))
        return updated

    @staticmethod
    def _is_onroad() -> bool:
        """Check if openpilot is currently driving. Yields CPU to controls when True."""
        try:
            return Path(f"{PARAMS_DIR}/IsOnroad").read_bytes().strip() == b"1"
        except Exception:
            return False

    def get_pending_route_ids(self) -> list[dict]:
        """Return minimal info for routes in _raw not yet in _metadata.

        These are routes on disk that haven't been scanned yet. The list
        endpoint includes them as placeholder cards with spinners.
        Skips hidden routes and single-segment stubs.
        """
        pending = []
        for lid, info in self._raw.items():
            if lid in self._metadata or lid in self._hidden:
                continue
            seg_count = len(info["segments"])
            if seg_count < 2:
                continue  # skip single-segment stubs
            pending.append({
                "local_id": lid,
                "seg_count": seg_count,
                "counter": _route_counter(lid),
            })
        pending.sort(key=lambda x: x["counter"], reverse=True)
        return pending

    def enrich_single_new(self, local_id: str) -> dict | None:
        """Enrich a single new route from qlog. Returns built route dict or None.

        Called by the /scan endpoint when the frontend requests enrichment
        for one pending route at a time (progressive loading).
        """
        if local_id in self._metadata or local_id in self._hidden:
            return None
        info = self._raw.get(local_id)
        if not info:
            return None

        sorted_segs = sorted(info["segments"], key=lambda s: s["number"])

        # Parse seg 0: prefer qlog (tiny) over rlog (huge)
        log_file = None
        for seg in sorted_segs[:1]:
            log_file = self._find_qlog(seg["path"]) or self._find_rlog(seg["path"])
        if not log_file:
            return None

        result = _parse_log_metadata(log_file)
        if not result:
            return None

        # GPS fallback for cold start
        need_coords = not result.get("start_lat")
        need_time = not result.get("gps_time")
        if need_coords or need_time:
            for seg in sorted_segs[1:]:
                log = self._find_qlog(seg["path"]) or self._find_rlog(seg["path"])
                if not log:
                    continue
                if need_coords:
                    gps = _find_first_gps(log)
                    if gps:
                        result["start_lat"] = gps[0]
                        result["start_lng"] = gps[1]
                        need_coords = False
                if need_time:
                    gps_t = _find_first_gps_time(log)
                    if gps_t:
                        result["gps_time"] = gps_t - seg["number"] * 60
                        need_time = False
                if not need_coords and not need_time:
                    break

        # Skip stubs: < 2 segments AND no GPS
        has_gps = result.get("start_lat") is not None
        if len(sorted_segs) < 2 and not has_gps:
            return None

        entry = self._log_to_metadata_entry(local_id, result)
        entry["enriched"] = False
        self._metadata[local_id] = entry

        # Reverse-geocode start address
        lat = result.get("start_lat")
        lng = result.get("start_lng")
        if lat and lng:
            addr, is_road = _reverse_geocode(lat, lng)
            if addr:
                entry["start_address"] = addr

        self._rebuild_routes()
        self._save_metadata()
        logger.info("Scanned route %s", local_id)

        # Return the built route
        fullname = self._local_id_map.get(local_id)
        return self._routes.get(fullname) if fullname else None

    def enrich_new_routes(self) -> int:
        """Enrich routes not yet in metadata — called by list endpoint, not during scan.

        Finds routes in _raw that are NOT in _metadata and NOT in _hidden.
        Parses seg 0 qlog.zst for initData + GPS, tries later segments for GPS
        on cold start. Reverse-geocodes start address. Stores in metadata with
        enriched=False (full enrichment deferred to explicit Enrich button).

        Skips stub routes (< 2 segments AND no GPS) — these won't appear in list.

        Returns count of newly enriched routes.
        """
        new_routes = [
            (lid, info) for lid, info in self._raw.items()
            if lid not in self._metadata and lid not in self._hidden
        ]
        if not new_routes:
            return 0

        # Sort newest first for consistent ordering
        new_routes.sort(key=lambda x: _route_counter(x[0]), reverse=True)

        logger.info("Enriching %d new routes from qlog", len(new_routes))
        enriched = 0

        for local_id, info in new_routes:
            try:
                sorted_segs = sorted(info["segments"], key=lambda s: s["number"])

                # Skip stubs: < 2 segments with no qlog/rlog
                seg_count = len(sorted_segs)

                # Parse seg 0: prefer qlog (tiny) over rlog (huge)
                log_file = None
                for seg in sorted_segs[:1]:
                    log_file = self._find_qlog(seg["path"]) or self._find_rlog(seg["path"])
                if not log_file:
                    continue

                result = _parse_log_metadata(log_file)
                if not result:
                    continue

                # If no GPS in seg 0, try later segments (cold start)
                need_coords = not result.get("start_lat")
                need_time = not result.get("gps_time")
                if need_coords or need_time:
                    for seg in sorted_segs[1:]:
                        log = self._find_qlog(seg["path"]) or self._find_rlog(seg["path"])
                        if not log:
                            continue
                        if need_coords:
                            gps = _find_first_gps(log)
                            if gps:
                                result["start_lat"] = gps[0]
                                result["start_lng"] = gps[1]
                                need_coords = False
                        if need_time:
                            gps_t = _find_first_gps_time(log)
                            if gps_t:
                                result["gps_time"] = gps_t - seg["number"] * 60
                                need_time = False
                        if not need_coords and not need_time:
                            break

                # Skip stubs: < 2 segments AND no GPS (too short to be useful)
                has_gps = result.get("start_lat") is not None
                if seg_count < 2 and not has_gps:
                    continue

                entry = self._log_to_metadata_entry(local_id, result)
                entry["enriched"] = False  # Full enrichment deferred to Enrich button
                self._metadata[local_id] = entry

                # Reverse-geocode start address from initData GPS
                lat = result.get("start_lat")
                lng = result.get("start_lng")
                if lat and lng:
                    addr, is_road = _reverse_geocode(lat, lng)
                    if addr:
                        entry["start_address"] = addr

                enriched += 1
            except Exception as e:
                logger.debug("Enrich error for %s: %s", local_id, e)

        if enriched:
            self._rebuild_routes()
            self._save_metadata()
            logger.info("Enriched %d/%d new routes", enriched, len(new_routes))

        return enriched

    def scan(self, force: bool = False) -> dict:
        """Directory scan — listing only, no log parsing or enrichment."""
        if not force and (time.time() - self._last_scan) < CACHE_TTL:
            return self._routes

        raw: dict = defaultdict(lambda: {"segments": [], "total_size": 0, "mtime": 0})

        if not self.data_dir.exists():
            logger.warning("Data directory does not exist: %s", self.data_dir)
            self._routes = {}
            self._last_scan = time.time()
            return self._routes

        for entry in self.data_dir.iterdir():
            if not entry.is_dir():
                continue
            m = ROUTE_DIR_RE.match(entry.name)
            if not m:
                continue
            local_id = m.group(1)
            seg_num = int(m.group(2))

            try:
                seg_size = sum(f.stat().st_size for f in entry.iterdir() if f.is_file())
                seg_mtime = entry.stat().st_mtime
            except OSError:
                continue

            raw[local_id]["segments"].append({
                "number": seg_num,
                "path": str(entry),
                "size": seg_size,
                "files": {f.name for f in entry.iterdir() if f.is_file()},
            })
            raw[local_id]["total_size"] += seg_size
            raw[local_id]["mtime"] = max(raw[local_id]["mtime"], seg_mtime)

        for info in raw.values():
            info["segments"].sort(key=lambda s: s["number"])

        self._raw = dict(raw)
        self._rebuild_routes()
        self._last_scan = time.time()

        logger.info("Scanned %d routes, %d total segments (%d in metadata.json)",
                     len(self._routes),
                     sum(len(r["_segments"]) for r in self._routes.values()),
                     len(self._metadata))

        return self._routes

    async def async_scan(self, force: bool = False) -> dict:
        """Non-blocking scan — returns stale cache instantly, refreshes in background.

        Never blocks the caller. If cache is expired, kicks off a background
        thread scan and returns the (stale) cached data immediately. The next
        request after the scan completes will see fresh data.
        Skips scan entirely while driving — dashboard only needs live telemetry,
        not route listings. This avoids 6s of I/O competing with real-time data.
        """
        if not force and (time.time() - self._last_scan) < CACHE_TTL:
            return self._routes
        # While driving, skip all scans — route data doesn't change and
        # the CPU/IO should be reserved for real-time dashboard telemetry
        if not force and self._is_onroad():
            return self._routes
        import asyncio
        if force or not self._routes:
            # First scan or forced — must wait for data
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, lambda: self.scan(force=force))
        # Stale cache — return immediately, refresh in background
        if not self._bg_scanning:
            self._bg_scanning = True
            loop = asyncio.get_event_loop()
            loop.run_in_executor(None, self._bg_scan)
        return self._routes

    def _bg_scan(self):
        """Background scan worker — updates cache, resets flag."""
        try:
            self.scan()
        finally:
            self._bg_scanning = False

    @property
    def dongle_id(self):
        return self._dongle_id

    def get_route(self, fullname: str) -> dict | None:
        self.scan()
        route = self._routes.get(fullname)
        if not route:
            # Fallback: try lookup by local_id (e.g. 0000011d--258ef95d84)
            fn = self._local_id_map.get(fullname)
            if fn:
                route = self._routes.get(fn)
        return route

    def get_local_id(self, fullname: str) -> str | None:
        self.scan()
        lid = self._fullname_map.get(fullname)
        if not lid and fullname in self._local_id_map:
            lid = fullname  # Already a local_id
        return lid

    def get_route_by_local_id(self, local_id: str) -> dict | None:
        self.scan()
        fullname = self._local_id_map.get(local_id)
        return self._routes.get(fullname) if fullname else None

    def hide_route(self, local_id: str):
        """Mark a route as hidden (soft-delete). Hidden from listings, auto-purged after 7 days."""
        self._hidden[local_id] = time.time()
        self._preserved.discard(local_id)
        self._rebuild_routes()
        self._save_metadata()

    def preserve_route(self, local_id: str):
        """Mark a route as preserved (protected from cleanup)."""
        self._preserved.add(local_id)
        self._save_metadata()

    def unpreserve_route(self, local_id: str):
        """Remove preservation from a route."""
        self._preserved.discard(local_id)
        self._save_metadata()

    def is_preserved(self, local_id: str) -> bool:
        return local_id in self._preserved

    def set_note(self, local_id: str, note: str):
        """Set or update the note for a route."""
        meta = self._metadata.get(local_id)
        if not meta:
            meta = {"route_id": local_id}
            self._metadata[local_id] = meta
        meta["notes"] = note
        self._rebuild_routes()
        self._save_metadata()

    def add_bookmark(self, local_id: str, time_sec: float, label: str) -> list:
        """Add a bookmark to a route. Returns updated bookmark list."""
        meta = self._metadata.get(local_id)
        if not meta:
            meta = {"route_id": local_id}
            self._metadata[local_id] = meta
        bookmarks = meta.get("bookmarks", [])
        bookmarks.append({"time_sec": round(time_sec, 1), "label": label})
        bookmarks.sort(key=lambda b: b["time_sec"])
        meta["bookmarks"] = bookmarks
        self._executor.submit(self._save_metadata)
        return bookmarks

    def update_bookmark(self, local_id: str, index: int, label: str) -> list:
        """Update a bookmark's label by index. Returns updated bookmark list."""
        meta = self._metadata.get(local_id)
        if not meta:
            return []
        bookmarks = meta.get("bookmarks", [])
        if 0 <= index < len(bookmarks):
            bookmarks[index]["label"] = label
        self._executor.submit(self._save_metadata)
        return bookmarks

    def delete_bookmark(self, local_id: str, index: int) -> list:
        """Delete a bookmark by index. Returns updated bookmark list."""
        meta = self._metadata.get(local_id)
        if not meta:
            return []
        bookmarks = meta.get("bookmarks", [])
        if 0 <= index < len(bookmarks):
            bookmarks.pop(index)
        meta["bookmarks"] = bookmarks
        self._executor.submit(self._save_metadata)
        return bookmarks

    def get_recycled_routes(self) -> list[dict]:
        """Return route dicts for hidden and invalid routes (recycled bin).

        Includes:
        - Routes in _hidden set → recycled_reason: "deleted"
        - Routes with maxqlog < 1 and no distance (stubs) → recycled_reason: "invalid"
        - Routes with no wall_time_nanos → recycled_reason: "invalid"

        Uses _build_route() to construct route dicts with recycled_reason added.
        """
        recycled = []
        for local_id, info in self._raw.items():
            internal = self._meta_to_internal(local_id)
            route = self._build_route(local_id, info, internal)

            if local_id in self._hidden:
                route["recycled_reason"] = "deleted"
                route["hidden_at"] = self._hidden[local_id]
            elif route["maxqlog"] < 1 and not route.get("distance"):
                route["recycled_reason"] = "invalid"
            elif not internal.get("wall_time_nanos"):
                route["recycled_reason"] = "invalid"
            else:
                continue  # valid, non-hidden route — skip

            recycled.append(route)

        recycled.sort(key=lambda r: _route_counter(r.get("_local_id", "")), reverse=True)
        return recycled

    def resolve_segment_path(self, fullname: str, segment: int, filename: str) -> Path | None:
        """Resolve a comma-style path to a local file."""
        local_id = self.get_local_id(fullname)
        if not local_id:
            return None
        path = self.data_dir / f"{local_id}--{segment}" / filename
        return path if path.exists() else None

    def clear_derived(self, local_id: str) -> int:
        """Delete cached events.json and coords.json for a route.

        Returns count of files deleted. The next events/coords fetch will
        regenerate them from qlogs/rlogs with the latest parser code.
        """
        info = self._raw.get(local_id)
        if not info:
            return 0
        deleted = 0
        for seg in info["segments"]:
            for fname in ("events.json", "coords.json"):
                p = Path(seg["path"]) / fname
                if p.exists():
                    p.unlink()
                    deleted += 1
        if deleted:
            logger.info("Cleared %d derived files for %s", deleted, local_id)
        return deleted

    def ensure_enriched(self, local_id: str) -> bool:
        """On-demand enrichment for a single route. Returns True if newly enriched.

        Called when the user selects a route in the UI. Synchronous — blocks the
        request briefly (~1-2s for 5MB head read) but avoids background CPU churn
        for routes the user never looks at.
        Skips enrichment while driving to avoid competing with openpilot.
        """
        if self._is_onroad():
            return False
        if not self._needs_enrich(local_id):
            return False
        info = self._raw.get(local_id)
        if not info:
            return False
        log_meta = self._enrich_one(local_id, info["segments"])
        if log_meta:
            entry = self._log_to_metadata_entry(local_id, log_meta)
            self._metadata[local_id] = entry
            self._rebuild_routes()
            self._save_metadata()
            logger.info("On-demand enriched route %s", local_id)
            return True
        return False
