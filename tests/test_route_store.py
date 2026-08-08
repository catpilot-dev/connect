"""Tests for route_store.py — pure functions and RouteStore business logic."""

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from route_store import RouteStore, _route_counter, CACHE_TTL


# ─── _route_counter ──────────────────────────────────────────────────

class TestRouteCounter:
    def test_normal_hex(self):
        # Openpilot uses hex counters: 0x114 = 276
        assert _route_counter("00000114--abc") == 0x114

    def test_leading_zeros(self):
        assert _route_counter("00000001--xyz") == 1

    def test_empty_string(self):
        assert _route_counter("") == 0

    def test_no_separator(self):
        assert _route_counter("noseparator") == 0

    def test_hex_chars(self):
        # 0x0000001d = 29
        assert _route_counter("0000001d--abc") == 0x1d

    def test_large_hex(self):
        assert _route_counter("0000ffff--abc") == 0xffff


# ─── _wall_time_to_route_date ────────────────────────────────────────

class TestWallTimeToRouteDate:
    def _make_store(self, tmp_path):
        with patch.object(RouteStore, "_detect_dongle_id"), \
             patch.object(RouteStore, "_detect_agnos_version"):
            return RouteStore(str(tmp_path))

    def test_utc_conversion(self, tmp_path):
        store = self._make_store(tmp_path)
        # 2025-06-15 12:30:45 UTC
        dt = datetime(2025, 6, 15, 12, 30, 45, tzinfo=timezone.utc)
        nanos = int(dt.timestamp() * 1e9)
        result = store._wall_time_to_route_date(nanos)
        assert result == "2025-06-15--12-30-45"

    def test_with_longitude_utc_plus_8(self, tmp_path):
        store = self._make_store(tmp_path)
        # 2025-06-15 20:00:00 UTC -> 2025-06-16 04:00:00 UTC+8
        dt = datetime(2025, 6, 15, 20, 0, 0, tzinfo=timezone.utc)
        nanos = int(dt.timestamp() * 1e9)
        result = store._wall_time_to_route_date(nanos, lng=121.0)
        assert result == "2025-06-16--04-00-00"


# ─── _meta_to_internal ───────────────────────────────────────────────

class TestMetaToInternal:
    def _make_store(self, tmp_path, metadata=None):
        with patch.object(RouteStore, "_detect_dongle_id"), \
             patch.object(RouteStore, "_detect_agnos_version"):
            store = RouteStore(str(tmp_path))
            if metadata:
                store._metadata = metadata
            return store

    def test_full_metadata(self, tmp_path):
        meta = {
            "test--route": {
                "dongle_id": "abc123",
                "creation_time": "2025-11-15T10:30:00+00:00",
                "gps_coordinates": [31.23, 121.47],
                "git_commit": "deadbeef",
                "git_branch": "main",
                "openpilot_version": "0.9.8",
                "car_fingerprint": "BMW_E90",
                "git_remote": "https://github.com/test/op.git",
                "device_type": "tici",
            }
        }
        store = self._make_store(tmp_path, meta)
        result = store._meta_to_internal("test--route")
        assert result["dongle_id"] == "abc123"
        assert result["start_lat"] == 31.23
        assert result["start_lng"] == 121.47
        assert result["car_fingerprint"] == "BMW_E90"
        assert result["git_commit"] == "deadbeef"
        assert result["device_type"] == "tici"
        assert "create_time" in result
        assert "wall_time_nanos" in result

    def test_missing_route(self, tmp_path):
        store = self._make_store(tmp_path)
        result = store._meta_to_internal("nonexistent--route")
        assert result == {}

    def test_unknown_values_excluded(self, tmp_path):
        meta = {
            "test--route": {
                "dongle_id": "Unknown",
                "git_commit": "Unknown",
                "openpilot_version": "Unknown",
            }
        }
        store = self._make_store(tmp_path, meta)
        result = store._meta_to_internal("test--route")
        assert "dongle_id" not in result
        assert "git_commit" not in result
        assert "version" not in result


# ─── _needs_enrich ───────────────────────────────────────────────────

class TestNeedsEnrich:
    def _make_store(self, tmp_path, metadata=None):
        with patch.object(RouteStore, "_detect_dongle_id"), \
             patch.object(RouteStore, "_detect_agnos_version"):
            store = RouteStore(str(tmp_path))
            if metadata:
                store._metadata = metadata
            return store

    def test_missing_metadata(self, tmp_path):
        store = self._make_store(tmp_path)
        assert store._needs_enrich("unknown--route") is True

    def test_missing_car_fingerprint(self, tmp_path):
        store = self._make_store(tmp_path, {"r": {"gps_coordinates": [1, 2], "total_distance_m": 100, "device_type": "tici"}})
        assert store._needs_enrich("r") is True

    def test_agnos_build_date(self, tmp_path):
        store = self._make_store(tmp_path, {
            "r": {
                "car_fingerprint": "BMW_E90",
                "device_type": "tici",
                "gps_coordinates": [1, 2],
                "total_distance_m": 100,
                "creation_time": "2025-07-02T12:00:00+00:00",
            }
        })
        assert store._needs_enrich("r") is True

    def test_fully_enriched(self, tmp_path):
        store = self._make_store(tmp_path, {
            "r": {
                "car_fingerprint": "BMW_E90",
                "device_type": "tici",
                "gps_coordinates": [31.23, 121.47],
                "total_distance_m": 5000.0,
                "creation_time": "2025-11-15T10:30:00+08:00",
                "enriched": True,
                "gps_time": 1731649800.0,
            }
        })
        assert store._needs_enrich("r") is False


