# Distance-Based Engagement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace COD's time-based engagement metric with a GPS-distance-based one, both at the per-route (`engagement_pct`) and aggregate (`/v1.1/devices/{id}/stats`) levels.

**Architecture:** New helper `_route_engaged_distance` walks cached `coords.json` to build a route-wide `(t_ms, cumulative_meters)` array, then interpolates cumulative distance at each engagement on/off transition recorded in `events.json`. Per-route migration uses an `engagement_metric_version` metadata field to trigger recompute lazily on first view. Aggregate stats are renamed to miles equivalents.

**Tech Stack:** Python 3, aiohttp, pytest. No new dependencies (uses stdlib `bisect`).

**Spec:** [`docs/superpowers/specs/2026-05-20-distance-based-engagement-design.md`](../specs/2026-05-20-distance-based-engagement-design.md)

---

## File Map

| File                                   | Change                                                                                    |
| -------------------------------------- | ----------------------------------------------------------------------------------------- |
| `route_helpers.py`                     | **Add** `_route_engaged_distance`; **Delete** `_route_engagement` (in Task 4 after switching callers) |
| `handlers/routes.py`                   | Switch call site at lines 228-234 to new function; add `engagement_metric_version` migration check |
| `handlers/auth.py`                     | Switch call site at lines 96-122 to new function; rename `engaged_minutes` → `engaged_miles`, `total_minutes_with_events` → `total_miles_with_engagement` |
| `API.md`                               | Update `/v1.1/devices/{id}/stats` field documentation                                     |
| `tests/test_route_helpers.py`          | Replace `TestRouteEngagement` class with `TestRouteEngagedDistance`                       |
| `tests/conftest.py`                    | Add `sample_coords_json` fixture                                                          |

---

## Task 1: Add `_route_engaged_distance` helper (TDD)

**Files:**
- Modify: `route_helpers.py` (add function, leave old `_route_engagement` in place for now)
- Modify: `tests/conftest.py` (add `sample_coords_json` fixture)
- Modify: `tests/test_route_helpers.py` (add `TestRouteEngagedDistance` class)

### - [ ] Step 1.1: Add the coords fixture to `tests/conftest.py`

Append after the `sample_events_json` fixture (around line 168):

```python
@pytest.fixture
def sample_coords_json():
    """List of (t_seconds, dist_meters) GPS samples for one segment."""
    return [
        {"t": 0.0,  "dist": 0.0,    "lat": 31.23, "lng": 121.47},
        {"t": 30.0, "dist": 500.0,  "lat": 31.24, "lng": 121.48},
        {"t": 60.0, "dist": 1000.0, "lat": 31.25, "lng": 121.49},
    ]
```

### - [ ] Step 1.2: Write the failing test class in `tests/test_route_helpers.py`

Add a new class after `TestRouteEngagement` (do not remove the old class yet). Imports at the top of the file should already include `pytest`, `json`, `Path`; add `from route_helpers import _route_engaged_distance` to the existing route_helpers import line.

```python
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
        """Key behavioral change: idle time with system on contributes 0 meters."""
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
```

### - [ ] Step 1.3: Run the new tests to verify they fail

```bash
cd /home/oxygen/catpilot-dev/connect-on-device
PYTHONPATH=. uv run pytest tests/test_route_helpers.py::TestRouteEngagedDistance -v
```

Expected: 7 failures, all with `ImportError: cannot import name '_route_engaged_distance' from 'route_helpers'`.

### - [ ] Step 1.4: Implement `_route_engaged_distance` in `route_helpers.py`

Add this function directly below the existing `_route_engagement` (do **not** remove the old one yet — Task 4 deletes it). At the top of the file, add `from bisect import bisect_left`.

```python
def _route_engaged_distance(store, route: dict) -> tuple[float, float]:
    """Compute engaged_m and total_m for a route by integrating GPS distance
    across engagement intervals.

    Walks each segment's coords.json to build route-wide (t_ms, cum_m) arrays,
    walks events.json for engagement state transitions, and sums the cumulative-
    distance delta across each on/off pair. Open engagements close at the last
    coord's t_ms. Returns (0.0, 0.0) if coords.json or events.json is missing
    for any segment, or if fewer than two GPS samples exist.
    """
    segments = sorted(route.get("_segments", []), key=lambda s: s["number"])
    if not segments:
        return (0.0, 0.0)

    # Build route-wide arrays from coords.json
    t_ms: list[float] = []
    cum_m: list[float] = []
    seg_offset = 0.0
    for seg in segments:
        seg_dir = Path(seg["path"])
        coords_path = seg_dir / "coords.json"
        events_path = seg_dir / "events.json"
        if not coords_path.exists() or not events_path.exists():
            return (0.0, 0.0)
        try:
            coords = json.loads(coords_path.read_text())
        except Exception:
            return (0.0, 0.0)
        if not coords:
            continue
        seg_last = 0.0
        for c in coords:
            t_ms.append(float(c["t"]) * 1000.0)
            cum_m.append(seg_offset + float(c["dist"]))
            seg_last = float(c["dist"])
        seg_offset += seg_last

    if len(t_ms) < 2:
        return (0.0, 0.0)

    # Collect engagement on/off intervals from events.json
    intervals: list[tuple[float, float]] = []
    open_on: float | None = None
    for seg in segments:
        events_path = Path(seg["path"]) / "events.json"
        try:
            events = json.loads(events_path.read_text())
        except Exception:
            continue
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
```

