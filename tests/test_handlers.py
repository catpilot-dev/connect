"""Tests for HTTP handlers — integration tests using pytest-aiohttp."""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ─── Auth / device endpoints ─────────────────────────────────────────

class TestAuth:
    @pytest.mark.asyncio
    async def test_me(self, client):
        c = await client
        resp = await c.get("/v1/me/")
        assert resp.status == 200
        data = await resp.json()
        assert data["user_id"] == "local"
        assert data["email"] == "local@device"

    @pytest.mark.asyncio
    async def test_auth(self, client):
        c = await client
        resp = await c.post("/v2/auth/")
        assert resp.status == 200
        data = await resp.json()
        assert "access_token" in data


class TestDevices:
    @pytest.mark.asyncio
    async def test_devices_list(self, client):
        c = await client
        resp = await c.get("/v1/me/devices/")
        assert resp.status == 200
        data = await resp.json()
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["dongle_id"] == "test123"

    @pytest.mark.asyncio
    async def test_device_get(self, client):
        c = await client
        resp = await c.get("/v1.1/devices/test123/")
        assert resp.status == 200
        data = await resp.json()
        assert data["dongle_id"] == "test123"
        assert data["is_owner"] is True

    @pytest.mark.asyncio
    async def test_device_stats(self, client):
        c = await client
        resp = await c.get("/v1.1/devices/test123/stats")
        assert resp.status == 200
        data = await resp.json()
        assert "all" in data
        assert "week" in data
        assert "routes" in data["all"]
        assert "distance_m" in data["all"]
        assert "engaged_m" in data["all"]
        assert "total_m_with_engagement" in data["all"]


# ─── Route list ──────────────────────────────────────────────────────

class TestRouteList:
    @pytest.mark.asyncio
    async def test_default_list(self, client):
        c = await client
        resp = await c.get("/v1/devices/test123/routes")
        assert resp.status == 200
        data = await resp.json()
        assert isinstance(data, list)
        assert len(data) >= 1

    @pytest.mark.asyncio
    async def test_limit_pagination(self, client):
        c = await client
        resp = await c.get("/v1/devices/test123/routes?limit=1")
        assert resp.status == 200
        data = await resp.json()
        assert len(data) <= 1

    @pytest.mark.asyncio
    async def test_before_counter_cursor(self, client):
        c = await client
        # Get all routes first
        resp = await c.get("/v1/devices/test123/routes")
        all_routes = await resp.json()
        if len(all_routes) >= 1:
            # Use the first route's counter as cursor
            counter = all_routes[0].get("route_counter", 999999)
            resp = await c.get(f"/v1/devices/test123/routes?filter=all&before_counter={counter}")
            data = await resp.json()
            # Should have fewer routes (or same if only one)
            for r in data:
                assert r["route_counter"] < counter

    @pytest.mark.asyncio
    async def test_sorted_by_counter_desc(self, client):
        c = await client
        resp = await c.get("/v1/devices/test123/routes")
        data = await resp.json()
        if len(data) >= 2:
            counters = [r["route_counter"] for r in data]
            assert counters == sorted(counters, reverse=True)

    @pytest.mark.asyncio
    async def test_no_underscore_fields(self, client):
        c = await client
        resp = await c.get("/v1/devices/test123/routes")
        data = await resp.json()
        for route in data:
            for key in route:
                assert not key.startswith("_"), f"Internal field {key} leaked"

    @pytest.mark.asyncio
    async def test_route_counter_field_present(self, client):
        c = await client
        resp = await c.get("/v1/devices/test123/routes")
        data = await resp.json()
        for route in data:
            assert "route_counter" in route


# ─── Route detail ────────────────────────────────────────────────────