# ─── scan ────────────────────────────────────────────────────────────

class TestScan:
    def test_finds_routes(self, mock_store):
        routes = mock_store.scan()
        # Should find 2 routes (42 and 100)
        assert len(routes) >= 1
        # Check _raw has the local_ids
        assert "00000042--abc123" in mock_store._raw
        assert "00000100--def456" in mock_store._raw

    def test_ignores_non_route_dirs(self, populated_data_dir):
        # Create non-route directory
        (populated_data_dir / "random_dir").mkdir()
        (populated_data_dir / "notaroute").mkdir()
        with patch.object(RouteStore, "_detect_dongle_id"), \
             patch.object(RouteStore, "_detect_agnos_version"):
            store = RouteStore(str(populated_data_dir))
            store._dongle_id = "test123"
            store.scan(force=True)
        assert "random_dir" not in store._raw
        assert "notaroute" not in store._raw

    def test_cache_hit_within_ttl(self, mock_store):
        mock_store.scan(force=True)
        first_scan_time = mock_store._last_scan
        # Scan again without force — should use cache
        mock_store.scan()
        assert mock_store._last_scan == first_scan_time

    def test_force_bypasses_cache(self, mock_store):
        mock_store.scan(force=True)
        first_scan_time = mock_store._last_scan
        import time
        time.sleep(0.01)
        mock_store.scan(force=True)
        assert mock_store._last_scan > first_scan_time


# ─── _build_route ────────────────────────────────────────────────────

class TestBuildRoute:
    def test_full_build(self, mock_store):
        routes = mock_store.scan()
        # Find the route with counter 100
        route_100 = None
        for r in routes.values():
            if r["_local_id"] == "00000100--def456":
                route_100 = r
                break
        assert route_100 is not None
        assert route_100["dongle_id"] == "test123"
        assert route_100["platform"] == "BMW_E90"
        assert route_100["_local_id"] == "00000100--def456"
        assert route_100["maxqlog"] >= 0
        assert "fullname" in route_100
        assert "start_time" in route_100
        assert "end_time" in route_100

    def test_counter_fallback_no_create_time(self, populated_data_dir):
        """Without create_time in metadata, fall back to counter."""
        meta_path = populated_data_dir / ".route_metadata.json"
        data = json.loads(meta_path.read_text())
        # Remove creation_time and gps_time from a route so counter is used
        del data["routes"]["00000100--def456"]["creation_time"]
        data["routes"]["00000100--def456"].pop("gps_time", None)
        meta_path.write_text(json.dumps(data))

        with patch.object(RouteStore, "_detect_dongle_id"), \
             patch.object(RouteStore, "_detect_agnos_version"):
            store = RouteStore(str(populated_data_dir))
            store._dongle_id = "test123"
            store.scan(force=True)

        for r in store._routes.values():
            if r["_local_id"] == "00000100--def456":
                # create_time should be the counter (0x100 = 256)
                assert r["create_time"] == 0x100
                break

    def test_stub_filtering(self, tmp_path):
        """Routes with maxqlog < 1 and no distance should be filtered out."""
        # Create a single-segment route with no distance data
        d = tmp_path / "00000001--stub--0"
        d.mkdir()
        (d / "rlog.zst").write_bytes(b"")
        # No metadata, no distance
        with patch.object(RouteStore, "_detect_dongle_id"), \
             patch.object(RouteStore, "_detect_agnos_version"):
            store = RouteStore(str(tmp_path))
            store._dongle_id = "test123"
            store.scan(force=True)
        # Single segment = maxqlog 0, no distance -> should be filtered
        for r in store._routes.values():
            assert r["_local_id"] != "00000001--stub"


# ─── Route operations ────────────────────────────────────────────────

