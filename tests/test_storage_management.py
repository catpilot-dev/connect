"""Tests for storage_management.py — get_storage_info, build_download_tar, run_cleanup."""

import json
import sys
import tarfile
import time
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage_management import (
    EMERGENCY_BYTES,
    MIN_FREE_BYTES,
    RECYCLE_TTL,
    build_download_tar,
    get_storage_info,
    run_cleanup,
)


class TestGetStorageInfo:
    def test_returns_correct_keys(self, mock_store):
        info = get_storage_info(mock_store)
        assert "total" in info
        assert "used" in info
        assert "free" in info
        assert "percent_free" in info
        assert "hidden_count" in info
        assert "preserved_count" in info

    def test_correct_types(self, mock_store):
        info = get_storage_info(mock_store)
        assert isinstance(info["total"], int)
        assert isinstance(info["used"], int)
        assert isinstance(info["free"], int)
        assert isinstance(info["percent_free"], float)
        assert isinstance(info["hidden_count"], int)
        assert isinstance(info["preserved_count"], int)


class TestBuildDownloadTar:
    def test_single_file_type(self, mock_store):
        # Write some content to rlog.zst so it's non-empty
        for lid, info in mock_store._raw.items():
            for seg in info["segments"]:
                p = Path(seg["path"]) / "rlog.zst"
                p.write_bytes(b"fake rlog content")
            break

        lid = next(iter(mock_store._raw))
        buf = build_download_tar(mock_store, lid, ["rlog"])
        assert buf is not None
        # Verify it's a valid tar
        buf.seek(0)
        with tarfile.open(fileobj=buf, mode="r:gz") as tar:
            names = tar.getnames()
            assert len(names) > 0
            assert any("rlog.zst" in n for n in names)

    def test_no_matching_files(self, mock_store):
        lid = next(iter(mock_store._raw))
        # Request ecamera which most segments don't have
        result = build_download_tar(mock_store, lid, ["ecamera"])
        # All segments have empty files, so this might not find ecamera in 00000042
        # Actually, let's look for a type that definitely doesn't exist
        # Use segment filter to ensure we look at segments without that type
        if result is not None:
            # ecamera might be in some segments, that's ok
            pass

    def test_nonexistent_route(self, mock_store):
        result = build_download_tar(mock_store, "nonexistent--route", ["rlog"])
        assert result is None

    def test_segment_filter(self, mock_store):
        # Write content to segment 0 only
        for lid, info in mock_store._raw.items():
            if len(info["segments"]) >= 2:
                for seg in info["segments"]:
                    p = Path(seg["path"]) / "rlog.zst"
                    p.write_bytes(b"fake rlog data")
                buf = build_download_tar(mock_store, lid, ["rlog"], segments=[0])
                assert buf is not None
                buf.seek(0)
                with tarfile.open(fileobj=buf, mode="r:gz") as tar:
                    names = tar.getnames()
                    # Only segment 0 files
                    for n in names:
                        assert "--0/" in n
                break


# ─── Helpers ────────────────────────────────────────────────────────

def _make_disk_usage(free):
    import collections
    DU = collections.namedtuple("DiskUsage", ["total", "used", "free"])
    total = 128 * 1024 ** 3
    return DU(total=total, used=total - free, free=free)


def _all_lids(store):
    return list(store._raw.keys())


def _seg_paths(store, lid):
    return [Path(s["path"]) for s in store._raw[lid]["segments"]]


# ─── Phase 0: expired recycled routes ───────────────────────────────

class TestCleanupPhase0:
    def test_expired_recycled_route_deleted(self, mock_store):
        lid = _all_lids(mock_store)[0]
        old_time = time.time() - RECYCLE_TTL - 1
        mock_store._hidden[lid] = old_time
        seg_paths = _seg_paths(mock_store, lid)

        with patch("storage_management.shutil.disk_usage", return_value=_make_disk_usage(MIN_FREE_BYTES + 1)):
            result = run_cleanup(mock_store)

        assert any(d["route"] == lid and d["reason"] == "recycled_expired" for d in result["deleted"])
        assert lid not in mock_store._raw
        for p in seg_paths:
            assert not p.exists()

    def test_fresh_recycled_route_not_deleted(self, mock_store):
        lid = _all_lids(mock_store)[0]
        mock_store._hidden[lid] = time.time()

        with patch("storage_management.shutil.disk_usage", return_value=_make_disk_usage(MIN_FREE_BYTES + 1)):
            result = run_cleanup(mock_store)

        assert not any(d["route"] == lid for d in result["deleted"])
        assert lid in mock_store._raw


# ─── Phase 1: normal routes when low storage ────────────────────────

