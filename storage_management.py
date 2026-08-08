"""
Storage management for Connect on Device.

Handles route preservation, soft-deletion, disk cleanup, and download streaming.
Keeps server.py slim by isolating all storage logic here.

Cleanup is a wrapper around openpilot's stock deleter (system/loggerd/deleter.py)
with COD-specific logic:
- Recycled routes: auto-purge after 7 days
- COD-saved routes: respected unless emergency (<10GB)
- xattr-preserved routes (from comma cloud): always respected by COD
- Target: 20GB free
"""

import io
import logging
import os
import shutil
import tarfile
import time
from pathlib import Path

from route_store import _route_counter

logger = logging.getLogger("connect.storage")

# File types available for download
DOWNLOAD_FILES = {
    "rlog": ["rlog.zst", "rlog"],
    "qcamera": ["qcamera.ts"],
    "fcamera": ["fcamera.hevc"],
    "ecamera": ["ecamera.hevc"],
    "qlog": ["qlog.zst", "qlog"],
}

# COD cleanup thresholds
MIN_FREE_BYTES = 20 * 1024 * 1024 * 1024   # 20 GB — phase 1 threshold
EMERGENCY_BYTES = 10 * 1024 * 1024 * 1024   # 10 GB — phase 2 (emergency) threshold
RECYCLE_TTL = 7 * 86400                      # 7 days before recycled routes auto-purge


def get_storage_info(store) -> dict:
    """Get disk usage stats for the data directory."""
    stat = shutil.disk_usage(store.data_dir)
    return {
        "total": stat.total,
        "used": stat.used,
        "free": stat.free,
        "percent_free": round(stat.free / stat.total * 100, 1),
        "hidden_count": len(store._hidden),
        "preserved_count": len(store._preserved),
    }


def has_xattr_preserve(store, local_id: str) -> bool:
    """Check if any segment of a route has the user.preserve xattr (comma cloud)."""
    info = store._raw.get(local_id)
    if not info:
        return False
    for seg in info["segments"]:
        seg_path = Path(seg["path"])
        if not seg_path.exists():
            continue
        try:
            os.getxattr(str(seg_path), b"user.preserve")
            return True
        except OSError:
            pass
    return False


def _free_bytes(store) -> int:
    """Current free bytes on the data partition."""
    return shutil.disk_usage(store.data_dir).free


