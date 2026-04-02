"""Tests for COD storage cleanup — run_cleanup() with mock routes and garbage segments.

Creates fake route directories with real (small) files on disk, then exercises
all three cleanup phases:
  Phase 0: Expired recycled routes (>7 days)
  Phase 1: Normal routes deleted when free < 10GB
  Phase 2: Emergency — COD-saved routes when free < 5GB
"""

import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from route_store import RouteStore
from storage_management import (
    EMERGENCY_BYTES,
    MIN_FREE_BYTES,
    RECYCLE_TTL,
    _delete_route_from_disk,
    has_xattr_preserve,
    run_cleanup,
)


# ─── Helpers ──────────────────────────────────────────────────────────

def _make_route(tmp_path, local_id, num_segments=2, file_size=1024):
    """Create fake route segment directories with garbage files."""
    for seg_num in range(num_segments):
        seg_dir = tmp_path / f"{local_id}--{seg_num}"
        seg_dir.mkdir(exist_ok=True)
        (seg_dir / "rlog.zst").write_bytes(os.urandom(file_size))
        (seg_dir / "qcamera.ts").write_bytes(os.urandom(file_size))


def _make_store(tmp_path, route_ids, hidden=None, preserved=None, metadata_routes=None):
    """Create a RouteStore with fake routes, bypassing detection methods."""
    # Write metadata.json
    hidden = hidden or {}
    preserved = preserved or []
    metadata_routes = metadata_routes or {}
    meta = {
        "version": "1.0",
        "last_updated": "2026-02-26T00:00:00+00:00",
        "hidden_routes": hidden,
        "preserved_routes": preserved,
        "routes": metadata_routes,
    }
    (tmp_path / ".route_metadata.json").write_text(json.dumps(meta))

    with patch.object(RouteStore, "_detect_dongle_id"), \
         patch.object(RouteStore, "_detect_agnos_version"):
        store = RouteStore(str(tmp_path))
        store._dongle_id = "test123"
        store._agnos_version = "12.4"
        store.scan(force=True)
    return store


def _fake_free(bytes_free):
    """Patch shutil.disk_usage to report a specific free byte count."""
    FakeUsage = type("FakeUsage", (), {"total": 64 * 1024**3, "used": 64 * 1024**3 - bytes_free, "free": bytes_free})
    return patch("storage_management.shutil.disk_usage", return_value=FakeUsage())


# ─── Phase 0: Expired recycled routes ────────────────────────────────

class TestPhase0ExpiredRecycled:
    def test_expired_recycled_route_is_purged(self, tmp_path):
        """Routes hidden >7 days ago should be deleted regardless of storage."""
        _make_route(tmp_path, "00000001--aaa111", num_segments=2)
        _make_route(tmp_path, "00000002--bbb222", num_segments=2)

        eight_days_ago = time.time() - 8 * 86400
        store = _make_store(tmp_path, ["00000001--aaa111", "00000002--bbb222"],
                            hidden={"00000001--aaa111": eight_days_ago})

        # Plenty of free space — phase 0 should still run
        with _fake_free(50 * 1024**3):
            result = run_cleanup(store)

        assert len(result["deleted"]) == 1
        assert result["deleted"][0]["route"] == "00000001--aaa111"
        assert result["deleted"][0]["reason"] == "recycled_expired"
        # Route should be gone from disk
        assert not (tmp_path / "00000001--aaa111--0").exists()
        assert not (tmp_path / "00000001--aaa111--1").exists()
        # Other route untouched
        assert (tmp_path / "00000002--bbb222--0").exists()
        # Gone from store state
        assert "00000001--aaa111" not in store._hidden
        assert "00000001--aaa111" not in store._raw

    def test_recent_recycled_route_kept(self, tmp_path):
        """Routes hidden <7 days ago should NOT be purged."""
        _make_route(tmp_path, "00000001--aaa111", num_segments=2)

        one_day_ago = time.time() - 1 * 86400
        store = _make_store(tmp_path, ["00000001--aaa111"],
                            hidden={"00000001--aaa111": one_day_ago})

        with _fake_free(50 * 1024**3):
            result = run_cleanup(store)

        assert len(result["deleted"]) == 0
        assert (tmp_path / "00000001--aaa111--0").exists()
        assert "00000001--aaa111" in store._hidden

    def test_multiple_expired_routes(self, tmp_path):
        """All expired recycled routes should be purged in one pass."""
        for i in range(5):
            lid = f"0000000{i}--route{i:03d}"
            _make_route(tmp_path, lid, num_segments=1)

        ten_days_ago = time.time() - 10 * 86400
        hidden = {f"0000000{i}--route{i:03d}": ten_days_ago for i in range(5)}
        store = _make_store(tmp_path, list(hidden.keys()), hidden=hidden)

        with _fake_free(50 * 1024**3):
            result = run_cleanup(store)

        assert len(result["deleted"]) == 5
        assert all(d["reason"] == "recycled_expired" for d in result["deleted"])
        assert len(store._hidden) == 0