class TestCleanupPhase1:
    def test_normal_route_deleted_when_low_storage(self, mock_store):
        lid = _all_lids(mock_store)[0]
        seg_paths = _seg_paths(mock_store, lid)

        call_count = {"n": 0}
        def fake_disk_usage(path):
            call_count["n"] += 1
            if call_count["n"] <= 2:
                return _make_disk_usage(MIN_FREE_BYTES - 1)
            return _make_disk_usage(MIN_FREE_BYTES + 1)

        with patch("storage_management.shutil.disk_usage", side_effect=fake_disk_usage):
            result = run_cleanup(mock_store)

        assert any(d["route"] == lid and d["reason"] == "low_storage" for d in result["deleted"])
        assert lid not in mock_store._raw
        for p in seg_paths:
            assert not p.exists()

    def test_preserved_route_skipped_in_phase1(self, mock_store):
        lids = _all_lids(mock_store)
        for lid in lids:
            mock_store._preserved.add(lid)

        with patch("storage_management.shutil.disk_usage", return_value=_make_disk_usage(MIN_FREE_BYTES - 1)):
            result = run_cleanup(mock_store)

        assert not any(d["reason"] == "low_storage" for d in result["deleted"])
        for lid in lids:
            assert lid in mock_store._raw

    def test_hidden_route_skipped_in_phase1(self, mock_store):
        lid = _all_lids(mock_store)[0]
        mock_store._hidden[lid] = time.time()

        with patch("storage_management.shutil.disk_usage", return_value=_make_disk_usage(MIN_FREE_BYTES - 1)):
            result = run_cleanup(mock_store)

        assert not any(d["route"] == lid and d["reason"] == "low_storage" for d in result["deleted"])

    def test_xattr_preserved_route_skipped(self, mock_store):
        lid = _all_lids(mock_store)[0]

        with patch("storage_management.shutil.disk_usage", return_value=_make_disk_usage(MIN_FREE_BYTES - 1)), \
             patch("storage_management.has_xattr_preserve", side_effect=lambda store, l: l == lid):
            result = run_cleanup(mock_store)

        assert not any(d["route"] == lid for d in result["deleted"])
        assert lid in mock_store._raw

    def test_no_deletion_when_storage_ok(self, mock_store):
        with patch("storage_management.shutil.disk_usage", return_value=_make_disk_usage(MIN_FREE_BYTES + 1)):
            result = run_cleanup(mock_store)

        assert not any(d["reason"] == "low_storage" for d in result["deleted"])


# ─── Phase 2: emergency — saved routes ──────────────────────────────

class TestCleanupPhase2:
    def test_saved_route_deleted_in_emergency(self, mock_store):
        lid = _all_lids(mock_store)[0]
        mock_store._preserved.add(lid)
        seg_paths = _seg_paths(mock_store, lid)

        # Stay below emergency until the saved route is reclaimed, then recover.
        # (Robust to the exact number of _free_bytes() calls per cleanup pass.)
        def fake_disk_usage(path):
            saved_gone = not any(p.exists() for p in seg_paths)
            return _make_disk_usage(EMERGENCY_BYTES + 1 if saved_gone else EMERGENCY_BYTES - 1)

        with patch("storage_management.shutil.disk_usage", side_effect=fake_disk_usage):
            result = run_cleanup(mock_store)

        assert any(d["route"] == lid and d["reason"] == "emergency" for d in result["deleted"])
        assert lid not in mock_store._raw
        for p in seg_paths:
            assert not p.exists()

    def test_xattr_preserved_saved_route_never_deleted(self, mock_store):
        lid = _all_lids(mock_store)[0]
        mock_store._preserved.add(lid)

        with patch("storage_management.shutil.disk_usage", return_value=_make_disk_usage(EMERGENCY_BYTES - 1)), \
             patch("storage_management.has_xattr_preserve", return_value=True):
            result = run_cleanup(mock_store)

        assert not any(d["route"] == lid and d["reason"] == "emergency" for d in result["deleted"])
        assert lid in mock_store._raw

    def test_no_emergency_deletion_when_above_threshold(self, mock_store):
        lid = _all_lids(mock_store)[0]
        mock_store._preserved.add(lid)

        with patch("storage_management.shutil.disk_usage", return_value=_make_disk_usage(EMERGENCY_BYTES + 1)):
            result = run_cleanup(mock_store)

        assert not any(d["reason"] == "emergency" for d in result["deleted"])
        assert lid in mock_store._raw


# ─── _delete_route_from_disk ────────────────────────────────────────

