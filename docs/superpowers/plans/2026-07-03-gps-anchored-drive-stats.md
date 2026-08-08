# GPS-Anchored Drive Stats Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make COD's `/v1.1/devices/{id}/stats` all-time / past-7-days figures GPS-time-anchored and materialized when a drive ends, so a build-time-seeded system clock can never widen the 7-day window into the all-time totals.

**Architecture:** The device (`ui_mod/drive_tracker`) POSTs brief per-drive stats to COD at offroad. COD stores them per-route in `metadata.json` and recomputes a cached aggregate (`self._stats`) anchored to `max(create_time)` (the newest route's GPS time) — never `time.time()`. `handle_device_stats` serves the cache.

**Tech Stack:** Python 3.12, aiohttp (COD server), pyray (device UI), pytest.

## Global Constraints

- **Never call `time.time()`** (or any wall-clock source) in stats aggregation. On AGNOS the system clock is seeded from the OS build time. GPS-derived `create_time` is the only trusted time source.
- COD test runner: `cd connect-on-device && python -m pytest tests/ -v` (repo root on `sys.path` via the test files' `sys.path.insert`).
- Preserve the existing stats JSON shape: `{"all": {distance_m, minutes, routes, engaged_m, total_m_with_engagement}, "week": {...}}`. The device renderer (`ui_mod/drive_stats.py`) must need no change.
- Engaged % is distance-based: `Σ engaged_m / Σ distance_m` per bucket, over routes that have a brief `drive_stats` record only.
- Route endpoints use singular `/v1/route/{routeName}/...`; `routeName` is `local_id` with `/`→`|` escaping (here `local_id` has no `/`, so it passes through).
- Two repos: `connect-on-device` (branch `dev`) and `plugins` (branch `dev`, device `ui_mod`).

---

### Task 1: `RouteStore._compute_stats()` — GPS-anchored aggregation

**Files:**
- Modify: `route_store.py` — add `_compute_stats()` method; init `self._stats` in `__init__` (after line ~140, near other state).
- Test: `tests/test_route_store.py` — new `TestComputeStats` class.

**Interfaces:**
- Consumes: `self._routes` (dict `fullname -> route dict`); each route dict has `create_time: float` (GPS-derived seconds), `distance_m: float|None`, `_segments: list`, and an optional `drive_stats: {distance_m, duration_s, engaged_m, engaged_s}` injected by Task 2.
- Produces: `self._stats: dict` shaped `{"all": {...}, "week": {...}, "reference_time": float}`; method `_compute_stats() -> dict` (also returns it).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_route_store.py`:

```python
from unittest.mock import patch

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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd connect-on-device && python -m pytest tests/test_route_store.py::TestComputeStats -v`
Expected: FAIL — `AttributeError: 'RouteStore' object has no attribute '_compute_stats'`

- [ ] **Step 3: Add `self._stats` init**

In `route_store.py` `__init__`, alongside the other `self._...` state (right after `self._preserved: set = set()`):

```python
        self._stats: dict = {"all": {}, "week": {}, "reference_time": 0}
```

- [ ] **Step 4: Implement `_compute_stats`**

Add this method to `RouteStore` (place it just above `_rebuild_routes`):

```python
    WEEK_SECONDS = 7 * 86400

    def _compute_stats(self) -> dict:
        """Aggregate all-time and past-7-day drive stats from brief per-route
        records. Anchored to the newest route's GPS create_time — never the
        system wall clock (AGNOS seeds it from build time). Result cached on
        self._stats and returned.
        """
        routes = list(self._routes.values())
        reference_now = max((r.get("create_time") or 0 for r in routes), default=0)
        week_ago = reference_now - self.WEEK_SECONDS

        def _empty():
            return {"distance_m": 0.0, "minutes": 0, "routes": 0,
                    "engaged_m": 0.0, "total_m_with_engagement": 0.0}

        all_stats, week_stats = _empty(), _empty()

        for r in routes:
            ds = r.get("drive_stats") or {}
            distance_m = ds.get("distance_m")
            if distance_m is None:
                distance_m = r.get("distance_m") or 0
            duration_s = ds.get("duration_s")
            minutes = round(duration_s / 60) if duration_s is not None else len(r.get("_segments", []))
            engaged_m = ds.get("engaged_m")
            has_engagement = engaged_m is not None and distance_m and distance_m > 0

            buckets = [all_stats]
            if (r.get("create_time") or 0) >= week_ago:
                buckets.append(week_stats)
            for b in buckets:
                b["routes"] += 1
                b["minutes"] += minutes
                b["distance_m"] += distance_m
                if has_engagement:
                    b["engaged_m"] += engaged_m
                    b["total_m_with_engagement"] += distance_m

        for s in (all_stats, week_stats):
            s["distance_m"] = round(s["distance_m"], 1)
            s["engaged_m"] = round(s["engaged_m"], 1)
            s["total_m_with_engagement"] = round(s["total_m_with_engagement"], 1)

        self._stats = {"all": all_stats, "week": week_stats, "reference_time": reference_now}
        return self._stats
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd connect-on-device && python -m pytest tests/test_route_store.py::TestComputeStats -v`
Expected: PASS (4 tests)

- [ ] **Step 6: Commit**

```bash
cd connect-on-device
git add route_store.py tests/test_route_store.py
git commit -m "feat(stats): GPS-anchored _compute_stats aggregation"
```

---

### Task 2: Inject `drive_stats` into route dicts + recompute on rebuild

**Files:**
- Modify: `route_store.py` — `_meta_to_internal` (carry `drive_stats` from metadata), `_build_route` (copy it onto the route dict), `_rebuild_routes` (call `_compute_stats` at the end).
- Test: `tests/test_route_store.py::TestComputeStats` — add rebuild integration test.

**Interfaces:**
- Consumes: `self._metadata[local_id]["drive_stats"]` (dict written by Task 3's `set_drive_stats`).
- Produces: route dicts carry `drive_stats`; `self._stats` is refreshed whenever `_rebuild_routes()` runs (scan, enrichment, note/stat setters).

- [ ] **Step 1: Write the failing test**

Add to `TestComputeStats`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd connect-on-device && python -m pytest tests/test_route_store.py::TestComputeStats::test_rebuild_populates_stats_from_metadata -v`
Expected: FAIL — `store._stats["all"]["routes"]` is 0 (drive_stats not wired through) or KeyError.

- [ ] **Step 3: Carry `drive_stats` in `_meta_to_internal`**

In `route_store.py::_meta_to_internal`, before `return result` (end of the method), add:

```python
        ds = meta.get("drive_stats")
        if isinstance(ds, dict):
            result["drive_stats"] = ds
```

- [ ] **Step 4: Copy it onto the route dict in `_build_route`**

In `_build_route`'s returned dict, add one key (place after `"distance_m": self._calc_route_distance(...)`):

```python
            "drive_stats": internal.get("drive_stats"),
```

- [ ] **Step 5: Recompute at the end of `_rebuild_routes`**

At the very end of `_rebuild_routes`, after `self._local_id_map = local_id_map`, add:

```python
        self._compute_stats()
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd connect-on-device && python -m pytest tests/test_route_store.py::TestComputeStats -v`
Expected: PASS (5 tests)

- [ ] **Step 7: Commit**

```bash
cd connect-on-device
git add route_store.py tests/test_route_store.py
git commit -m "feat(stats): wire drive_stats through rebuild, recompute on rebuild"
```

---

### Task 3: `RouteStore.set_drive_stats()` setter

**Files:**
- Modify: `route_store.py` — add `set_drive_stats` (mirror `set_note`).
- Test: `tests/test_route_store.py` — new `TestSetDriveStats` class.

**Interfaces:**
- Consumes: `local_id: str`, `stats: dict` with float keys `distance_m, duration_s, engaged_m, engaged_s`.
- Produces: `set_drive_stats(local_id, stats)` — persists to `_metadata[local_id]["drive_stats"]`, rebuilds routes (which recomputes `self._stats`), saves metadata. Used by Task 4's handler.

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd connect-on-device && python -m pytest tests/test_route_store.py::TestSetDriveStats -v`
Expected: FAIL — `AttributeError: ... has no attribute 'set_drive_stats'`

- [ ] **Step 3: Implement the setter**

Add to `RouteStore` (just below `set_note`):

```python
    def set_drive_stats(self, local_id: str, stats: dict):
        """Store brief per-drive stats (distance_m, duration_s, engaged_m,
        engaged_s) posted by the device at offroad. Triggers a GPS-anchored
        aggregate recompute via _rebuild_routes.
        """
        meta = self._metadata.get(local_id)
        if not meta:
            meta = {"route_id": local_id}
            self._metadata[local_id] = meta
        meta["drive_stats"] = {
            "distance_m": float(stats.get("distance_m", 0.0)),
            "duration_s": float(stats.get("duration_s", 0.0)),
            "engaged_m": float(stats.get("engaged_m", 0.0)),
            "engaged_s": float(stats.get("engaged_s", 0.0)),
        }
        self._rebuild_routes()
        self._save_metadata()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd connect-on-device && python -m pytest tests/test_route_store.py::TestSetDriveStats -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd connect-on-device
git add route_store.py tests/test_route_store.py
git commit -m "feat(stats): set_drive_stats setter persists brief per-route stats"
```

---

### Task 4: `POST /v1/route/{routeName}/drive_stats` endpoint + serve cache

**Files:**
- Modify: `handlers/routes.py` — add `handle_route_drive_stats`.
- Modify: `handlers/auth.py` — rewrite `handle_device_stats` to serve `store._stats`.
- Modify: `handlers/__init__.py` — export `handle_route_drive_stats`.
- Modify: `server.py` — import + register the POST route.
- Test: `tests/test_route_store.py` — endpoint is exercised via `set_drive_stats` (Task 3) + a handler unit test using aiohttp test utils if present; otherwise a direct handler call test below.

**Interfaces:**
- Consumes: `store.set_drive_stats` (Task 3), `store._stats` (Task 1), `_resolve_local_id` (existing, `route_helpers`).
- Produces: `handle_route_drive_stats(request) -> web.Response`; `handle_device_stats` returns `{"all", "week"}` from cache.

- [ ] **Step 1: Write the failing test**

Add a handler test (direct-call style, no live server) to `tests/test_route_store.py`:

```python
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
```

Note: `handle_device_stats` currently calls `store.async_scan()` and `enrich_new_routes`. The rewrite (Step 4) keeps `async_scan` but the test's store has an empty realdata dir, so scan is a no-op and the pre-seeded `_routes`/`_stats` survive. If `async_scan` clears `_routes` on an empty dir, the test seeds via `_rebuild_routes()` after setting `_raw`, and `set_drive_stats` re-runs it — cache stays populated.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd connect-on-device && python -m pytest tests/test_route_store.py::TestDriveStatsEndpoint -v`
Expected: FAIL — `ImportError: cannot import name 'handle_route_drive_stats'`

- [ ] **Step 3: Add the POST handler**

In `handlers/routes.py`, add (near `handle_route_note`):

```python
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
    store.set_drive_stats(local_id, stats)
    return web.json_response({"status": "ok"})
```

- [ ] **Step 4: Rewrite `handle_device_stats` to serve the cache**

In `handlers/auth.py`, replace the body of `handle_device_stats` with:

```python
async def handle_device_stats(request: web.Request) -> web.Response:
    """GET /v1.1/devices/{dongleId}/stats — GPS-anchored driving statistics.

    Serves the aggregate materialized by RouteStore._compute_stats (recomputed
    when routes change / a drive is POSTed). Does NOT read the system wall clock:
    on AGNOS it is build-time-seeded and unreliable.
    """
    store = request.app["store"]
    await store.async_scan()
    stats = store._stats
    if not stats.get("all"):
        stats = store._compute_stats()
    return web.json_response({"all": stats["all"], "week": stats["week"]})
```

Then remove the now-unused import if nothing else in the file uses it. Check first:
Run: `grep -n "_route_engaged_distance\|^import time\|time\.time" connect-on-device/handlers/auth.py`
- Keep `import time` only if still referenced elsewhere in the file; the test patches `handlers.auth.time.time`, so **leave `import time` in place** even though the new code doesn't call it (harmless, and keeps the patch target valid).
- Remove `from route_helpers import _route_engaged_distance` only if no other function in `auth.py` uses it (it does not — it was stats-only).

- [ ] **Step 5: Export + register the endpoint**

In `handlers/__init__.py`, add `handle_route_drive_stats` to the `from .routes import (...)` block.

In `server.py`: add to the routes import block, then register alongside the other route POSTs (after line ~227):

```python
    app.router.add_post("/v1/route/{routeName}/drive_stats", handle_route_drive_stats)
```

- [ ] **Step 6: Run the full COD suite**

Run: `cd connect-on-device && python -m pytest tests/ -v`
Expected: PASS (new tests + existing green). If `test_route_store.py`'s async tests need `pytest-asyncio`, they use `asyncio.run` directly (no marker needed) per the code above.

- [ ] **Step 7: Commit**

```bash
cd connect-on-device
git add handlers/routes.py handlers/auth.py handlers/__init__.py server.py tests/test_route_store.py
git commit -m "feat(stats): drive_stats POST endpoint; serve GPS-anchored cache"
```

---

### Task 5: Device — POST brief stats from `drive_tracker._save`

**Files:**
- Modify: `plugins/plugins/ui_mod/drive_tracker.py` — add `_resolve_route_id()` and a best-effort POST at the end of `_save`.
- Test: `plugins/plugins/ui_mod/tests/test_drive_tracker.py` (create if absent) — unit test for `_resolve_route_id` and that `_save` attempts a POST with the right payload.

**Interfaces:**
- Consumes: `Paths.log_root()` → `/data/media/0/realdata/` (newest `<route>--<seg>` dir); COD at `http://localhost`.
- Produces: `POST http://localhost/v1/route/{local_id}/drive_stats` with JSON `{distance_m, duration_s, engaged_m, engaged_s}`.

- [ ] **Step 1: Write the failing test**

Create `plugins/plugins/ui_mod/tests/test_drive_tracker.py`:

```python
import os
from unittest.mock import patch, MagicMock

# _resolve_route_id: newest realdata dir minus the --<seg> suffix
def test_resolve_route_id_strips_segment(tmp_path):
    from importlib import import_module
    dt = import_module("plugins.ui_mod.drive_tracker")
    root = tmp_path / "realdata"
    root.mkdir()
    (root / "00000385--6e363981a3--6").mkdir()
    (root / "00000385--6e363981a3--7").mkdir()
    (root / "00000385--6e363981a3--8").mkdir()
    with patch.object(dt, "_log_root", return_value=str(root)):
        assert dt._resolve_route_id() == "00000385--6e363981a3"

def test_resolve_route_id_none_when_empty(tmp_path):
    from importlib import import_module
    dt = import_module("plugins.ui_mod.drive_tracker")
    root = tmp_path / "realdata"; root.mkdir()
    with patch.object(dt, "_log_root", return_value=str(root)):
        assert dt._resolve_route_id() is None
```

(Adjust the import path to match how the plugin test harness imports `ui_mod` modules — see the sibling `tests/` in `plugins` for the established `sys.path`/import pattern and mirror it.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd plugins && PYTHONPATH=. python -m pytest plugins/ui_mod/tests/test_drive_tracker.py -v`
Expected: FAIL — `_resolve_route_id` / `_log_root` not defined.

- [ ] **Step 3: Add route-id resolution + POST helpers**

In `plugins/plugins/ui_mod/drive_tracker.py`, add near the top (after imports):

```python
import re
import urllib.request

COD_BASE = "http://localhost"
_SEG_RE = re.compile(r"^(.*)--\d+$")


def _log_root() -> str:
    """Realdata root — indirection kept for tests."""
    from openpilot.system.hardware.hw import Paths
    return Paths.log_root()


def _resolve_route_id() -> str | None:
    """local_id of the most recently written route (newest realdata dir with
    the --<segment> suffix stripped). None if realdata is empty/unreadable.
    """
    try:
        root = _log_root()
        entries = [d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))]
    except OSError:
        return None
    if not entries:
        return None
    newest = max(entries, key=lambda d: os.path.getmtime(os.path.join(root, d)))
    m = _SEG_RE.match(newest)
    return m.group(1) if m else newest
```

- [ ] **Step 4: POST at the end of `_save`**

At the end of `DriveTracker._save` (after the `.last_drive.json` write block, still inside `_save`), add:

```python
    self._post_stats(data)
```

And add the method:

```python
  def _post_stats(self, data):
    """Best-effort POST of brief drive stats to COD. Non-fatal on failure —
    .last_drive.json is already written and the next offroad transition re-POSTs.
    """
    route_id = _resolve_route_id()
    if not route_id:
      return
    payload = {
      "distance_m": data["distance_m"],
      "duration_s": data["duration_s"],
      "engaged_m": data["engaged_m"],
      "engaged_s": data["engaged_s"],
    }
    def _send():
      try:
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
          f"{COD_BASE}/v1/route/{route_id}/drive_stats",
          data=body, headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=10).close()
      except Exception:
        pass
    threading.Thread(target=_send, daemon=True).start()