class TestRouteDetail:
    @pytest.mark.asyncio
    async def test_found(self, client, mock_store):
        c = await client
        routes = mock_store.scan()
        fullname = next(iter(routes))
        route_name = fullname.replace("/", "|")
        resp = await c.get(f"/v1/route/{route_name}/")
        assert resp.status == 200
        data = await resp.json()
        assert data["fullname"] == fullname

    @pytest.mark.asyncio
    async def test_not_found(self, client):
        c = await client
        resp = await c.get("/v1/route/nonexistent|route/")
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_files_endpoint(self, client, mock_store):
        c = await client
        routes = mock_store.scan()
        fullname = next(iter(routes))
        route_name = fullname.replace("/", "|")
        resp = await c.get(f"/v1/route/{route_name}/files")
        assert resp.status == 200
        data = await resp.json()
        # Should have array keys
        for key in ("cameras", "dcameras", "ecameras", "logs", "qcameras", "qlogs"):
            assert key in data
            assert isinstance(data[key], list)

    @pytest.mark.asyncio
    async def test_share_signature(self, client, mock_store):
        c = await client
        routes = mock_store.scan()
        fullname = next(iter(routes))
        route_name = fullname.replace("/", "|")
        resp = await c.get(f"/v1/route/{route_name}/share_signature")
        assert resp.status == 200
        data = await resp.json()
        assert "exp" in data
        assert "sig" in data


# ─── Route actions ───────────────────────────────────────────────────

class TestRouteActions:
    @pytest.mark.asyncio
    async def test_delete_hides_route(self, client, mock_store):
        c = await client
        routes = mock_store.scan()
        fullname = next(iter(routes))
        route_name = fullname.replace("/", "|")
        resp = await c.delete(f"/v1/route/{route_name}/")
        assert resp.status == 200
        data = await resp.json()
        assert data["success"] == 1

    @pytest.mark.asyncio
    async def test_preserve_unpreserve(self, client, mock_store):
        c = await client
        routes = mock_store.scan()
        fullname = next(iter(routes))
        route_name = fullname.replace("/", "|")

        # Preserve
        resp = await c.post(f"/v1/route/{route_name}/preserve")
        assert resp.status == 200

        # Unpreserve
        resp = await c.delete(f"/v1/route/{route_name}/preserve")
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_preserved_list(self, client, mock_store):
        c = await client
        routes = mock_store.scan()
        fullname = next(iter(routes))
        route_name = fullname.replace("/", "|")

        # Preserve a route
        await c.post(f"/v1/route/{route_name}/preserve")

        # Check preserved list
        resp = await c.get("/v1/devices/test123/routes/preserved")
        assert resp.status == 200
        data = await resp.json()
        assert isinstance(data, list)
        assert len(data) >= 1
        assert any(r.get("is_preserved") for r in data)


# ─── File serving ────────────────────────────────────────────────────

class TestFileServing:
    @pytest.mark.asyncio
    async def test_qcamera_served(self, client, mock_store):
        c = await client
        routes = mock_store.scan()
        for fullname, r in routes.items():
            # Write some content to qcamera.ts
            if r["_segments"]:
                seg_path = Path(r["_segments"][0]["path"])
                (seg_path / "qcamera.ts").write_bytes(b"\x00\x01\x02\x03")
                route_date = fullname.split("/")[-1]
                dongle = r["dongle_id"]
                seg_num = r["_segments"][0]["number"]
                resp = await c.get(f"/connectdata/{dongle}/{route_date}/{seg_num}/qcamera.ts")
                assert resp.status == 200
                assert resp.content_type == "video/mp2t"
                break

    @pytest.mark.asyncio
    async def test_forbidden_filename(self, client, mock_store):
        c = await client
        routes = mock_store.scan()
        fullname = next(iter(routes))
        route_date = fullname.split("/")[-1]
        resp = await c.get(f"/connectdata/test123/{route_date}/0/secret.txt")
        assert resp.status == 403


# ─── Storage / download ─────────────────────────────────────────────

class TestStorage:
    @pytest.mark.asyncio
    async def test_storage_endpoint(self, client):
        c = await client
        resp = await c.get("/v1/storage")
        assert resp.status == 200
        data = await resp.json()
        assert "total" in data
        assert "free" in data

    @pytest.mark.asyncio
    async def test_download_invalid_file_type(self, client, mock_store):
        c = await client
        routes = mock_store.scan()
        fullname = next(iter(routes))
        route_name = fullname.replace("/", "|")
        resp = await c.get(f"/v1/route/{route_name}/download?files=invalid_type")
        assert resp.status == 400