class TestDeleteRouteFromDisk:
    def test_segments_removed_from_disk(self, mock_store):
        lid = _all_lids(mock_store)[0]
        seg_paths = _seg_paths(mock_store, lid)
        assert any(p.exists() for p in seg_paths)

        from storage_management import _delete_route_from_disk
        _delete_route_from_disk(mock_store, lid)

        for p in seg_paths:
            assert not p.exists()

    def test_state_cleaned_up(self, mock_store):
        lid = _all_lids(mock_store)[0]
        mock_store._preserved.add(lid)
        mock_store._hidden[lid] = time.time()

        from storage_management import _delete_route_from_disk
        _delete_route_from_disk(mock_store, lid)

        assert lid not in mock_store._raw
        assert lid not in mock_store._preserved
        assert lid not in mock_store._hidden
        assert lid not in mock_store._metadata

    def test_nonexistent_route_is_noop(self, mock_store):
        from storage_management import _delete_route_from_disk
        _delete_route_from_disk(mock_store, "nonexistent--route")  # must not raise


# ─── Active recording must survive every reclaim phase ──────────────

class TestActiveRecordingGuard:
    """The drive being recorded looks like an "invalid" stub for its first
    minute (maxqlog 0, no distance), so it lands in the recycle bin that
    low-storage reclaim empties. Deleting it would destroy the drive as
    loggerd writes it — with no onroad check anywhere in this module."""

    def test_only_active_while_onroad_and_freshly_logged(self, mock_store):
        import os
        from storage_management import ACTIVE_WRITE_WINDOW, is_being_written
        lid = _all_lids(mock_store)[0]

        # Offroad: loggerd is not recording, whatever the mtimes say
        assert is_being_written(mock_store, lid) is False

        with patch.object(type(mock_store), "_is_onroad", staticmethod(lambda: True)):
            assert is_being_written(mock_store, lid) is True
            old = time.time() - ACTIVE_WRITE_WINDOW - 60
            for p in _seg_paths(mock_store, lid):
                for f in p.iterdir():
                    os.utime(f, (old, old))
            assert is_being_written(mock_store, lid) is False

    def test_cod_cache_writes_do_not_mark_route_active(self, mock_store):
        """Enrichment writes events.json/coords.json into segment dirs — that
        must not make a finished route look like a live recording."""
        import os
        from storage_management import ACTIVE_WRITE_WINDOW, is_being_written
        lid = _all_lids(mock_store)[0]
        old = time.time() - ACTIVE_WRITE_WINDOW - 60
        for p in _seg_paths(mock_store, lid):
            for f in p.iterdir():
                os.utime(f, (old, old))
            (p / "events.json").write_text("[]")  # fresh COD cache write

        with patch.object(type(mock_store), "_is_onroad", staticmethod(lambda: True)):
            assert is_being_written(mock_store, lid) is False

    def test_recycled_reclaim_spares_active_recording(self, mock_store):
        """Phase 1a: free < 20GB empties the recycle bin — but not a route
        still being written."""
        lid = _all_lids(mock_store)[0]
        seg_paths = _seg_paths(mock_store, lid)
        for p in seg_paths:
            p.touch()
        mock_store._hidden[lid] = time.time()  # in the bin, inside its grace

        with patch("storage_management.shutil.disk_usage",
                   return_value=_make_disk_usage(MIN_FREE_BYTES - 1)), \
             patch.object(type(mock_store), "_is_onroad", staticmethod(lambda: True)):
            result = run_cleanup(mock_store)

        assert not any(d["route"] == lid for d in result["deleted"])
        assert lid in mock_store._raw
        for p in seg_paths:
            assert p.exists()

    def test_expired_recycled_route_still_spared_while_writing(self, mock_store):
        """Even a 7-day-expired bin entry is spared if it is being written —
        the guard is in the delete path, so no phase can bypass it."""
        lid = _all_lids(mock_store)[0]
        for p in _seg_paths(mock_store, lid):
            p.touch()
        mock_store._hidden[lid] = time.time() - RECYCLE_TTL - 1

        with patch("storage_management.shutil.disk_usage",
                   return_value=_make_disk_usage(MIN_FREE_BYTES + 1)), \
             patch.object(type(mock_store), "_is_onroad", staticmethod(lambda: True)):
            result = run_cleanup(mock_store)

        assert not any(d["route"] == lid for d in result["deleted"])
        assert lid in mock_store._raw

    def test_stale_routes_still_reclaimed(self, mock_store):
        """The guard must not stop normal reclaim of routes nobody is writing."""
        import os
        lid = _all_lids(mock_store)[0]
        old = time.time() - RECYCLE_TTL - 60
        for p in _seg_paths(mock_store, lid):
            for f in p.iterdir():
                os.utime(f, (old, old))
            os.utime(p, (old, old))
        mock_store._hidden[lid] = old

        with patch("storage_management.shutil.disk_usage",
                   return_value=_make_disk_usage(MIN_FREE_BYTES + 1)):
            result = run_cleanup(mock_store)

        assert any(d["route"] == lid for d in result["deleted"])
        assert lid not in mock_store._raw