class TestRouteOperations:
    def test_hide_route(self, mock_store):
        lid = "00000042--abc123"
        mock_store.preserve_route(lid)
        assert mock_store.is_preserved(lid) is True

        mock_store.hide_route(lid)
        assert lid in mock_store._hidden
        # Should be removed from routes
        for r in mock_store._routes.values():
            assert r["_local_id"] != lid
        # Should be removed from preserved
        assert lid not in mock_store._preserved

    def test_preserve_unpreserve(self, mock_store):
        lid = "00000042--abc123"
        mock_store.preserve_route(lid)
        assert mock_store.is_preserved(lid) is True
        mock_store.unpreserve_route(lid)
        assert mock_store.is_preserved(lid) is False

    def test_is_preserved_default_false(self, mock_store):
        assert mock_store.is_preserved("nonexistent") is False

    def test_resolve_segment_path_exists(self, mock_store):
        routes = mock_store.scan()
        # Get a route's fullname
        for fullname, r in routes.items():
            if r["_local_id"] == "00000042--abc123":
                path = mock_store.resolve_segment_path(fullname, 0, "rlog.zst")
                assert path is not None
                assert path.exists()
                break

    def test_resolve_segment_path_missing(self, mock_store):
        routes = mock_store.scan()
        for fullname, r in routes.items():
            if r["_local_id"] == "00000042--abc123":
                path = mock_store.resolve_segment_path(fullname, 99, "rlog.zst")
                assert path is None
                break

    def test_get_route_and_get_local_id(self, mock_store):
        routes = mock_store.scan()
        for fullname, r in routes.items():
            lid = mock_store.get_local_id(fullname)
            assert lid == r["_local_id"]
            route_back = mock_store.get_route(fullname)
            assert route_back is not None
            assert route_back["fullname"] == fullname
            break


# ─── Metadata persistence ────────────────────────────────────────────

class TestMetadata:
    def test_save_load_roundtrip(self, mock_store):
        mock_store.preserve_route("00000042--abc123")
        mock_store._save_metadata()

        # Reload
        with patch.object(RouteStore, "_detect_dongle_id"), \
             patch.object(RouteStore, "_detect_agnos_version"):
            store2 = RouteStore(str(mock_store.data_dir))
        assert "00000042--abc123" in store2._preserved
        assert "00000042--abc123" in store2._metadata

    def test_log_to_metadata_entry(self, mock_store):
        rlog_meta = {
            "dongle_id": "test123",
            "wall_time_nanos": 1700000000000000000,
            "start_lat": 31.23,
            "start_lng": 121.47,
            "git_commit": "abc123",
            "version": "0.9.8",
            "car_fingerprint": "BMW_E90",
            "device_type": "tici",
            "total_distance_m": 5000.0,
        }
        entry = mock_store._log_to_metadata_entry("00000042--abc123", rlog_meta)
        assert entry["route_id"] == "00000042--abc123"
        assert entry["dongle_id"] == "test123"
        assert entry["car_fingerprint"] == "BMW_E90"
        assert entry["gps_coordinates"] == [31.23, 121.47]
        assert entry["creation_time"] is not None
        assert entry["source"] == "connect_server"


# ─── Distance calculation ────────────────────────────────────────────

class TestDistance:
    def test_from_coords_json(self, mock_store):
        # Create a coords.json in a segment directory
        routes = mock_store.scan()
        for r in routes.values():
            if r["_local_id"] == "00000042--abc123":
                seg_path = Path(r["_segments"][0]["path"])
                coords = [{"t": 0, "lat": 31.0, "lng": 121.0, "speed": 10, "dist": 0},
                          {"t": 60, "lat": 31.01, "lng": 121.01, "speed": 10, "dist": 1500.0}]
                (seg_path / "coords.json").write_text(json.dumps(coords))
                # Force rebuild
                mock_store.scan(force=True)
                break
        # Check that route now has distance from coords
        for r in mock_store._routes.values():
            if r["_local_id"] == "00000042--abc123":
                assert r["distance_m"] is not None
                assert r["distance_m"] > 0
                break

    def test_fallback_to_metadata_distance(self, mock_store):
        # Routes should have distance from metadata total_distance_m
        routes = mock_store.scan()
        for r in routes.values():
            if r["_local_id"] == "00000100--def456":
                # total_distance_m=12300 m
                assert r["distance_m"] is not None
                assert r["distance_m"] > 0
                break


# ─── Notes ──────────────────────────────────────────────────────────

class TestNotes:
    def test_set_note(self, mock_store):
        lid = "00000042--abc123"
        mock_store.set_note(lid, "Test drive on highway")
        assert mock_store._metadata[lid]["notes"] == "Test drive on highway"

    def test_update_note(self, mock_store):
        lid = "00000042--abc123"
        mock_store.set_note(lid, "first")
        mock_store.set_note(lid, "updated")
        assert mock_store._metadata[lid]["notes"] == "updated"

    def test_set_note_creates_metadata(self, mock_store):
        # Use a route with no metadata entry
        lid = "nonexistent--route"
        mock_store.set_note(lid, "note for new route")
        assert lid in mock_store._metadata
        assert mock_store._metadata[lid]["notes"] == "note for new route"

    def test_empty_note(self, mock_store):
        lid = "00000042--abc123"
        mock_store.set_note(lid, "")
        assert mock_store._metadata[lid]["notes"] == ""

    def test_note_appears_in_route(self, mock_store):
        lid = "00000042--abc123"
        mock_store.set_note(lid, "highway test")
        routes = mock_store.scan(force=True)
        for r in routes.values():
            if r["_local_id"] == lid:
                assert r.get("notes") == "highway test"
                break