### - [ ] Step 1.5: Run the new tests to verify they pass

```bash
cd /home/oxygen/catpilot-dev/connect-on-device
PYTHONPATH=. uv run pytest tests/test_route_helpers.py::TestRouteEngagedDistance -v
```

Expected: 7 passes.

### - [ ] Step 1.6: Run the full helpers test file to confirm no regressions

```bash
PYTHONPATH=. uv run pytest tests/test_route_helpers.py -v
```

Expected: all existing tests still pass alongside the 7 new ones.

### - [ ] Step 1.7: Commit

```bash
git add route_helpers.py tests/test_route_helpers.py tests/conftest.py
git commit -m "feat(cod): add _route_engaged_distance helper

GPS-distance-based engagement metric; walks coords.json and interpolates
cumulative meters at each engagement state transition from events.json.
Idle time naturally contributes zero meters."
```

---

## Task 2: Switch per-route engagement_pct to distance + migration

**Files:**
- Modify: `handlers/routes.py:217-234`
- Modify: `tests/test_route_helpers.py` (no change — covered in Task 1)
- Test (new): integration via existing handler tests if present; otherwise rely on Task 1 unit coverage

### - [ ] Step 2.1: Read the current call site to confirm the exact lines

```bash
sed -n '215,240p' handlers/routes.py
```

Expected output shows the `events_cached` gate and the `_route_engagement` call.

### - [ ] Step 2.2: Update the import in `handlers/routes.py`

Change line 12 from:
```python
from route_helpers import _base_url, _clean_route, _resolve_local_id, _route_bookmarks, _route_engagement, _route_timeline_summary, _set_route_url
```
to:
```python
from route_helpers import _base_url, _clean_route, _resolve_local_id, _route_bookmarks, _route_engaged_distance, _route_timeline_summary, _set_route_url
```

### - [ ] Step 2.3: Replace the engagement compute block at lines 228-234

Old (lines 225-234 inclusive):
```python
    # Opportunistic: compute engagement % if events/coords are cached
    # but metadata is missing the value (e.g. after re-enrichment completed)
    if events_cached and meta:
        if meta.get("engagement_pct") is None:
            engaged_ms, total_ms = _route_engagement(store, route)
            if total_ms > 0 and engaged_ms > 0:
                meta["engagement_pct"] = round(engaged_ms / total_ms * 100)
                store._rebuild_routes()
                store._save_metadata()
                route = store.get_route(route_name)
```

New:
```python
    # Opportunistic: compute distance-based engagement % if coords + events are
    # cached. Migration: legacy time-based values (no engagement_metric_version)
    # are recomputed.
    if events_cached and meta:
        needs_compute = (
            meta.get("engagement_pct") is None
            or meta.get("engagement_metric_version") != 2
        )
        if needs_compute:
            engaged_m, total_m = _route_engaged_distance(store, route)
            if total_m > 0:
                meta["engagement_pct"] = round(engaged_m / total_m * 100)
                meta["engagement_metric_version"] = 2
                store._rebuild_routes()
                store._save_metadata()
                route = store.get_route(route_name)
```

### - [ ] Step 2.4: Run the handlers test file (if it exists) and the helpers tests

```bash
PYTHONPATH=. uv run pytest tests/test_route_helpers.py tests/test_route_store.py -v
```