def run_cleanup(store) -> dict:
    """Single cleanup pass — COD wrapper around stock deleter logic.

    Phase 0:  Expired recycled routes (>7 days) — always, regardless of storage.
    Phase 0b: Aged-out invalid stubs (>7 days) — always, regardless of storage.
    Phase 1a: Remaining recycle-bin routes (deleted + invalid) — when free < 20GB,
              reclaimed BEFORE any still-valid route since they're already discarded.
    Phase 1:  Normal routes (not saved, not xattr-preserved) — when free < 20GB.
    Phase 2:  COD-saved routes — emergency only, when free < 10GB after phase 1.

    xattr-preserved routes (comma cloud) are never deleted by COD.

    Returns summary of actions taken.
    """
    now = time.time()
    deleted = []

    # ── Phase 0: Expired recycled routes (always) ───────────────────────
    expired = [
        (lid, hide_time) for lid, hide_time in list(store._hidden.items())
        if now - hide_time > RECYCLE_TTL and lid in store._raw
    ]
    for lid, hide_time in expired:
        age_days = (now - hide_time) / 86400
        _delete_route_from_disk(store, lid)
        deleted.append({"route": lid, "reason": "recycled_expired", "age_days": round(age_days, 1)})
        logger.info("Cleanup: purged expired recycled route %s (%.1f days old)", lid, age_days)

    # ── Phase 0b: Aged-out invalid stubs (always) ───────────────────────
    # Single-segment boot/aborted stubs (maxqlog 0, no distance) never enter
    # _hidden, so Phase 0 never sees them and they linger in the recycled bin
    # until storage runs low. Purge those older than the recycle TTL, aged by
    # segment mtime (unenriched stubs have no reliable create_time).
    for lid, info in list(store._raw.items()):
        if lid in store._hidden or lid in store._preserved:
            continue
        segs = info["segments"]
        if max((s["number"] for s in segs), default=0) >= 1:
            continue  # multi-segment — not a stub
        if store._calc_route_distance(lid, segs):
            continue  # has distance — a real (short) drive, keep
        try:
            mtime = max(Path(s["path"]).stat().st_mtime for s in segs)
        except (OSError, ValueError):
            continue
        age_days = (now - mtime) / 86400
        if now - mtime <= RECYCLE_TTL:
            continue  # keep recent stubs briefly (may be mid-recording)
        _delete_route_from_disk(store, lid)
        deleted.append({"route": lid, "reason": "stub_expired", "age_days": round(age_days, 1)})
        logger.info("Cleanup: purged aged stub route %s (%.1f days old)", lid, age_days)

    # ── Phase 1a: Reclaim the recycle bin first when free < 20GB ────────
    # Routes already in the recycle bin (user-deleted + invalid) should be
    # reclaimed before any still-valid route, even inside their 7-day grace.
    # Order: deleted routes first (oldest-deleted first), then invalid stubs;
    # xattr-preserved (comma cloud) routes are still never touched.
    free = _free_bytes(store)
    if free < MIN_FREE_BYTES:
        def _recycle_key(r):
            deleted_first = 0 if r.get("recycled_reason") == "deleted" else 1
            age = r.get("hidden_at") or _route_counter(r.get("_local_id", ""))
            return (deleted_first, age)

        for r in sorted(store.get_recycled_routes(), key=_recycle_key):
            lid = r["_local_id"]
            if lid in store._preserved or has_xattr_preserve(store, lid):
                continue
            _delete_route_from_disk(store, lid)
            deleted.append({"route": lid, "reason": "recycled_low_storage",
                            "recycled_reason": r.get("recycled_reason")})
            logger.info("Cleanup: purged recycled route %s (%s, low storage)",
                        lid, r.get("recycled_reason"))
            free = _free_bytes(store)
            if free >= MIN_FREE_BYTES:
                break

    # ── Phase 1: Normal routes when free < 20GB ─────────────────────────
    free = _free_bytes(store)
    if free < MIN_FREE_BYTES:
        # Candidates: not saved, not hidden, not xattr-preserved
        candidates = []
        for lid in list(store._raw.keys()):
            if lid in store._preserved or lid in store._hidden:
                continue
            if has_xattr_preserve(store, lid):
                continue
            candidates.append(lid)

        def _has_bookmarks(lid: str) -> bool:
            meta = store._metadata.get(lid) or {}
            return bool(meta.get("bookmarks")
                        or (meta.get("hud_capture_state") or {}).get("bookmarks"))

        # Oldest first by route counter; bookmarked routes go last so their
        # footage survives until the screenshot worker has extracted the taps.
        candidates.sort(key=lambda lid: (_has_bookmarks(lid), _route_counter(lid)))

        for lid in candidates:
            _delete_route_from_disk(store, lid)
            deleted.append({"route": lid, "reason": "low_storage"})
            logger.info("Cleanup: deleted normal route %s (low storage)", lid)
            free = _free_bytes(store)
            if free >= MIN_FREE_BYTES:
                break

    # ── Phase 2: Emergency — COD-saved routes when free < 10GB ──────────
    free = _free_bytes(store)
    if free < EMERGENCY_BYTES:
        saved = [lid for lid in list(store._preserved) if lid in store._raw]
        saved.sort(key=_route_counter)
        for lid in saved:
            if has_xattr_preserve(store, lid):
                continue
            _delete_route_from_disk(store, lid)
            deleted.append({"route": lid, "reason": "emergency"})
            logger.warning("Cleanup: deleted SAVED route %s (emergency, free < 10GB)", lid)
            free = _free_bytes(store)
            if free >= EMERGENCY_BYTES:
                break

    # ── Screenshots: cap at 500MB, delete oldest when exceeded ──────────
    from handlers.screenshots import SCREENSHOTS_DIR
    _cleanup_screenshots(SCREENSHOTS_DIR)

    # ── HLS cache eviction: keep only the most recent route ─────────────
    from config import COD_CACHE_DIR, COD_HUD_CACHE_DIR
    hls_cache = Path(COD_CACHE_DIR) / "qcamera_hls"
    if hls_cache.exists():
        cached = sorted(hls_cache.iterdir(), key=lambda d: d.stat().st_mtime, reverse=True)
        for d in cached[1:]:  # evict all but newest
            if d.is_dir():
                shutil.rmtree(d, ignore_errors=True)

    # ── HUD render cache: cap at 500MB, delete oldest MP4s ────────────
    _cleanup_hud_cache(COD_HUD_CACHE_DIR)

    # ── General cache: cap MP4 muxes at 500MB, delete oldest ──────────
    _cleanup_media_cache(COD_CACHE_DIR)

    if deleted:
        store._save_metadata()

    return {"free_bytes": free, "deleted": deleted}


SCREENSHOT_MAX_BYTES = 500 * 1024 * 1024  # 500 MB cap for screenshots
HUD_CACHE_MAX_BYTES = 500 * 1024 * 1024   # 500 MB cap for HUD renders
MEDIA_CACHE_MAX_BYTES = 500 * 1024 * 1024 # 500 MB cap for MP4 muxes in general cache


