"""Shared fixtures for connect-on-device test suite."""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure the app package is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ─── Fake route data on disk ────────────────────────────────────────

@pytest.fixture
def fake_data_dir(tmp_path):
    """Temp dir with 2 fake routes containing empty files."""
    for seg in ("00000042--abc123--0", "00000042--abc123--1"):
        d = tmp_path / seg
        d.mkdir()
        (d / "rlog.zst").write_bytes(b"")
        (d / "qcamera.ts").write_bytes(b"")
    d = tmp_path / "00000100--def456--0"
    d.mkdir()
    (d / "rlog.zst").write_bytes(b"")
    (d / "fcamera.hevc").write_bytes(b"")
    return tmp_path


@pytest.fixture
def sample_metadata():
    """Dict in metadata.json format with one enriched route."""
    return {
        "version": "1.0",
        "last_updated": "2025-12-01T00:00:00+00:00",
        "hidden_routes": [],
        "preserved_routes": [],
        "routes": {
            "00000042--abc123": {
                "route_id": "00000042--abc123",
                "creation_time": "2025-11-15T10:30:00+08:00",
                "gps_coordinates": [31.23, 121.47],
                "dongle_id": "test123",
                "git_commit": "abcdef1",
                "git_branch": "bmw-master",
                "openpilot_version": "0.9.8",
                "car_fingerprint": "BMW_E90",
                "git_remote": "https://github.com/test/openpilot.git",
                "device_type": "tici",
                "total_distance_m": 5200.0,
                "gps_time": 1731649800.0,
                "enriched": True,
                "source": "connect_server",
            },
            "00000100--def456": {
                "route_id": "00000100--def456",
                "creation_time": "2025-11-20T14:00:00+08:00",
                "gps_coordinates": [31.20, 121.50],
                "dongle_id": "test123",
                "git_commit": "1234567",
                "git_branch": "bmw-master",
                "openpilot_version": "0.9.9",
                "car_fingerprint": "BMW_E90",
                "device_type": "tici",
                "total_distance_m": 12300.0,
                "gps_time": 1732082400.0,
                "enriched": True,
                "source": "connect_server",
            },
        },
    }


@pytest.fixture
def populated_data_dir(fake_data_dir, sample_metadata):
    """fake_data_dir with metadata.json written."""
    meta_path = fake_data_dir / ".route_metadata.json"
    meta_path.write_text(json.dumps(sample_metadata, indent=2))
    return fake_data_dir


@pytest.fixture
def mock_store(populated_data_dir):
    """RouteStore with patched detection methods."""
    from route_store import RouteStore
    with patch.object(RouteStore, "_detect_dongle_id") as mock_did, \
         patch.object(RouteStore, "_detect_agnos_version") as mock_av:
        store = RouteStore(str(populated_data_dir))
        store._dongle_id = "test123"
        store._agnos_version = "12.4"
        store.scan(force=True)
    return store


@pytest.fixture
def app(mock_store):
    """aiohttp Application with mock store, enrichment disabled."""
    from server import create_app
    application = create_app(str(mock_store.data_dir), str(mock_store.data_dir / "_static"))
    # Replace the store with our mock
    application["store"] = mock_store
    # Remove enrichment hooks (they require cereal)
    application.on_startup.clear()
    application.on_cleanup.clear()
    # Create minimal static dir
    static_dir = mock_store.data_dir / "_static"
    static_dir.mkdir(exist_ok=True)
    (static_dir / "index.html").write_text("<html><body>test</body></html>")
    application["static_dir"] = static_dir
    return application


@pytest.fixture
def client(aiohttp_client, app):
    """pytest-aiohttp test client."""
    return aiohttp_client(app)


@pytest.fixture
def sample_route_dict():
    """Fully-populated route dict with internal fields."""
    return {
        "create_time": 1700000000,
        "dongle_id": "test123",
        "end_lat": 31.23,
        "end_lng": 121.47,
        "end_time": "2025-11-15T11:30:00+08:00",
        "fullname": "test123/2025-11-15--10-30-00",
        "git_branch": "bmw-master",
        "git_commit": "abcdef1",
        "is_public": True,
        "distance": 3.2,
        "maxqlog": 5,
        "platform": "BMW_E90",
        "start_lat": 31.23,
        "start_lng": 121.47,
        "start_time": "2025-11-15T10:30:00+08:00",
        "url": None,
        "version": "0.9.8",
        "local_id": "00000042--abc123",
        "_local_id": "00000042--abc123",
        "_segments": [{"number": 0, "path": "/tmp/00000042--abc123--0", "size": 100, "files": {"rlog.zst"}}],
        "_seg_numbers": [0, 1, 2, 3, 4, 5],
        "_seg_start_times": [1700000000, 1700000060, 1700000120, 1700000180, 1700000240, 1700000300],
        "_seg_end_times": [1700000060, 1700000120, 1700000180, 1700000240, 1700000300, 1700000360],
    }


@pytest.fixture
def sample_events_json():
    """List of event dicts modeling an engage-disengage cycle."""
    return [
        {
            "type": "state",
            "time": 1700000010.0,
            "offset_millis": 10000,
            "route_offset_millis": 10000,
            "data": {"state": "enabled", "enabled": True, "alertStatus": 0},
        },
        {
            "type": "state",
            "time": 1700000040.0,
            "offset_millis": 40000,
            "route_offset_millis": 40000,
            "data": {"state": "disabled", "enabled": False, "alertStatus": 0},
        },
    ]