# ─── Bookmarks ──────────────────────────────────────────────────────

class TestBookmarks:
    def test_add_bookmark(self, mock_store):
        lid = "00000042--abc123"
        result = mock_store.add_bookmark(lid, 30.5, "Good merge")
        assert len(result) == 1
        assert result[0]["time_sec"] == 30.5
        assert result[0]["label"] == "Good merge"

    def test_add_multiple_sorted(self, mock_store):
        lid = "00000042--abc123"
        mock_store.add_bookmark(lid, 60.0, "Second")
        result = mock_store.add_bookmark(lid, 10.0, "First")
        assert len(result) == 2
        assert result[0]["time_sec"] == 10.0
        assert result[1]["time_sec"] == 60.0

    def test_add_bookmark_creates_metadata(self, mock_store):
        lid = "new--route"
        result = mock_store.add_bookmark(lid, 5.0, "test")
        assert lid in mock_store._metadata
        assert len(result) == 1

    def test_update_bookmark(self, mock_store):
        lid = "00000042--abc123"
        mock_store.add_bookmark(lid, 10.0, "Original")
        result = mock_store.update_bookmark(lid, 0, "Updated")
        assert result[0]["label"] == "Updated"
        assert result[0]["time_sec"] == 10.0

    def test_update_bookmark_nonexistent_route(self, mock_store):
        result = mock_store.update_bookmark("nonexistent", 0, "label")
        assert result == []

    def test_update_bookmark_out_of_range(self, mock_store):
        lid = "00000042--abc123"
        mock_store.add_bookmark(lid, 10.0, "Only one")
        result = mock_store.update_bookmark(lid, 99, "nope")
        assert len(result) == 1
        assert result[0]["label"] == "Only one"

    def test_delete_bookmark(self, mock_store):
        lid = "00000042--abc123"
        mock_store.add_bookmark(lid, 10.0, "First")
        mock_store.add_bookmark(lid, 20.0, "Second")
        result = mock_store.delete_bookmark(lid, 0)
        assert len(result) == 1
        assert result[0]["label"] == "Second"

    def test_delete_bookmark_nonexistent_route(self, mock_store):
        result = mock_store.delete_bookmark("nonexistent", 0)
        assert result == []

    def test_delete_bookmark_out_of_range(self, mock_store):
        lid = "00000042--abc123"
        mock_store.add_bookmark(lid, 10.0, "Only")
        result = mock_store.delete_bookmark(lid, 5)
        assert len(result) == 1


# ─── get_recycled_routes ────────────────────────────────────────────

class TestRecycledRoutes:
    def test_hidden_routes_in_recycled(self, mock_store):
        lid = "00000042--abc123"
        mock_store.hide_route(lid)
        recycled = mock_store.get_recycled_routes()
        hidden = [r for r in recycled if r["_local_id"] == lid]
        assert len(hidden) == 1
        assert hidden[0]["recycled_reason"] == "deleted"

    def test_hidden_at_timestamp(self, mock_store):
        lid = "00000042--abc123"
        mock_store.hide_route(lid)
        recycled = mock_store.get_recycled_routes()
        hidden = [r for r in recycled if r["_local_id"] == lid]
        assert "hidden_at" in hidden[0]
        assert isinstance(hidden[0]["hidden_at"], float)

    def test_empty_when_no_hidden(self, mock_store):
        recycled = mock_store.get_recycled_routes()
        # May have some "invalid" routes depending on metadata
        hidden = [r for r in recycled if r["recycled_reason"] == "deleted"]
        assert len(hidden) == 0

    def test_sorted_newest_first(self, mock_store):
        mock_store.hide_route("00000042--abc123")
        mock_store.hide_route("00000100--def456")
        recycled = mock_store.get_recycled_routes()
        counters = [_route_counter(r["_local_id"]) for r in recycled]
        assert counters == sorted(counters, reverse=True)


# ─── clear_derived ──────────────────────────────────────────────────

class TestClearDerived:
    def test_clears_events_and_coords(self, mock_store):
        routes = mock_store.scan()
        for r in routes.values():
            if r["_local_id"] == "00000042--abc123" and r["_segments"]:
                seg_path = Path(r["_segments"][0]["path"])
                (seg_path / "events.json").write_text("[]")
                (seg_path / "coords.json").write_text("[]")
                deleted = mock_store.clear_derived("00000042--abc123")
                assert deleted >= 2
                assert not (seg_path / "events.json").exists()
                assert not (seg_path / "coords.json").exists()
                break

    def test_returns_zero_for_unknown_route(self, mock_store):
        assert mock_store.clear_derived("nonexistent--route") == 0

    def test_returns_zero_when_no_derived(self, mock_store):
        deleted = mock_store.clear_derived("00000042--abc123")
        assert deleted == 0


