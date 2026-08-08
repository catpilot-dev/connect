"""Tests for route_helpers.py — pure functions and mock request objects."""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from route_helpers import _base_url, _clean_route, _resolve_local_id, _route_bookmarks, _route_engaged_distance, _route_timeline_summary, _set_route_url


# ─── _clean_route ────────────────────────────────────────────────────

class TestCleanRoute:
    def test_removes_underscore_keys(self):
        route = {"fullname": "a/b", "distance": 1.0, "_local_id": "x", "_segments": []}
        cleaned = _clean_route(route)
        assert "fullname" in cleaned
        assert "distance" in cleaned
        assert "_local_id" not in cleaned
        assert "_segments" not in cleaned

    def test_preserves_public_keys(self):
        route = {"a": 1, "b": 2, "c": 3}
        assert _clean_route(route) == {"a": 1, "b": 2, "c": 3}


# ─── _base_url ───────────────────────────────────────────────────────

def _make_request(host="localhost:8082", scheme="http", forwarded_proto=None, forwarded_host=None):
    req = MagicMock()
    req.host = host
    req.scheme = scheme
    headers = {}
    if forwarded_proto:
        headers["X-Forwarded-Proto"] = forwarded_proto
    if forwarded_host:
        headers["X-Forwarded-Host"] = forwarded_host
    req.headers = headers
    return req


class TestBaseUrl:
    def test_default(self):
        req = _make_request()
        assert _base_url(req) == "http://localhost:8082"

    def test_with_forwarded_headers(self):
        req = _make_request(forwarded_proto="https", forwarded_host="connect.example.com")
        assert _base_url(req) == "https://connect.example.com"


# ─── _set_route_url ─────────────────────────────────────────────────

class TestSetRouteUrl:
    def test_sets_url(self):
        route = {"dongle_id": "abc", "fullname": "abc/2025-01-01--12-00-00", "url": None}
        req = _make_request()
        result = _set_route_url(route, req)
        assert result["url"] == "http://localhost:8082/connectdata/abc/2025-01-01--12-00-00"
        # Original dict should not be modified
        assert route["url"] is None

    def test_does_not_mutate_original(self):
        route = {"dongle_id": "x", "fullname": "x/date", "url": None, "extra": 1}
        req = _make_request()
        result = _set_route_url(route, req)
        assert result is not route
        assert result["extra"] == 1


# ─── _resolve_local_id ──────────────────────────────────────────────

class TestResolveLocalId:
    def test_found(self, mock_store):
        from aiohttp import web
        routes = mock_store.scan()
        for fullname in routes:
            req = MagicMock()
            req.match_info = {"routeName": fullname.replace("/", "|")}
            lid = _resolve_local_id(mock_store, req)
            assert lid is not None
            break

    def test_not_found(self, mock_store):
        from aiohttp import web
        req = MagicMock()
        req.match_info = {"routeName": "nonexistent|route"}
        with pytest.raises(web.HTTPNotFound):
            _resolve_local_id(mock_store, req)

    def test_pipe_replacement(self, mock_store):
        """routeName uses pipes in URL, converted to slashes."""
        routes = mock_store.scan()
        for fullname in routes:
            req = MagicMock()
            piped = fullname.replace("/", "|")
            req.match_info = {"routeName": piped}
            lid = _resolve_local_id(mock_store, req)
            assert lid is not None
            break


# ─── _route_engaged_distance ─────────────────────────────────────────