# ─── Phase 1: Low storage cleanup ────────────────────────────────────

class TestPhase1LowStorage:
    def test_deletes_oldest_first_when_low(self, tmp_path):
        """When free < 20GB, delete oldest normal routes first."""
        # Create 3 routes — counters: 1, 5, 10 (oldest to newest)
        _make_route(tmp_path, "00000001--oldest00")
        _make_route(tmp_path, "00000005--middle00")
        _make_route(tmp_path, "0000000a--newest00")

        store = _make_store(tmp_path, ["00000001--oldest00", "00000005--middle00", "0000000a--newest00"])

        # First call: 15GB free (below 20GB threshold)
        # After deleting one route, report 25GB so it stops
        call_count = {"n": 0}
        def fake_usage(path):
            call_count["n"] += 1
            if call_count["n"] <= 2:
                # Initial check + first deletion check
                return type("U", (), {"total": 64 * 1024**3, "used": 49 * 1024**3, "free": 15 * 1024**3})()
            return type("U", (), {"total": 64 * 1024**3, "used": 39 * 1024**3, "free": 25 * 1024**3})()

        with patch("storage_management.shutil.disk_usage", side_effect=fake_usage):
            result = run_cleanup(store)

        # Should have deleted the oldest route (counter=1)
        deleted_routes = [d["route"] for d in result["deleted"]]
        assert "00000001--oldest00" in deleted_routes
        assert result["deleted"][0]["reason"] == "low_storage"
        # Newest should survive
        assert "0000000a--newest00" in store._raw

    def test_skips_preserved_routes(self, tmp_path):
        """Phase 1 should skip COD-saved (preserved) routes."""
        _make_route(tmp_path, "00000001--old00000")
        _make_route(tmp_path, "00000002--saved000")  # preserved
        _make_route(tmp_path, "00000003--normal00")

        store = _make_store(tmp_path,
                            ["00000001--old00000", "00000002--saved000", "00000003--normal00"],
                            preserved=["00000002--saved000"])

        # Stay below threshold so it keeps trying
        call_count = {"n": 0}
        def fake_usage(path):
            call_count["n"] += 1
            if call_count["n"] <= 3:
                return type("U", (), {"total": 64 * 1024**3, "used": 57 * 1024**3, "free": 7 * 1024**3})()
            return type("U", (), {"total": 64 * 1024**3, "used": 53 * 1024**3, "free": 11 * 1024**3})()

        with patch("storage_management.shutil.disk_usage", side_effect=fake_usage):
            result = run_cleanup(store)

        deleted_routes = [d["route"] for d in result["deleted"]]
        # Saved route should NOT be deleted in phase 1
        assert "00000002--saved000" not in deleted_routes
        assert "00000002--saved000" in store._preserved

    def test_skips_hidden_routes(self, tmp_path):
        """Phase 1 should skip hidden routes (handled by phase 0)."""
        _make_route(tmp_path, "00000001--normal00")
        _make_route(tmp_path, "00000002--hidden00")

        recent = time.time() - 86400  # 1 day ago, not expired
        store = _make_store(tmp_path,
                            ["00000001--normal00", "00000002--hidden00"],
                            hidden={"00000002--hidden00": recent})

        call_count = {"n": 0}
        def fake_usage(path):
            call_count["n"] += 1
            if call_count["n"] <= 2:
                return type("U", (), {"total": 64 * 1024**3, "used": 57 * 1024**3, "free": 7 * 1024**3})()
            return type("U", (), {"total": 64 * 1024**3, "used": 53 * 1024**3, "free": 11 * 1024**3})()

        with patch("storage_management.shutil.disk_usage", side_effect=fake_usage):
            result = run_cleanup(store)

        deleted_routes = [d["route"] for d in result["deleted"]]
        assert "00000002--hidden00" not in deleted_routes
        assert "00000002--hidden00" in store._hidden

    def test_no_cleanup_when_plenty_of_space(self, tmp_path):
        """When free >= 10GB, phase 1 should not delete anything."""
        _make_route(tmp_path, "00000001--normal00")

        store = _make_store(tmp_path, ["00000001--normal00"])

        with _fake_free(20 * 1024**3):
            result = run_cleanup(store)

        assert len(result["deleted"]) == 0
        assert "00000001--normal00" in store._raw


# ─── Phase 2: Emergency cleanup ──────────────────────────────────────