```

Add `import threading` to the imports if not already present (drive_stats.py uses it; confirm drive_tracker imports it — add if missing).

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd plugins && PYTHONPATH=. python -m pytest plugins/ui_mod/tests/test_drive_tracker.py -v`
Expected: PASS

- [ ] **Step 6: Run the plugins suite (pre-push parity)**

Run: `cd plugins && PYTHONPATH=. python -m pytest -q`
Expected: PASS (no regressions).

- [ ] **Step 7: Commit**

```bash
cd plugins
git add plugins/ui_mod/drive_tracker.py plugins/ui_mod/tests/test_drive_tracker.py
git commit -m "feat(ui_mod): POST brief drive stats to COD at offroad"
```

---

### Task 6: End-to-end verification on C3

**Files:** none (deploy + observe).

- [ ] **Step 1: Deploy COD**

Deploy the `connect-on-device` change to the C3 (per project deploy flow for COD). Restart the COD service.

- [ ] **Step 2: Verify GET is wall-clock-independent**

Run:
```bash
ssh c3 'DID=$(cat /data/params/d/DongleId); date; curl -s "http://localhost/v1.1/devices/$DID/stats"'
```
Expected: `week` routes/distance clearly **less** than `all` (matching the real ~5 drives / ~73 km, not the all-time totals), regardless of the reported system date.