class TestRouteEngagedDistance:
    def _write_seg(self, seg_path: Path, coords: list, events: list):
        seg_path.mkdir(parents=True, exist_ok=True)
        (seg_path / "coords.json").write_text(json.dumps(coords))
        (seg_path / "events.json").write_text(json.dumps(events))

    def test_no_cache_returns_zero(self, mock_store, tmp_path):
        # Segment dir exists but no coords.json or events.json
        seg = tmp_path / "seg0"
        seg.mkdir()
        route = {"_segments": [{"path": str(seg), "number": 0}]}
        engaged_m, total_m = _route_engaged_distance(mock_store, route)
        assert engaged_m == 0.0
        assert total_m == 0.0

    def test_single_segment_engagement(self, mock_store, tmp_path, sample_coords_json):
        seg = tmp_path / "seg0"
        events = [
            {"type": "state", "route_offset_millis": 10_000, "data": {"enabled": True}},
            {"type": "state", "route_offset_millis": 50_000, "data": {"enabled": False}},
        ]
        self._write_seg(seg, sample_coords_json, events)
        route = {"_segments": [{"path": str(seg), "number": 0}]}
        engaged_m, total_m = _route_engaged_distance(mock_store, route)
        # interp(10_000) = 500 * (10/30) ≈ 166.67
        # interp(50_000) = 500 + 500 * (20/30) ≈ 833.33
        assert engaged_m == pytest.approx(666.67, abs=0.01)
        assert total_m == 1000.0

    def test_multi_segment_engagement(self, mock_store, tmp_path):
        seg0 = tmp_path / "seg0"
        seg1 = tmp_path / "seg1"
        coords0 = [
            {"t": 0.0,  "dist": 0.0},
            {"t": 60.0, "dist": 1000.0},
        ]
        coords1 = [
            {"t": 60.0,  "dist": 0.0},
            {"t": 120.0, "dist": 800.0},
        ]
        events0 = [
            {"type": "state", "route_offset_millis": 40_000, "data": {"enabled": True}},
        ]
        events1 = [
            {"type": "state", "route_offset_millis": 80_000, "data": {"enabled": False}},
        ]
        self._write_seg(seg0, coords0, events0)
        self._write_seg(seg1, coords1, events1)
        route = {"_segments": [
            {"path": str(seg0), "number": 0},
            {"path": str(seg1), "number": 1},
        ]}
        engaged_m, total_m = _route_engaged_distance(mock_store, route)
        # cum_m at t=40_000 ≈ 666.67 (seg0); cum_m at t=80_000 ≈ 1000+800*(20/60) ≈ 1266.67
        assert engaged_m == pytest.approx(600.0, abs=0.01)
        assert total_m == 1800.0

    def test_open_engagement_closes_at_route_end(self, mock_store, tmp_path):
        seg = tmp_path / "seg0"
        coords = [
            {"t": 0.0,  "dist": 0.0},
            {"t": 60.0, "dist": 1000.0},
        ]
        events = [
            {"type": "state", "route_offset_millis": 30_000, "data": {"enabled": True}},
            # No falling edge — closes at t_ms[-1]=60_000
        ]
        self._write_seg(seg, coords, events)
        route = {"_segments": [{"path": str(seg), "number": 0}]}
        engaged_m, total_m = _route_engaged_distance(mock_store, route)
        assert engaged_m == pytest.approx(500.0, abs=0.01)
        assert total_m == 1000.0

    def test_stationary_while_engaged_yields_zero(self, mock_store, tmp_path):
        """Idle/stationary time with system engaged contributes zero meters (distance, not time)."""
        seg = tmp_path / "seg0"
        coords = [
            {"t": 0.0,  "dist": 0.0},
            {"t": 20.0, "dist": 100.0},
            {"t": 40.0, "dist": 100.0},  # stationary
            {"t": 60.0, "dist": 200.0},
        ]
        events = [
            {"type": "state", "route_offset_millis": 25_000, "data": {"enabled": True}},
            {"type": "state", "route_offset_millis": 35_000, "data": {"enabled": False}},
        ]
        self._write_seg(seg, coords, events)
        route = {"_segments": [{"path": str(seg), "number": 0}]}
        engaged_m, total_m = _route_engaged_distance(mock_store, route)
        assert engaged_m == 0.0
        assert total_m == 200.0

    def test_no_segments_returns_zero(self, mock_store):
        route = {"_segments": []}
        engaged_m, total_m = _route_engaged_distance(mock_store, route)
        assert engaged_m == 0.0
        assert total_m == 0.0

    def test_missing_events_returns_zero(self, mock_store, tmp_path, sample_coords_json):
        """Coords cached but events.json missing -> bail."""
        seg = tmp_path / "seg0"
        seg.mkdir()
        (seg / "coords.json").write_text(json.dumps(sample_coords_json))
        route = {"_segments": [{"path": str(seg), "number": 0}]}
        engaged_m, total_m = _route_engaged_distance(mock_store, route)
        assert engaged_m == 0.0
        assert total_m == 0.0

    def test_corrupt_events_returns_zero(self, mock_store, tmp_path, sample_coords_json):
        """Corrupt events.json (unparseable) is treated the same as missing — (0, 0)."""
        seg = tmp_path / "seg0"
        seg.mkdir()
        (seg / "coords.json").write_text(json.dumps(sample_coords_json))
        (seg / "events.json").write_text("{ not valid json")
        route = {"_segments": [{"path": str(seg), "number": 0}]}
        engaged_m, total_m = _route_engaged_distance(mock_store, route)
        assert engaged_m == 0.0
        assert total_m == 0.0