class TestPhase2Emergency:
    def test_deletes_saved_routes_in_emergency(self, tmp_path):
        """When free < 5GB and phase 1 exhausted, delete saved routes."""
        _make_route(tmp_path, "00000001--saved100")  # preserved, oldest
        _make_route(tmp_path, "00000002--saved200")  # preserved, newer

        store = _make_store(tmp_path,
                            ["00000001--saved100", "00000002--saved200"],
                            preserved=["00000001--saved100", "00000002--saved200"])

        # Always report 4GB free — phase 1 has no candidates, phase 2 kicks in
        call_count = {"n": 0}
        def fake_usage(path):
            call_count["n"] += 1
            if call_count["n"] <= 2:
                return type("U", (), {"total": 64 * 1024**3, "used": 60 * 1024**3, "free": 4 * 1024**3})()
            return type("U", (), {"total": 64 * 1024**3, "used": 58 * 1024**3, "free": 6 * 1024**3})()

        with patch("storage_management.shutil.disk_usage", side_effect=fake_usage):
            result = run_cleanup(store)

        deleted_routes = [d["route"] for d in result["deleted"]]
        # Oldest saved route should be deleted first
        assert "00000001--saved100" in deleted_routes
        assert result["deleted"][0]["reason"] == "emergency"

    def test_emergency_skips_xattr_preserved(self, tmp_path):
        """Even in emergency, xattr-preserved routes are never deleted by COD."""
        _make_route(tmp_path, "00000001--xattr00")
        _make_route(tmp_path, "00000002--saved200")

        store = _make_store(tmp_path,
                            ["00000001--xattr00", "00000002--saved200"],
                            preserved=["00000001--xattr00", "00000002--saved200"])

        # Mock xattr for route 1
        def fake_xattr_preserve(st, lid):
            return lid == "00000001--xattr00"

        call_count = {"n": 0}
        def fake_usage(path):
            call_count["n"] += 1
            if call_count["n"] <= 2:
                return type("U", (), {"total": 64 * 1024**3, "used": 60 * 1024**3, "free": 4 * 1024**3})()
            return type("U", (), {"total": 64 * 1024**3, "used": 58 * 1024**3, "free": 6 * 1024**3})()

        with patch("storage_management.shutil.disk_usage", side_effect=fake_usage), \
             patch("storage_management.has_xattr_preserve", side_effect=fake_xattr_preserve):
            result = run_cleanup(store)

        deleted_routes = [d["route"] for d in result["deleted"]]
        assert "00000001--xattr00" not in deleted_routes
        assert "00000002--saved200" in deleted_routes


# ─── xattr helper ─────────────────────────────────────────────────────

class TestXattrPreserve:
    def test_no_xattr_returns_false(self, tmp_path):
        """Routes without xattr should return False."""
        _make_route(tmp_path, "00000001--normal00")
        store = _make_store(tmp_path, ["00000001--normal00"])
        assert has_xattr_preserve(store, "00000001--normal00") is False

    def test_nonexistent_route_returns_false(self, tmp_path):
        store = _make_store(tmp_path, [])
        assert has_xattr_preserve(store, "nonexistent") is False

    @pytest.mark.skipif(not hasattr(os, "setxattr"), reason="xattr not supported")
    def test_xattr_set_returns_true(self, tmp_path):
        """Routes with user.preserve xattr should return True."""
        _make_route(tmp_path, "00000001--xattr00")
        store = _make_store(tmp_path, ["00000001--xattr00"])
        # Set xattr on first segment dir
        seg_dir = tmp_path / "00000001--xattr00--0"
        try:
            os.setxattr(str(seg_dir), b"user.preserve", b"1")
        except OSError:
            pytest.skip("xattr not supported on this filesystem")
        assert has_xattr_preserve(store, "00000001--xattr00") is True


# ─── _delete_route_from_disk ──────────────────────────────────────────

class TestDeleteRouteFromDisk:
    def test_removes_all_segments(self, tmp_path):
        """All segment directories should be deleted."""
        _make_route(tmp_path, "00000001--todelete", num_segments=3)
        store = _make_store(tmp_path, ["00000001--todelete"])

        assert (tmp_path / "00000001--todelete--0").exists()
        assert (tmp_path / "00000001--todelete--1").exists()
        assert (tmp_path / "00000001--todelete--2").exists()

        _delete_route_from_disk(store, "00000001--todelete")

        assert not (tmp_path / "00000001--todelete--0").exists()
        assert not (tmp_path / "00000001--todelete--1").exists()
        assert not (tmp_path / "00000001--todelete--2").exists()
        assert "00000001--todelete" not in store._raw

    def test_cleans_hidden_and_preserved(self, tmp_path):
        """Should remove route from _hidden dict and _preserved set."""
        _make_route(tmp_path, "00000001--cleanup0")
        store = _make_store(tmp_path, ["00000001--cleanup0"],
                            hidden={"00000001--cleanup0": time.time()},
                            preserved=["00000001--cleanup0"])

        _delete_route_from_disk(store, "00000001--cleanup0")

        assert "00000001--cleanup0" not in store._hidden
        assert "00000001--cleanup0" not in store._preserved
        assert "00000001--cleanup0" not in store._raw