# ─── get_pending_route_ids ──────────────────────────────────────────

class TestGetPendingRouteIds:
    def test_no_pending_when_all_enriched(self, mock_store):
        mock_store.scan()
        pending = mock_store.get_pending_route_ids()
        # All routes in mock_store have metadata, so no pending
        assert len(pending) == 0

    def test_pending_route_without_metadata(self, populated_data_dir):
        # Create a route on disk with no metadata entry
        for seg in range(3):
            d = populated_data_dir / f"00000200--newroute--{seg}"
            d.mkdir()
            (d / "rlog.zst").write_bytes(b"")

        with patch.object(RouteStore, "_detect_dongle_id"), \
             patch.object(RouteStore, "_detect_agnos_version"):
            store = RouteStore(str(populated_data_dir))
            store._dongle_id = "test123"
            store.scan(force=True)

        pending = store.get_pending_route_ids()
        assert any(p["local_id"] == "00000200--newroute" for p in pending)

    def test_pending_skips_hidden(self, populated_data_dir):
        for seg in range(3):
            d = populated_data_dir / f"00000200--newroute--{seg}"
            d.mkdir()
            (d / "rlog.zst").write_bytes(b"")

        # Add to hidden
        meta_path = populated_data_dir / ".route_metadata.json"
        import json
        data = json.loads(meta_path.read_text())
        data["hidden_routes"] = {"00000200--newroute": time.time()}
        meta_path.write_text(json.dumps(data))

        with patch.object(RouteStore, "_detect_dongle_id"), \
             patch.object(RouteStore, "_detect_agnos_version"):
            store = RouteStore(str(populated_data_dir))
            store._dongle_id = "test123"
            store.scan(force=True)

        pending = store.get_pending_route_ids()
        assert not any(p["local_id"] == "00000200--newroute" for p in pending)

    def test_pending_skips_single_segment(self, populated_data_dir):
        # Single-segment route should be filtered as stub
        d = populated_data_dir / "00000300--single00--0"
        d.mkdir()
        (d / "rlog.zst").write_bytes(b"")

        with patch.object(RouteStore, "_detect_dongle_id"), \
             patch.object(RouteStore, "_detect_agnos_version"):
            store = RouteStore(str(populated_data_dir))
            store._dongle_id = "test123"
            store.scan(force=True)

        pending = store.get_pending_route_ids()
        assert not any(p["local_id"] == "00000300--single00" for p in pending)

    def test_pending_sorted_newest_first(self, populated_data_dir):
        for lid in ("00000200--aaa00000", "00000300--bbb00000"):
            for seg in range(3):
                d = populated_data_dir / f"{lid}--{seg}"
                d.mkdir()
                (d / "rlog.zst").write_bytes(b"")

        with patch.object(RouteStore, "_detect_dongle_id"), \
             patch.object(RouteStore, "_detect_agnos_version"):
            store = RouteStore(str(populated_data_dir))
            store._dongle_id = "test123"
            store.scan(force=True)

        pending = store.get_pending_route_ids()
        counters = [p["counter"] for p in pending]
        assert counters == sorted(counters, reverse=True)


# ─── _find_rlog / _find_qlog ───────────────────────────────────────

class TestFindLogFiles:
    def test_find_rlog_zst(self, tmp_path):
        seg = tmp_path / "seg"
        seg.mkdir()
        (seg / "rlog.zst").write_bytes(b"data")
        assert RouteStore._find_rlog(str(seg)) == str(seg / "rlog.zst")

    def test_find_rlog_uncompressed(self, tmp_path):
        seg = tmp_path / "seg"
        seg.mkdir()
        (seg / "rlog").write_bytes(b"data")
        assert RouteStore._find_rlog(str(seg)) == str(seg / "rlog")

    def test_find_rlog_prefers_zst(self, tmp_path):
        seg = tmp_path / "seg"
        seg.mkdir()
        (seg / "rlog.zst").write_bytes(b"zst")
        (seg / "rlog").write_bytes(b"raw")
        assert RouteStore._find_rlog(str(seg)) == str(seg / "rlog.zst")

    def test_find_rlog_none(self, tmp_path):
        seg = tmp_path / "seg"
        seg.mkdir()
        assert RouteStore._find_rlog(str(seg)) is None

    def test_find_qlog_zst(self, tmp_path):
        seg = tmp_path / "seg"
        seg.mkdir()
        (seg / "qlog.zst").write_bytes(b"data")
        assert RouteStore._find_qlog(str(seg)) == str(seg / "qlog.zst")

    def test_find_qlog_uncompressed(self, tmp_path):
        seg = tmp_path / "seg"
        seg.mkdir()
        (seg / "qlog").write_bytes(b"data")
        assert RouteStore._find_qlog(str(seg)) == str(seg / "qlog")

    def test_find_qlog_none(self, tmp_path):
        seg = tmp_path / "seg"
        seg.mkdir()
        assert RouteStore._find_qlog(str(seg)) is None