# ─── _route_bookmarks ───────────────────────────────────────────────

class TestRouteBookmarks:
    def test_no_segments(self):
        route = {"_segments": []}
        assert _route_bookmarks(route) == []

    def test_no_events_file(self, tmp_path):
        seg_dir = tmp_path / "seg0"
        seg_dir.mkdir()
        route = {"_segments": [{"path": str(seg_dir), "number": 0}]}
        assert _route_bookmarks(route) == []

    def test_collects_user_flags(self, tmp_path):
        seg_dir = tmp_path / "seg0"
        seg_dir.mkdir()
        events = [
            {"type": "state", "route_offset_millis": 5000, "data": {"state": "enabled", "enabled": True}},
            {"type": "user_flag", "route_offset_millis": 15000},
            {"type": "user_flag", "route_offset_millis": 30000},
            {"type": "state", "route_offset_millis": 40000, "data": {"state": "disabled", "enabled": False}},
        ]
        (seg_dir / "events.json").write_text(json.dumps(events))
        route = {"_segments": [{"path": str(seg_dir), "number": 0}]}
        bookmarks = _route_bookmarks(route)
        assert bookmarks == [15000, 30000]

    def test_sorted_across_segments(self, tmp_path):
        seg0 = tmp_path / "seg0"
        seg1 = tmp_path / "seg1"
        seg0.mkdir()
        seg1.mkdir()
        (seg0 / "events.json").write_text(json.dumps([{"type": "user_flag", "route_offset_millis": 50000}]))
        (seg1 / "events.json").write_text(json.dumps([{"type": "user_flag", "route_offset_millis": 10000}]))
        route = {"_segments": [
            {"path": str(seg0), "number": 0},
            {"path": str(seg1), "number": 1},
        ]}
        bookmarks = _route_bookmarks(route)
        assert bookmarks == [10000, 50000]

    def test_ignores_non_user_flag(self, tmp_path):
        seg_dir = tmp_path / "seg0"
        seg_dir.mkdir()
        events = [
            {"type": "state", "route_offset_millis": 5000, "data": {"state": "enabled", "enabled": True}},
        ]
        (seg_dir / "events.json").write_text(json.dumps(events))
        route = {"_segments": [{"path": str(seg_dir), "number": 0}]}
        assert _route_bookmarks(route) == []

    def test_handles_corrupt_json(self, tmp_path):
        seg_dir = tmp_path / "seg0"
        seg_dir.mkdir()
        (seg_dir / "events.json").write_text("not json")
        route = {"_segments": [{"path": str(seg_dir), "number": 0}]}
        assert _route_bookmarks(route) == []


# ─── _route_timeline_summary ────────────────────────────────────────