- [ ] **Step 3: Deploy plugins + drive, or simulate a POST**

Simulate the device POST and confirm materialization:
```bash
ssh c3 'RID=$(ls -1dt /data/media/0/realdata/*/ | head -1 | xargs basename | sed -E "s/--[0-9]+$//"); \
  curl -s -X POST "http://localhost/v1/route/$RID/drive_stats" \
  -H "Content-Type: application/json" \
  -d "{\"distance_m\":1234,\"duration_s\":600,\"engaged_m\":1000,\"engaged_s\":480}"; echo; \
  DID=$(cat /data/params/d/DongleId); curl -s "http://localhost/v1.1/devices/$DID/stats"'
```
Expected: POST returns `{"status":"ok"}`; the GET reflects the posted brief stats in the aggregate; `.route_metadata.json` for that route now has a `drive_stats` block.

- [ ] **Step 4: Confirm the screenshot panel is correct**

Trigger a fresh screen capture (or take a real drive), fetch the newest `capture_*.png`, and confirm the "Past 7 Days" panel shows the true 7-day figures, not all-time.

---

## Self-Review

- **Spec coverage:** GPS-only anchoring (Task 1), materialize-on-arrival (Task 2 rebuild hook + Task 3 setter), no full-enrichment dependency (Task 1 reads `drive_stats`, `_route_engaged_distance` untouched), API endpoint (Task 4), device POST (Task 5), distance-based engaged % (Task 1), tests incl. stale-clock regression (Task 1). E2E (Task 6). All spec sections mapped.
- **Placeholders:** none — all steps carry concrete code/commands. The two "match the existing harness import pattern" notes (Task 5 Step 1) point at a real sibling `tests/` convention rather than inventing one; the implementer mirrors it.
- **Type consistency:** `_compute_stats` / `self._stats` shape, `set_drive_stats(local_id, stats)`, `drive_stats` dict keys (`distance_m, duration_s, engaged_m, engaged_s`), and `handle_route_drive_stats` payload are identical across Tasks 1–5. Endpoint path `/v1/route/{routeName}/drive_stats` consistent between Task 4 registration and Task 5 client.
```

---

## Addendum (post-final-review): hybrid GPS-time materialization

**Why:** The final whole-branch review found a Critical gap the per-task reviews couldn't see. Task 4 removed the `enrich_new_routes()` call from `handle_device_stats` — the only automatic GPS-time enrichment trigger. A freshly-POSTed drive's metadata (`{route_id, drive_stats}`) has no `gps_time`, so `_meta_to_internal` sets no `wall_time_nanos` and `_rebuild_routes:524` drops the route from `_routes` → it never appears in the aggregate until manually opened in the web UI. Chosen fix (user decision): **hybrid** — the device supplies GPS time in the POST as the primary path; COD falls back to a cheap seg0 qlog GPS-time parse when the device had no lock.

### Task 7: COD — accept device gps_time (primary) + restore qlog fallback

**Files:**
- Modify: `route_store.py` — `set_drive_stats` gains `gps_time` param; `enrich_new_routes` gate + merge + attempted-marker.
- Modify: `handlers/routes.py` — `handle_route_drive_stats` reads optional `gps_time`.
- Modify: `handlers/auth.py` — `handle_device_stats` restores gated `enrich_new_routes` call.
- Test: `tests/test_route_store.py`.

**Interfaces:**
- Produces: `set_drive_stats(local_id, stats, gps_time: float|None = None)`; `enrich_new_routes` now also brief-enriches routes whose only metadata is a `drive_stats` entry (missing `gps_time`), preserving `drive_stats`, marking `gps_enrich_attempted` on failure.

- [ ] **Step 1: Write failing tests**

Add to `tests/test_route_store.py`:

```python
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
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd connect-on-device && python -m pytest tests/test_route_store.py::TestHybridGpsTime -v`
Expected: FAIL (set_drive_stats has no gps_time param; enrich gate skips bare drive_stats entries).

- [ ] **Step 3: `set_drive_stats` accepts gps_time**

Replace the `set_drive_stats` signature/body (keep the docstring, extend it):

```python
    def set_drive_stats(self, local_id: str, stats: dict, gps_time: float | None = None):
        """Store brief per-drive stats (distance_m, duration_s, engaged_m,
        engaged_s) posted by the device at offroad, plus the drive's GPS start
        time when the device had a fix. gps_time lets the route enter the
        aggregate immediately without qlog enrichment (GPS time only — never the
        wall clock). Triggers a recompute via _rebuild_routes.
        """
        meta = self._metadata.get(local_id)
        if not meta:
            meta = {"route_id": local_id}
            self._metadata[local_id] = meta
        meta["drive_stats"] = {
            "distance_m": float(stats.get("distance_m", 0.0)),
            "duration_s": float(stats.get("duration_s", 0.0)),
            "engaged_m": float(stats.get("engaged_m", 0.0)),
            "engaged_s": float(stats.get("engaged_s", 0.0)),
        }
        if gps_time:
            meta["gps_time"] = float(gps_time)
        self._rebuild_routes()
        self._save_metadata()