# ─── CORS ────────────────────────────────────────────────────────────

class TestCORS:
    @pytest.mark.asyncio
    async def test_cors_headers(self, client):
        c = await client
        resp = await c.get("/v1/me/")
        assert resp.headers.get("Access-Control-Allow-Origin") == "*"

    @pytest.mark.asyncio
    async def test_options_returns_200(self, client):
        c = await client
        resp = await c.options("/v1/me/")
        assert resp.status == 200
        assert resp.headers.get("Access-Control-Allow-Origin") == "*"


# ─── SPA ─────────────────────────────────────────────────────────────

class TestSPA:
    @pytest.mark.asyncio
    async def test_index_served(self, client):
        c = await client
        resp = await c.get("/")
        assert resp.status == 200
        body = await resp.text()
        assert "test" in body


# ─── Route notes ────────────────────────────────────────────────────

class TestRouteNotes:
    @pytest.mark.asyncio
    async def test_set_note(self, client, mock_store):
        c = await client
        routes = mock_store.scan()
        fullname = next(iter(routes))
        route_name = fullname.replace("/", "|")
        resp = await c.post(
            f"/v1/route/{route_name}/note",
            json={"note": "Test note"},
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "ok"

    @pytest.mark.asyncio
    async def test_empty_note(self, client, mock_store):
        c = await client
        routes = mock_store.scan()
        fullname = next(iter(routes))
        route_name = fullname.replace("/", "|")
        resp = await c.post(
            f"/v1/route/{route_name}/note",
            json={"note": ""},
        )
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_note_persists(self, client, mock_store):
        c = await client
        routes = mock_store.scan()
        fullname = next(iter(routes))
        route_name = fullname.replace("/", "|")

        await c.post(f"/v1/route/{route_name}/note", json={"note": "My note"})

        # Verify via route detail
        resp = await c.get(f"/v1/route/{route_name}/")
        data = await resp.json()
        assert data.get("notes") == "My note"


# ─── Route bookmarks ───────────────────────────────────────────────

class TestRouteBookmarks:
    @pytest.mark.asyncio
    async def test_add_bookmark(self, client, mock_store):
        c = await client
        routes = mock_store.scan()
        fullname = next(iter(routes))
        route_name = fullname.replace("/", "|")
        resp = await c.post(
            f"/v1/route/{route_name}/bookmark",
            json={"time_sec": 30.5, "label": "Good merge"},
        )
        assert resp.status == 200
        data = await resp.json()
        assert len(data["bookmarks"]) == 1
        assert data["bookmarks"][0]["label"] == "Good merge"

    @pytest.mark.asyncio
    async def test_add_bookmark_no_label(self, client, mock_store):
        c = await client
        routes = mock_store.scan()
        fullname = next(iter(routes))
        route_name = fullname.replace("/", "|")
        resp = await c.post(
            f"/v1/route/{route_name}/bookmark",
            json={"time_sec": 10.0, "label": ""},
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_update_bookmark(self, client, mock_store):
        c = await client
        routes = mock_store.scan()
        fullname = next(iter(routes))
        route_name = fullname.replace("/", "|")

        # Add first
        await c.post(f"/v1/route/{route_name}/bookmark", json={"time_sec": 10.0, "label": "Original"})

        # Update
        resp = await c.put(
            f"/v1/route/{route_name}/bookmark/0",
            json={"label": "Updated"},
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["bookmarks"][0]["label"] == "Updated"

    @pytest.mark.asyncio
    async def test_delete_bookmark(self, client, mock_store):
        c = await client
        routes = mock_store.scan()
        fullname = next(iter(routes))
        route_name = fullname.replace("/", "|")

        # Add
        await c.post(f"/v1/route/{route_name}/bookmark", json={"time_sec": 10.0, "label": "Delete me"})

        # Delete
        resp = await c.delete(f"/v1/route/{route_name}/bookmark/0")
        assert resp.status == 200
        data = await resp.json()
        assert len(data["bookmarks"]) == 0


# ─── Route list filters ────────────────────────────────────────────

class TestRouteListFilters:
    @pytest.mark.asyncio
    async def test_saved_filter(self, client, mock_store):
        c = await client
        routes = mock_store.scan()
        fullname = next(iter(routes))
        route_name = fullname.replace("/", "|")

        # Preserve a route
        await c.post(f"/v1/route/{route_name}/preserve")

        # Query saved tab
        resp = await c.get("/v1/devices/test123/routes?filter=saved")
        assert resp.status == 200
        data = await resp.json()
        assert all(r.get("is_preserved") for r in data)

    @pytest.mark.asyncio
    async def test_recycled_filter(self, client, mock_store):
        c = await client
        routes = mock_store.scan()
        fullname = next(iter(routes))
        route_name = fullname.replace("/", "|")

        # Delete a route
        await c.delete(f"/v1/route/{route_name}/")

        # Query recycled tab
        resp = await c.get("/v1/devices/test123/routes?filter=recycled")
        assert resp.status == 200
        data = await resp.json()
        assert isinstance(data, list)

    @pytest.mark.asyncio
    async def test_all_filter(self, client):
        c = await client
        resp = await c.get("/v1/devices/test123/routes?filter=all")
        assert resp.status == 200
        data = await resp.json()
        assert isinstance(data, list)

    @pytest.mark.asyncio
    async def test_limit_parameter(self, client):
        c = await client
        resp = await c.get("/v1/devices/test123/routes?limit=1")
        assert resp.status == 200
        data = await resp.json()
        assert len(data) <= 1


# ─── Route segments endpoint ───────────────────────────────────────

class TestRouteSegments:
    @pytest.mark.asyncio
    async def test_routes_segments(self, client, mock_store):
        c = await client
        resp = await c.get("/v1/devices/test123/routes_segments")
        assert resp.status == 200
        data = await resp.json()
        assert isinstance(data, list)
        if data:
            seg = data[0]
            assert "segment_numbers" in seg
            assert "segment_start_times" in seg
            assert "segment_end_times" in seg
            assert "start_time_utc_millis" in seg
            assert "end_time_utc_millis" in seg

    @pytest.mark.asyncio
    async def test_routes_segments_with_filter(self, client, mock_store):
        c = await client
        routes = mock_store.scan()
        fullname = next(iter(routes))
        resp = await c.get(f"/v1/devices/test123/routes_segments?route_str={fullname}")
        assert resp.status == 200
        data = await resp.json()
        assert isinstance(data, list)


# ─── Stubs ──────────────────────────────────────────────────────────

class TestStubs:
    @pytest.mark.asyncio
    async def test_bootlogs_returns_empty_array(self, client):
        c = await client
        resp = await c.get("/v1/devices/test123/bootlogs")
        assert resp.status == 200
        data = await resp.json()
        assert data == []

    @pytest.mark.asyncio
    async def test_crashlogs_returns_empty_array(self, client):
        c = await client
        resp = await c.get("/v1/devices/test123/crashlogs")
        assert resp.status == 200
        data = await resp.json()
        assert data == []


# ─── Route manifest redirect ───────────────────────────────────────

class TestManifest:
    @pytest.mark.asyncio
    async def test_manifest_redirects(self, client, mock_store):
        c = await client
        routes = mock_store.scan()
        fullname = next(iter(routes))
        route_name = fullname.replace("/", "|")
        resp = await c.get(f"/v1/route/{route_name}/manifest.m3u8", allow_redirects=False)
        assert resp.status == 302
        assert "qcamera.m3u8" in resp.headers["Location"]


# ─── Share signature ────────────────────────────────────────────────

class TestShareSignature:
    @pytest.mark.asyncio
    async def test_returns_dummy_signature(self, client, mock_store):
        c = await client
        routes = mock_store.scan()
        fullname = next(iter(routes))
        route_name = fullname.replace("/", "|")
        resp = await c.get(f"/v1/route/{route_name}/share_signature")
        assert resp.status == 200
        data = await resp.json()
        assert data["exp"] == "9999999999"
        assert data["sig"] == "local"