# ─── _log_to_metadata_entry / _log_to_metadata_entry ───────────────

class TestLogToMetadataEntry:
    def _make_store(self, tmp_path):
        with patch.object(RouteStore, "_detect_dongle_id"), \
             patch.object(RouteStore, "_detect_agnos_version"):
            return RouteStore(str(tmp_path))

    def test_full_entry(self, tmp_path):
        store = self._make_store(tmp_path)
        log_meta = {
            "dongle_id": "test123",
            "wall_time_nanos": 1700000000000000000,
            "start_lat": 31.23,
            "start_lng": 121.47,
            "git_commit": "abc123",
            "git_branch": "main",
            "version": "0.9.8",
            "car_fingerprint": "BMW_E90",
            "device_type": "tici",
            "gps_time": 1700000100.0,
            "total_distance_m": 5000.0,
        }
        entry = store._log_to_metadata_entry("test--route", log_meta)
        assert entry["route_id"] == "test--route"
        assert entry["dongle_id"] == "test123"
        assert entry["car_fingerprint"] == "BMW_E90"
        assert entry["gps_coordinates"] == [31.23, 121.47]
        assert entry["enriched"] is True
        assert entry["gps_time"] == 1700000100.0

    def test_missing_gps(self, tmp_path):
        store = self._make_store(tmp_path)
        log_meta = {"dongle_id": "test"}
        entry = store._log_to_metadata_entry("test--route", log_meta)
        assert entry["gps_coordinates"] is None

    def test_no_wall_time(self, tmp_path):
        store = self._make_store(tmp_path)
        log_meta = {"dongle_id": "test"}
        entry = store._log_to_metadata_entry("test--route", log_meta)
        assert entry["creation_time"] is None


# ─── RouteStore._compute_stats ───────────────────────────────────────

class TestComputeStats:
    def _store_with_routes(self, tmp_path, routes):
        store = RouteStore(str(tmp_path))
        store._routes = routes
        return store

    def _route(self, create_time, distance_m, segments, drive_stats=None):
        r = {"create_time": create_time, "distance_m": distance_m,
             "_segments": [{"number": n, "path": ""} for n in range(segments)]}
        if drive_stats is not None:
            r["drive_stats"] = drive_stats
        return r

    def test_week_independent_of_wall_clock(self, tmp_path):
        # newest route is at t=1_000_000; one route 10 days earlier, one 1 day earlier
        newest = 1_000_000.0
        day = 86400.0
        routes = {
            "a": self._route(newest, 10_000, 10,
                             {"distance_m": 10_000, "duration_s": 600, "engaged_m": 8_000, "engaged_s": 480}),
            "b": self._route(newest - 1 * day, 5_000, 5,
                             {"distance_m": 5_000, "duration_s": 300, "engaged_m": 4_000, "engaged_s": 240}),
            "c": self._route(newest - 10 * day, 20_000, 20,
                             {"distance_m": 20_000, "duration_s": 1200, "engaged_m": 10_000, "engaged_s": 600}),
        }
        store = self._store_with_routes(tmp_path, routes)
        # Stale wall clock (build time, years off) must not affect the result.
        with patch("route_store.time.time", return_value=0.0):
            s = store._compute_stats()
        # week = routes within 7 days of newest GPS time -> a and b only
        assert s["week"]["routes"] == 2
        assert s["week"]["distance_m"] == 15_000
        assert s["all"]["routes"] == 3
        # week != all — the regression guard
        assert s["week"]["routes"] != s["all"]["routes"]

    def test_distance_based_engaged_pct(self, tmp_path):
        newest = 1_000_000.0
        routes = {
            "a": self._route(newest, 10_000, 10,
                             {"distance_m": 10_000, "duration_s": 600, "engaged_m": 8_000, "engaged_s": 480}),
        }
        store = self._store_with_routes(tmp_path, routes)
        s = store._compute_stats()
        # engaged_m / total_m_with_engagement = 8000/10000
        assert s["all"]["engaged_m"] == 8_000
        assert s["all"]["total_m_with_engagement"] == 10_000

    def test_route_without_drive_stats_excluded_from_ratio(self, tmp_path):
        newest = 1_000_000.0
        routes = {
            "a": self._route(newest, 10_000, 10,
                             {"distance_m": 10_000, "duration_s": 600, "engaged_m": 8_000, "engaged_s": 480}),
            "b": self._route(newest, 9_000, 9, None),  # no brief record
        }
        store = self._store_with_routes(tmp_path, routes)
        s = store._compute_stats()
        assert s["all"]["routes"] == 2               # counted for routes/distance
        assert s["all"]["distance_m"] == 19_000
        assert s["all"]["total_m_with_engagement"] == 10_000  # only 'a' in ratio

    def test_empty_store_no_divide_by_zero(self, tmp_path):
        store = self._store_with_routes(tmp_path, {})
        s = store._compute_stats()
        assert s["all"]["routes"] == 0
        assert s["all"]["total_m_with_engagement"] == 0
        assert s["reference_time"] == 0

    def test_rebuild_populates_stats_from_metadata(self, tmp_path):
        store = RouteStore(str(tmp_path))
        # Minimal raw + metadata for one enriched route with brief stats.
        lid = "00000001--aaaa"
        store._raw = {lid: {"segments": [{"number": 0, "path": str(tmp_path)},
                                         {"number": 1, "path": str(tmp_path)}]}}
        store._metadata = {lid: {
            "route_id": lid, "dongle_id": "d", "gps_time": 1_000_000.0,
            "gps_coordinates": [1.0, 2.0], "total_distance_m": 10_000,
            "drive_stats": {"distance_m": 10_000, "duration_s": 600,
                            "engaged_m": 8_000, "engaged_s": 480},
        }}
        store._rebuild_routes()
        assert store._stats["all"]["routes"] == 1
        assert store._stats["all"]["engaged_m"] == 8_000
        assert store._stats["reference_time"] == 1_000_000.0