```

- [ ] **Step 4: Fix `enrich_new_routes` gate + merge + attempted-marker**

In `enrich_new_routes`, change the selection list from:

```python
        new_routes = [
            (lid, info) for lid, info in self._raw.items()
            if lid not in self._metadata and lid not in self._hidden
        ]
```

to:

```python
        new_routes = [
            (lid, info) for lid, info in self._raw.items()
            if lid not in self._hidden
            and (lid not in self._metadata
                 or (self._metadata[lid].get("gps_time") is None
                     and not self._metadata[lid].get("gps_enrich_attempted")))
        ]
```

Then, in the per-route body, replace the two lines:

```python
                entry = self._log_to_metadata_entry(local_id, result)
                entry["enriched"] = False  # Full enrichment deferred to Enrich button
                self._metadata[local_id] = entry
```

with (preserve an existing brief drive_stats record; mark attempted when no GPS time was recoverable so no-lock routes aren't re-parsed every stats poll):

```python
                existing = self._metadata.get(local_id, {})
                entry = self._log_to_metadata_entry(local_id, result)
                entry["enriched"] = False  # Full enrichment deferred to Enrich button
                if existing.get("drive_stats"):
                    entry["drive_stats"] = existing["drive_stats"]
                if not result.get("gps_time"):
                    entry["gps_enrich_attempted"] = True
                self._metadata[local_id] = entry