class TestRouteTimelineSummary:
    def test_no_events_returns_none(self):
        route = {"_segments": [], "maxqlog": 0}
        assert _route_timeline_summary(route) is None

    def test_no_events_file_returns_none(self, tmp_path):
        seg_dir = tmp_path / "seg0"
        seg_dir.mkdir()
        route = {"_segments": [{"path": str(seg_dir), "number": 0}], "maxqlog": 0}
        assert _route_timeline_summary(route) is None

    def test_engage_disengage_span(self, tmp_path):
        seg_dir = tmp_path / "seg0"
        seg_dir.mkdir()
        events = [
            {"type": "state", "route_offset_millis": 5000, "data": {"state": "enabled", "enabled": True, "alertStatus": 0}},
            {"type": "state", "route_offset_millis": 25000, "data": {"state": "disabled", "enabled": False, "alertStatus": 0}},
        ]
        (seg_dir / "events.json").write_text(json.dumps(events))
        route = {"_segments": [{"path": str(seg_dir), "number": 0}], "maxqlog": 0}
        result = _route_timeline_summary(route)
        assert result is not None
        engaged = [s for s in result if s["type"] == "engaged"]
        assert len(engaged) == 1
        assert engaged[0]["route_offset_millis"] == 5000
        assert engaged[0]["end_route_offset_millis"] == 25000

    def test_trailing_engaged_span_closes_at_route_end(self, tmp_path):
        seg_dir = tmp_path / "seg0"
        seg_dir.mkdir()
        events = [
            {"type": "state", "route_offset_millis": 10000, "data": {"state": "enabled", "enabled": True, "alertStatus": 0}},
        ]
        (seg_dir / "events.json").write_text(json.dumps(events))
        route = {"_segments": [{"path": str(seg_dir), "number": 0}], "maxqlog": 2}
        result = _route_timeline_summary(route)
        engaged = [s for s in result if s["type"] == "engaged"]
        assert len(engaged) == 1
        # maxqlog=2 → end_ms = (2+1)*60000 = 180000
        assert engaged[0]["end_route_offset_millis"] == 180000

    def test_alert_span(self, tmp_path):
        seg_dir = tmp_path / "seg0"
        seg_dir.mkdir()
        events = [
            {"type": "state", "route_offset_millis": 1000, "data": {"state": "enabled", "enabled": True, "alertStatus": 2}},
            {"type": "state", "route_offset_millis": 5000, "data": {"state": "enabled", "enabled": True, "alertStatus": 0}},
        ]
        (seg_dir / "events.json").write_text(json.dumps(events))
        route = {"_segments": [{"path": str(seg_dir), "number": 0}], "maxqlog": 0}
        result = _route_timeline_summary(route)
        alerts = [s for s in result if s["type"] == "alert"]
        assert len(alerts) == 1
        assert alerts[0]["alertStatus"] == 2
        assert alerts[0]["route_offset_millis"] == 1000
        assert alerts[0]["end_route_offset_millis"] == 5000

    def test_overriding_span(self, tmp_path):
        seg_dir = tmp_path / "seg0"
        seg_dir.mkdir()
        events = [
            {"type": "state", "route_offset_millis": 2000, "data": {"state": "overriding", "enabled": True, "alertStatus": 0}},
            {"type": "state", "route_offset_millis": 6000, "data": {"state": "enabled", "enabled": True, "alertStatus": 0}},
        ]
        (seg_dir / "events.json").write_text(json.dumps(events))
        route = {"_segments": [{"path": str(seg_dir), "number": 0}], "maxqlog": 0}
        result = _route_timeline_summary(route)
        overrides = [s for s in result if s["type"] == "overriding"]
        assert len(overrides) == 1
        assert overrides[0]["route_offset_millis"] == 2000
        assert overrides[0]["end_route_offset_millis"] == 6000

    def test_user_flag_included(self, tmp_path):
        seg_dir = tmp_path / "seg0"
        seg_dir.mkdir()
        events = [
            {"type": "user_flag", "route_offset_millis": 12345},
        ]
        (seg_dir / "events.json").write_text(json.dumps(events))
        route = {"_segments": [{"path": str(seg_dir), "number": 0}], "maxqlog": 0}
        result = _route_timeline_summary(route)
        flags = [s for s in result if s["type"] == "user_flag"]
        assert len(flags) == 1
        assert flags[0]["route_offset_millis"] == 12345

    def test_preEnabled_counts_as_overriding(self, tmp_path):
        seg_dir = tmp_path / "seg0"
        seg_dir.mkdir()
        events = [
            {"type": "state", "route_offset_millis": 0, "data": {"state": "preEnabled", "enabled": True, "alertStatus": 0}},
            {"type": "state", "route_offset_millis": 3000, "data": {"state": "enabled", "enabled": True, "alertStatus": 0}},
        ]
        (seg_dir / "events.json").write_text(json.dumps(events))
        route = {"_segments": [{"path": str(seg_dir), "number": 0}], "maxqlog": 0}
        result = _route_timeline_summary(route)
        overrides = [s for s in result if s["type"] == "overriding"]
        assert len(overrides) == 1