class TestSetDriveStats:
    def test_persists_and_recomputes(self, tmp_path):
        store = RouteStore(str(tmp_path))
        lid = "00000001--aaaa"
        store._raw = {lid: {"segments": [{"number": 0, "path": str(tmp_path)},
                                         {"number": 1, "path": str(tmp_path)}]}}
        store._metadata = {lid: {"route_id": lid, "dongle_id": "d",
                                 "gps_time": 1_000_000.0, "gps_coordinates": [1.0, 2.0],
                                 "total_distance_m": 10_000}}
        store.set_drive_stats(lid, {"distance_m": 10_000, "duration_s": 600,
                                    "engaged_m": 8_000, "engaged_s": 480})
        assert store._metadata[lid]["drive_stats"]["engaged_m"] == 8_000
        assert store._stats["all"]["engaged_m"] == 8_000
        # persisted to disk
        on_disk = json.loads((tmp_path / ".route_metadata.json").read_text())
        assert on_disk["routes"][lid]["drive_stats"]["distance_m"] == 10_000


class TestDriveStatsEndpoint:
    async def _call(self, handler, store, route_name, body):
        from unittest.mock import MagicMock
        req = MagicMock()
        req.app = {"store": store}
        req.match_info = {"routeName": route_name}
        async def _json():
            return body
        req.json = _json
        return await handler(req)

    def test_post_updates_stats_then_get_serves_cache(self, tmp_path):
        import asyncio
        from handlers.routes import handle_route_drive_stats
        from handlers.auth import handle_device_stats
        store = RouteStore(str(tmp_path))
        lid = "00000001--aaaa"
        store._raw = {lid: {"segments": [{"number": 0, "path": str(tmp_path)},
                                         {"number": 1, "path": str(tmp_path)}]}}
        store._metadata = {lid: {"route_id": lid, "dongle_id": "d",
                                 "gps_time": 1_000_000.0, "gps_coordinates": [1.0, 2.0],
                                 "total_distance_m": 10_000}}
        store._rebuild_routes()
        # mock get_local_id so _resolve_local_id finds the route
        store.get_local_id = lambda name: lid if name == lid else None

        resp = asyncio.run(self._call(handle_route_drive_stats, store, lid,
            {"distance_m": 10_000, "duration_s": 600, "engaged_m": 8_000, "engaged_s": 480}))
        assert resp.status == 200

        # GET serves the cached aggregate with NO wall clock
        get_req_store = store
        import handlers.auth as auth_mod
        from unittest.mock import MagicMock
        greq = MagicMock()
        greq.app = {"store": store}
        greq.match_info = {"dongleId": "d"}
        with patch("handlers.auth.time.time", return_value=0.0):
            gresp = asyncio.run(handle_device_stats(greq))
        payload = json.loads(gresp.body.decode())
        assert payload["all"]["engaged_m"] == 8_000
        assert payload["week"]["engaged_m"] == 8_000