```

- [ ] **Step 5: Handler reads optional gps_time**

In `handlers/routes.py::handle_route_drive_stats`, after building `stats` and before the `store.set_drive_stats(...)` call, add:

```python
    gps_time = body.get("gps_time")
    if gps_time is not None:
        try:
            gps_time = float(gps_time)
        except (TypeError, ValueError):
            return web.json_response({"error": "gps_time must be a number"}, status=400)
    store.set_drive_stats(local_id, stats, gps_time=gps_time)
```

(Remove the old `store.set_drive_stats(local_id, stats)` line — replaced by the call above.)

- [ ] **Step 6: Restore gated fallback enrich on the stats path**

In `handlers/auth.py::handle_device_stats`, between `await store.async_scan()` and reading `store._stats`, add:

```python
    # Fallback: brief-enrich routes still missing a GPS time (device had no
    # lock, or an old route predating device-supplied gps_time). Skipped while
    # onroad to avoid competing with openpilot. Uses qlog GPS time — never the
    # wall clock.
    if not store._is_onroad():
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, store.enrich_new_routes)
```

`import asyncio` is already present in auth.py (previously flagged as unused — now used again). Ensure it remains imported.

- [ ] **Step 7: Run the tests**

Run: `cd connect-on-device && python -m pytest tests/test_route_store.py -v`
Expected: TestHybridGpsTime PASS (4), all prior stats tests still PASS; no new failures beyond the 55 pre-existing pytest-aiohttp errors.

- [ ] **Step 8: Commit**

```bash
cd connect-on-device
git add route_store.py handlers/routes.py handlers/auth.py tests/test_route_store.py
git commit -m "fix(stats): device-supplied gps_time + qlog fallback so new drives materialize"
```

---

### Task 8: Device — capture and send GPS time

**Files:**
- Modify: `plugins/ui_mod/drive_tracker.py` — capture first GPS-fix unix time; include `gps_time` in the POST payload.
- Test: `plugins/ui_mod/tests/test_drive_tracker.py`.

**Interfaces:**
- Produces: POST payload gains optional `gps_time` (float seconds) when the drive had a GPS fix; omitted otherwise.

- [ ] **Step 1: Write failing tests** (mirror the existing `tracker_module` fixture harness in the file — do NOT use `import_module`)

Add tests that: (a) feed `tick` a mocked `sm` whose `gpsLocationExternal` has `flags=1` and `unixTimestampMillis=1_000_000_000` and assert the tracker captured `gps_time == 1_000_000.0` (first fix only — a later different timestamp must not overwrite it); (b) call `_post_stats` with a `data` dict that includes `gps_time` and assert the built `urllib.request.Request` body JSON contains `gps_time`; (c) call `_post_stats` with `data` lacking `gps_time` (None/0) and assert the payload has NO `gps_time` key. Use the file's existing GPS-tick mock shape for (a) and the existing Request/urlopen patching approach for (b)/(c).

- [ ] **Step 2: Run to verify they fail**

Run: `cd plugins && PYTHONPATH=.:plugins python -m pytest plugins/ui_mod/tests/test_drive_tracker.py -k gps_time -v`
Expected: FAIL — tracker has no `_gps_time`; payload has no `gps_time`.

- [ ] **Step 3: Capture GPS time in the tracker**

In `DriveTracker._reset`, add `self._gps_time = 0.0` alongside the other accumulator resets. In `tick`, inside the existing `if sm.updated.get('gpsLocationExternal', False):` block where `gps` is read and `flags & 1` is checked, capture the first fix's unix time:

```python
        if not self._gps_time:
          ts_ms = getattr(gps, 'unixTimestampMillis', 0)
          if ts_ms:
            self._gps_time = ts_ms / 1000.0
```

(Place this where `flags & 1` is already known true — i.e. within the existing fixed-GPS branch, so only fixed positions set it.)

- [ ] **Step 4: Add gps_time to the saved data + POST payload**

In `_save`, add to the `data` dict: `'gps_time': round(self._gps_time, 3) if self._gps_time else None`. In `_post_stats`, after building `payload` with the four stat fields, add:

```python
      gt = data.get('gps_time')
      if gt:
        payload['gps_time'] = gt
```

- [ ] **Step 5: Run the tests**

Run: `cd plugins && PYTHONPATH=.:plugins python -m pytest plugins/ui_mod/tests/test_drive_tracker.py -v`
then full suite: `cd plugins && PYTHONPATH=. python -m pytest -q`
Expected: new tests PASS; full suite no new failures vs the 252 passed/20 skipped baseline.

- [ ] **Step 6: Commit**

```bash
cd plugins
git add plugins/ui_mod/drive_tracker.py plugins/ui_mod/tests/test_drive_tracker.py
git commit -m "feat(ui_mod): capture GPS fix time and send gps_time in drive_stats POST"
```