# ─── Backward compatibility ───────────────────────────────────────────

class TestHiddenBackwardCompat:
    def test_list_format_converted_to_dict(self, tmp_path):
        """Old metadata with hidden_routes as list should be converted to dict."""
        _make_route(tmp_path, "00000001--oldformat")

        meta = {
            "version": "1.0",
            "last_updated": "2026-01-01T00:00:00+00:00",
            "hidden_routes": ["00000001--oldformat"],  # old list format
            "preserved_routes": [],
            "routes": {},
        }
        (tmp_path / ".route_metadata.json").write_text(json.dumps(meta))

        with patch.object(RouteStore, "_detect_dongle_id"), \
             patch.object(RouteStore, "_detect_agnos_version"):
            store = RouteStore(str(tmp_path))

        # Should be dict now, not list/set
        assert isinstance(store._hidden, dict)
        assert "00000001--oldformat" in store._hidden
        assert isinstance(store._hidden["00000001--oldformat"], float)

    def test_dict_format_loaded_correctly(self, tmp_path):
        """New metadata with hidden_routes as dict should load directly."""
        _make_route(tmp_path, "00000001--newformat")

        ts = 1740500000.0
        meta = {
            "version": "1.0",
            "last_updated": "2026-01-01T00:00:00+00:00",
            "hidden_routes": {"00000001--newformat": ts},
            "preserved_routes": [],
            "routes": {},
        }
        (tmp_path / ".route_metadata.json").write_text(json.dumps(meta))

        with patch.object(RouteStore, "_detect_dongle_id"), \
             patch.object(RouteStore, "_detect_agnos_version"):
            store = RouteStore(str(tmp_path))

        assert store._hidden["00000001--newformat"] == ts


# ─── Full pipeline: phase ordering ────────────────────────────────────

class TestFullPipeline:
    def test_phase_ordering_all_three(self, tmp_path):
        """Phase 0 runs first, then phase 1, then phase 2 if still critical."""
        # Route A: expired recycled (phase 0)
        _make_route(tmp_path, "00000001--expired0")
        # Route B: normal, not saved (phase 1 candidate)
        _make_route(tmp_path, "00000002--normal00")
        # Route C: preserved / saved (phase 2 candidate)
        _make_route(tmp_path, "00000003--saved000")
        # Route D: preserved / saved, newer (phase 2 candidate)
        _make_route(tmp_path, "00000004--saved001")

        eight_days_ago = time.time() - 8 * 86400
        store = _make_store(
            tmp_path,
            ["00000001--expired0", "00000002--normal00", "00000003--saved000", "00000004--saved001"],
            hidden={"00000001--expired0": eight_days_ago},
            preserved=["00000003--saved000", "00000004--saved001"],
        )

        # Always report 3GB free — all phases should trigger
        with _fake_free(3 * 1024**3):
            result = run_cleanup(store)

        reasons = [d["reason"] for d in result["deleted"]]
        routes = [d["route"] for d in result["deleted"]]

        # Phase 0: expired recycled
        assert "recycled_expired" in reasons
        assert "00000001--expired0" in routes

        # Phase 1: normal route
        assert "low_storage" in reasons
        assert "00000002--normal00" in routes

        # Phase 2: saved routes (oldest first)
        assert "emergency" in reasons
        assert "00000003--saved000" in routes

    def test_get_recycled_routes_has_hidden_at(self, tmp_path):
        """get_recycled_routes() should include hidden_at for hidden routes."""
        _make_route(tmp_path, "00000001--hidden00")
        _make_route(tmp_path, "00000005--visible0", num_segments=3)

        hide_time = time.time() - 2 * 86400
        store = _make_store(
            tmp_path,
            ["00000001--hidden00", "00000005--visible0"],
            hidden={"00000001--hidden00": hide_time},
        )

        recycled = store.get_recycled_routes()
        hidden_route = next((r for r in recycled if r["_local_id"] == "00000001--hidden00"), None)
        assert hidden_route is not None
        assert hidden_route["recycled_reason"] == "deleted"
        assert hidden_route["hidden_at"] == pytest.approx(hide_time, abs=1)