def _cleanup_hud_cache(hud_cache_dir: str):
    """Delete oldest HUD render MP4s when total size exceeds cap.
    Also removes orphaned status files without a matching MP4."""
    if not os.path.isdir(hud_cache_dir):
        return
    mp4s = []
    status_files = []
    total = 0
    for name in os.listdir(hud_cache_dir):
        path = os.path.join(hud_cache_dir, name)
        if name.endswith('.mp4'):
            size = os.path.getsize(path)
            mp4s.append((os.path.getmtime(path), path, size))
            total += size
        elif name.endswith('.status.json'):
            status_files.append(path)
        elif name in ('preview.jpg', 'preview.jpg.tmp'):
            try:
                os.unlink(path)
            except OSError:
                pass

    if total <= HUD_CACHE_MAX_BYTES:
        return

    # Delete oldest MP4s until under cap
    mp4s.sort()  # oldest first
    for mtime, path, size in mp4s:
        if total <= HUD_CACHE_MAX_BYTES:
            break
        try:
            os.unlink(path)
            total -= size
            # Remove matching status file
            base = os.path.basename(path)
            for sf in status_files:
                if os.path.basename(sf).startswith(base.split('_')[0]):
                    try:
                        os.unlink(sf)
                    except OSError:
                        pass
            logger.info("HUD cache cleanup: deleted %s (%.1fMB)", base, size / 1024 / 1024)
        except OSError:
            pass


def _cleanup_screenshots(screenshots_dir: str):
    """Delete oldest screenshots when total size exceeds cap."""
    if not os.path.isdir(screenshots_dir):
        return
    files = []
    total = 0
    for name in os.listdir(screenshots_dir):
        if not name.lower().endswith('.png'):
            continue
        path = os.path.join(screenshots_dir, name)
        try:
            stat = os.stat(path)
            files.append((path, stat.st_mtime, stat.st_size))
            total += stat.st_size
        except OSError:
            continue

    if total <= SCREENSHOT_MAX_BYTES:
        return

    # Sort oldest first
    files.sort(key=lambda f: f[1])
    for path, _, size in files:
        if total <= SCREENSHOT_MAX_BYTES:
            break
        try:
            os.remove(path)
            total -= size
            logger.info("Cleanup: deleted screenshot %s", os.path.basename(path))
        except OSError:
            pass


def _cleanup_media_cache(cache_dir: str):
    """Delete oldest MP4 muxes in the general cache when total size exceeds cap.
    Only targets .mp4 files directly in cache_dir (not subdirectories like qcamera_hls/)."""
    if not os.path.isdir(cache_dir):
        return
    mp4s = []
    total = 0
    for name in os.listdir(cache_dir):
        if not name.endswith('.mp4'):
            continue
        path = os.path.join(cache_dir, name)
        if not os.path.isfile(path):
            continue
        try:
            stat = os.stat(path)
            mp4s.append((stat.st_mtime, path, stat.st_size))
            total += stat.st_size
        except OSError:
            continue

    if total <= MEDIA_CACHE_MAX_BYTES:
        return

    mp4s.sort()  # oldest first
    for mtime, path, size in mp4s:
        if total <= MEDIA_CACHE_MAX_BYTES:
            break
        try:
            os.unlink(path)
            total -= size
            logger.info("Media cache cleanup: deleted %s (%.1fMB)", os.path.basename(path), size / 1024 / 1024)
        except OSError:
            pass


def _delete_route_from_disk(store, local_id: str):
    """Remove all segment directories for a route from disk and clean up state."""
    info = store._raw.get(local_id)
    if info:
        for seg in info["segments"]:
            seg_path = Path(seg["path"])
            if seg_path.exists():
                shutil.rmtree(seg_path, ignore_errors=True)

    store._hidden.pop(local_id, None)
    store._preserved.discard(local_id)
    store._raw.pop(local_id, None)
    store._metadata.pop(local_id, None)


def build_download_tar(store, local_id: str, file_types: list[str], segments: list[int] | None = None) -> io.BytesIO | None:
    """Build a tar.gz archive of requested files across segments.

    Args:
        store: RouteStore instance
        local_id: Route local_id (e.g. "00000042--abc123")
        file_types: List of file type keys from DOWNLOAD_FILES
        segments: Optional list of segment numbers to include (None = all)

    Returns:
        BytesIO containing tar.gz data, or None if no files found.
    """
    info = store._raw.get(local_id)
    if not info:
        return None

    buf = io.BytesIO()
    files_added = 0

    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for seg in sorted(info["segments"], key=lambda s: s["number"]):
            if segments is not None and seg["number"] not in segments:
                continue
            seg_path = Path(seg["path"])
            seg_name = f"{local_id}--{seg['number']}"

            for ftype in file_types:
                candidates = DOWNLOAD_FILES.get(ftype, [])
                for fname in candidates:
                    fpath = seg_path / fname
                    if fpath.exists():
                        arcname = f"{seg_name}/{fname}"
                        tar.add(str(fpath), arcname=arcname)
                        files_added += 1
                        break  # use first match (e.g. rlog.zst over rlog)

    if files_added == 0:
        return None

    buf.seek(0)
    return buf