Expected: all pass. (Handler-level integration of this branch is exercised indirectly through Task 1's unit tests; per the spec we are not adding a new integration test for the migration check.)

### - [ ] Step 2.5: Commit

```bash
git add handlers/routes.py
git commit -m "feat(cod): per-route engagement_pct now distance-based

Add engagement_metric_version=2 migration key; legacy time-based values
are recomputed lazily on next route view. No longer skips routes where
engaged_m=0 but the route had GPS coverage."
```

---

## Task 3: Switch aggregate stats to miles + rename fields

**Files:**
- Modify: `handlers/auth.py:9` (import) and `:96-122` (handler body)
- Modify (if existing): `tests/test_handlers_auth.py` or similar — locate by grep

### - [ ] Step 3.1: Check for existing handler tests of `/v1.1/devices/{id}/stats`

```bash
grep -rn "engaged_minutes\|total_minutes_with_events\|/stats" tests/ 2>/dev/null
```

If any test references `engaged_minutes`, those assertions must be updated to `engaged_miles` and `total_miles_with_engagement`. Note the file paths from the output; edits in Step 3.4 apply only if a match exists.

### - [ ] Step 3.2: Update the import in `handlers/auth.py`

Change line 9 from:
```python
from route_helpers import _route_engagement
```
to:
```python
from route_helpers import _route_engaged_distance
```

### - [ ] Step 3.3: Replace the aggregation block at lines 96-122 in `handlers/auth.py`

Old (lines 96-122 inclusive):
```python
    all_stats = {"distance": 0.0, "minutes": 0, "routes": 0, "engaged_minutes": 0.0, "total_minutes_with_events": 0}
    week_stats = {"distance": 0.0, "minutes": 0, "routes": 0, "engaged_minutes": 0.0, "total_minutes_with_events": 0}

    for r in routes.values():
        minutes = len(r["_segments"])  # ~1 min per segment
        distance = r.get("distance") or 0
        engaged_ms, total_ms = _route_engagement(store, r)

        all_stats["routes"] += 1
        all_stats["minutes"] += minutes
        all_stats["distance"] += distance
        if total_ms > 0 and engaged_ms > 0:
            all_stats["engaged_minutes"] += engaged_ms / 60_000
            all_stats["total_minutes_with_events"] += total_ms / 60_000

        if r.get("create_time", 0) >= week_ago:
            week_stats["routes"] += 1
            week_stats["minutes"] += minutes
            week_stats["distance"] += distance
            if total_ms > 0 and engaged_ms > 0:
                week_stats["engaged_minutes"] += engaged_ms / 60_000
                week_stats["total_minutes_with_events"] += total_ms / 60_000

    for s in (all_stats, week_stats):
        s["distance"] = round(s["distance"], 1)
        s["engaged_minutes"] = round(s["engaged_minutes"], 1)
        s["total_minutes_with_events"] = round(s["total_minutes_with_events"], 1)
```

New:
```python
    all_stats = {"distance": 0.0, "minutes": 0, "routes": 0, "engaged_miles": 0.0, "total_miles_with_engagement": 0.0}
    week_stats = {"distance": 0.0, "minutes": 0, "routes": 0, "engaged_miles": 0.0, "total_miles_with_engagement": 0.0}

    for r in routes.values():
        minutes = len(r["_segments"])  # ~1 min per segment
        distance = r.get("distance") or 0
        engaged_m, total_m = _route_engaged_distance(store, r)

        all_stats["routes"] += 1
        all_stats["minutes"] += minutes
        all_stats["distance"] += distance
        if total_m > 0 and engaged_m > 0:
            all_stats["engaged_miles"] += engaged_m / 1609.344
            all_stats["total_miles_with_engagement"] += total_m / 1609.344

        if r.get("create_time", 0) >= week_ago:
            week_stats["routes"] += 1
            week_stats["minutes"] += minutes
            week_stats["distance"] += distance
            if total_m > 0 and engaged_m > 0:
                week_stats["engaged_miles"] += engaged_m / 1609.344
                week_stats["total_miles_with_engagement"] += total_m / 1609.344

    for s in (all_stats, week_stats):
        s["distance"] = round(s["distance"], 1)
        s["engaged_miles"] = round(s["engaged_miles"], 1)
        s["total_miles_with_engagement"] = round(s["total_miles_with_engagement"], 1)
```

### - [ ] Step 3.4: Update any existing handler tests found in Step 3.1

If Step 3.1 found references to `engaged_minutes` or `total_minutes_with_events` in `tests/`, replace each with `engaged_miles` / `total_miles_with_engagement`. Update any expected-value assertions: scale was `ms/60_000`, now is `m/1609.344` — values change accordingly. If no test files reference these fields, skip this step.

### - [ ] Step 3.5: Run the full test suite to confirm nothing else broke

```bash
PYTHONPATH=. uv run pytest tests/ -v
```

Expected: all pass.

### - [ ] Step 3.6: Commit

```bash
git add handlers/auth.py
# if Step 3.4 modified tests:
# git add tests/...
git commit -m "feat(cod): aggregate stats use distance-based engagement

Rename engaged_minutes -> engaged_miles and total_minutes_with_events ->
total_miles_with_engagement in /v1.1/devices/{id}/stats. Breaking API
change documented in API.md."
```

---

## Task 4: Remove legacy `_route_engagement` and its tests

**Files:**
- Modify: `route_helpers.py` (delete the old function)
- Modify: `tests/test_route_helpers.py` (delete `TestRouteEngagement` class)

### - [ ] Step 4.1: Confirm no remaining callers

```bash
grep -rn "_route_engagement\b" --include="*.py" .
```

Expected: matches only inside `tests/test_route_helpers.py` (the legacy `TestRouteEngagement` class) and possibly inside `route_helpers.py` itself. No matches in `handlers/`.

If anything else matches, stop and investigate before deleting.

### - [ ] Step 4.2: Delete `_route_engagement` from `route_helpers.py`

Remove lines 47-84 (the entire `_route_engagement` function and its docstring).

### - [ ] Step 4.3: Delete `TestRouteEngagement` from `tests/test_route_helpers.py`

Remove the entire `class TestRouteEngagement:` block (the legacy section header comment too: `# ─── _route_engagement ─────`). Update the imports at the top of the file to remove `_route_engagement`.

### - [ ] Step 4.4: Run the full test suite

```bash
PYTHONPATH=. uv run pytest tests/ -v
```

Expected: all pass; test count drops by the number of removed legacy tests (4).

### - [ ] Step 4.5: Commit

```bash
git add route_helpers.py tests/test_route_helpers.py
git commit -m "refactor(cod): remove legacy time-based _route_engagement

All callers now use _route_engaged_distance."
```

---

## Task 5: Update API.md docs

**Files:**
- Modify: `API.md:56-65` (stats endpoint schema)
- Modify: `API.md:160` (route engagement_pct context)

### - [ ] Step 5.1: Update the stats schema example

Replace lines 56-65 (the `/v1.1/devices/{dongleId}/stats` section). Find this block:

```markdown
### `GET /v1.1/devices/{dongleId}/stats`
Driving statistics with engagement breakdown.

...
{
  "all":  {"distance": 1234.5, "minutes": 420, "routes": 15, "engaged_minutes": 280.3, "total_minutes_with_events": 350.0},
  "week": {"distance": 120.0,  "minutes": 45,  "routes": 3,  "engaged_minutes": 30.5,  "total_minutes_with_events": 40.0}
...
```

Replace the JSON example with:

```json
{
  "all":  {"distance": 1234.5, "minutes": 420, "routes": 15, "engaged_miles": 1050.2, "total_miles_with_engagement": 1180.0},
  "week": {"distance": 120.0,  "minutes": 45,  "routes": 3,  "engaged_miles": 95.7,   "total_miles_with_engagement": 110.0}
}
```

Add a sentence immediately after the description "Driving statistics with engagement breakdown.":

> Engagement is measured by GPS distance: `engaged_miles` is the sum of cumulative distance traveled while `selfdriveState.enabled` was true, computed by interpolating the cached `coords.json` track at each engagement on/off transition. `total_miles_with_engagement` is the sum of route distance across routes that had any engagement, providing the denominator for engaged %.

### - [ ] Step 5.2: Touch up the per-route description around line 142 and 160

Around line 142 (`Single route with full metadata. Triggers on-demand enrichment...`), the existing text says "GPS extraction, engagement computation". No change needed — engagement is still computed, just by distance.

Around line 160 (`"engagement_pct": 85,`), add a brief inline note:

```markdown
"engagement_pct": 85,   // distance-based: engaged_meters / total_route_meters * 100
```

### - [ ] Step 5.3: Verify the file parses as valid markdown (visual scan only)

```bash
sed -n '50,70p' API.md
sed -n '155,165p' API.md
```

Expected: the rendered sections show the new field names and the explanatory sentences.

### - [ ] Step 5.4: Commit

```bash
git add API.md
git commit -m "docs(cod): document distance-based engagement in stats + route schemas"
```

---

## Task 6: Final verification

### - [ ] Step 6.1: Run the entire test suite

```bash
cd /home/oxygen/catpilot-dev/connect-on-device
PYTHONPATH=. uv run pytest tests/ -v
```

Expected: all tests pass. No skipped tests for the new helper.

### - [ ] Step 6.2: Confirm no lingering references to legacy names

```bash
grep -rn "_route_engagement\b\|engaged_minutes\|total_minutes_with_events" \
  --include="*.py" --include="*.md" .
```

Expected: zero matches. (The spec doc may match if it quoted the old names — that's documentation and stays.)

### - [ ] Step 6.3: Verify the deploy bundle is clean

```bash
git status
git log --oneline -7
```

Expected: working tree clean; 5 new commits on `dev` (one per Task 1-5). No uncommitted changes.

### - [ ] Step 6.4: Push to `origin/dev`

```bash
git push origin dev
```

Expected: push succeeds. Deploy to C3 is a separate manual step (per memory: `ssh c3 'cd /data/connect-on-device && git fetch origin dev && git reset --hard origin/dev'`) — not part of this plan, user-triggered.