class TestHybridGpsTime:
    def test_set_drive_stats_with_gps_time_materializes(self, tmp_path):
        store = RouteStore(str(tmp_path))
        lid = "00000001--aaaa"
        store._raw = {lid: {"segments": [{"number": 0, "path": str(tmp_path)},
                                         {"number": 1, "path": str(tmp_path)}]}}
        store._metadata = {}
        # No prior gps_time anywhere; device supplies it in the POST.
        store.set_drive_stats(lid, {"distance_m": 10_000, "duration_s": 600,
                                    "engaged_m": 8_000, "engaged_s": 480},
                              gps_time=1_000_000.0)
        assert store._metadata[lid]["gps_time"] == 1_000_000.0
        assert store._stats["all"]["routes"] == 1
        assert store._stats["all"]["engaged_m"] == 8_000
        assert store._stats["reference_time"] == 1_000_000.0

    def test_set_drive_stats_without_gps_time_stores_record(self, tmp_path):
        store = RouteStore(str(tmp_path))
        lid = "00000001--aaaa"
        store._raw = {lid: {"segments": [{"number": 0, "path": str(tmp_path)}]}}
        store._metadata = {}
        store.set_drive_stats(lid, {"distance_m": 1, "duration_s": 1,
                                    "engaged_m": 1, "engaged_s": 1})
        # Record persisted; route not yet in aggregate (no gps_time -> filtered).
        assert store._metadata[lid]["drive_stats"]["distance_m"] == 1.0
        assert "gps_time" not in store._metadata[lid]

    def test_enrich_fallback_fills_gps_and_preserves_drive_stats(self, tmp_path, monkeypatch):
        store = RouteStore(str(tmp_path))
        lid = "00000001--aaaa"
        store._raw = {lid: {"segments": [{"number": 0, "path": str(tmp_path)},
                                         {"number": 1, "path": str(tmp_path)}]}}
        # Bare drive_stats entry (device had no GPS lock -> no gps_time).
        store._metadata = {lid: {"route_id": lid,
                                 "drive_stats": {"distance_m": 5_000, "duration_s": 300,
                                                 "engaged_m": 4_000, "engaged_s": 240}}}
        # Fallback parse yields GPS time from the qlog.
        monkeypatch.setattr("route_store.RouteStore._find_qlog",
                            staticmethod(lambda p: "fake.zst"))
        monkeypatch.setattr("route_store._parse_log_metadata",
                            lambda p: {"gps_time": 1_000_000.0, "start_lat": 1.0,
                                       "start_lng": 2.0, "dongle_id": "d"})
        store.enrich_new_routes()
        assert store._metadata[lid]["gps_time"] == 1_000_000.0
        assert store._metadata[lid]["drive_stats"]["engaged_m"] == 4_000  # preserved
        assert store._stats["all"]["routes"] == 1

    def test_enrich_fallback_marks_attempted_when_no_gps(self, tmp_path, monkeypatch):
        store = RouteStore(str(tmp_path))
        lid = "00000001--aaaa"
        store._raw = {lid: {"segments": [{"number": 0, "path": str(tmp_path)},
                                         {"number": 1, "path": str(tmp_path)}]}}
        store._metadata = {lid: {"route_id": lid,
                                 "drive_stats": {"distance_m": 5_000, "duration_s": 300,
                                                 "engaged_m": 4_000, "engaged_s": 240}}}
        monkeypatch.setattr("route_store.RouteStore._find_qlog",
                            staticmethod(lambda p: "fake.zst"))
        # Parse finds coords but no gps_time (never got a fix).
        monkeypatch.setattr("route_store._parse_log_metadata",
                            lambda p: {"start_lat": 1.0, "start_lng": 2.0, "dongle_id": "d"})
        monkeypatch.setattr("route_store._find_first_gps_time", lambda p: None)
        monkeypatch.setattr("route_store._find_first_gps", lambda p: (1.0, 2.0))
        assert store.enrich_new_routes() >= 0
        assert store._metadata[lid].get("gps_enrich_attempted") is True
        # Second pass must skip it (no churn).
        calls = []
        orig = __import__("route_store")._parse_log_metadata
        monkeypatch.setattr("route_store._parse_log_metadata",
                            lambda p: (calls.append(p), {"start_lat": 1.0})[1])
        store.enrich_new_routes()
        assert calls == []

    def test_ensure_enriched_preserves_drive_stats(self, tmp_path, monkeypatch):
        store = RouteStore(str(tmp_path))
        lid = "00000001--aaaa"
        store._raw = {lid: {"segments": [{"number": 0, "path": str(tmp_path)},
                                         {"number": 1, "path": str(tmp_path)}]}}
        # Bare drive_stats-only entry (posted by device, not yet GPS-enriched).
        store._metadata = {lid: {"route_id": lid,
                                 "drive_stats": {"distance_m": 5_000, "duration_s": 300,
                                                 "engaged_m": 4_000, "engaged_s": 240}}}
        monkeypatch.setattr("route_store.RouteStore._is_onroad",
                            staticmethod(lambda: False))
        monkeypatch.setattr("route_store.RouteStore._enrich_one",
                            lambda self, lid, segs: {"gps_time": 1_000_000.0,
                                                     "start_lat": 1.0, "start_lng": 2.0,
                                                     "dongle_id": "d"})
        assert store.ensure_enriched(lid) is True
        assert store._metadata[lid]["gps_time"] == 1_000_000.0
        assert store._metadata[lid]["drive_stats"]["engaged_m"] == 4_000  # preserved
        assert store._stats["all"]["total_m_with_engagement"] == 5_000
