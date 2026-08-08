# GPS-Anchored, Materialized Drive Stats

**Date:** 2026-07-03
**Status:** Design — awaiting review
**Scope:** `connect-on-device` (primary), `plugins/ui_mod` (device-side POST)

## Problem

The offroad home "Past 7 Days" panel (rendered by `ui_mod/drive_stats.py`) showed
all-time totals instead of the 7-day window — screenshot `capture_20260703_050212.png`
displayed `903 km / 60 h / 137 drives / 84%` when the true 7-day figures were
`73 km / 2 h / 5 drives / 86%`.

### Root cause

`handlers/auth.py::handle_device_stats` builds the week bucket with:

```python
week_ago = time.time() - 7 * 86400
...
if r.get("create_time", 0) >= week_ago:   # route counted in "week"
```

On AGNOS the system wall clock is seeded from the **OS build time**, not a real-time
clock or a synced NTP value. Near boot (before/without NTP sync) `time.time()` reads a
time earlier than the device's oldest route. `week_ago` then falls before every route's
GPS-derived `create_time`, so the 7-day filter matches all routes and the `week` bucket
collapses into `all`. Reproduced against the live store:

```
week, correct clock      : 5 routes,  2 h,  73 km   (truth)
week, stale build-time    : 136 routes, 60 h, 896 km  (matches screenshot)
all                       : 136 routes, 60 h, 896 km
```

### Secondary defect

Engagement is re-derived per request via `route_helpers._route_engaged_distance`, which
reads each segment's `coords.json` + `events.json`. Those files exist only after **on-click
full enrichment**. So the aggregate silently excludes un-enriched routes from the engaged
ratio and re-parses logs on every stats request.

## Principles

1. **GPS time only.** Never read the system wall clock (`time.time()`) for any stats
   computation. On AGNOS it is build-time-seeded and untrustworthy.
2. **Materialize on drive-end.** Brief per-drive stats are computed once, when the drive
   ends (offroad), and pushed to COD. The aggregate is recomputed when the newest route
   arrives, not lazily re-derived at fetch time.
3. **No full-enrichment dependency.** The aggregate reads brief per-route stats, never the
   on-click `coords.json`/`events.json`. `_route_engaged_distance` remains only for the
   on-click detail view.

## Architecture

### 1. Device — `plugins/ui_mod/drive_tracker.py`

`DriveTracker._save()` already accumulates `{distance_m, duration_s, engaged_m, engaged_s}`
from live cereal at the offroad transition (no wall-clock dependence for the values).

Change: in addition to writing `.last_drive.json`, POST the four numbers to COD, tagged
with the route that just ended.

- **Route identity:** the just-finished route's `local_id` is the basename of the newest
  `/data/media/0/realdata/<route>--<seg>` directory with the `--<seg>` suffix stripped
  (e.g. `00000385--6e363981a3`). The tracker resolves it at save time.
- **Delivery:** best-effort `POST` on a daemon thread (mirroring the existing
  fire-and-forget fetch pattern). Failure is non-fatal — `.last_drive.json` is still
  written, and COD can be re-POSTed on a later transition. No retry queue in v1.

### 2. COD — new endpoint

`POST /v1/routes/{routeName}/drive_stats`

- `routeName` is the route `local_id` (`|`-escaped like other route routes).
- Body: `{"distance_m": float, "duration_s": float, "engaged_m": float, "engaged_s": float}`.
- Handler validates the body, stores the record into `metadata.json` under the route's
  entry as `drive_stats: {distance_m, duration_s, engaged_m, engaged_s}`, then triggers an
  aggregate recompute + persist. Returns `{"status": "ok"}`.
- If the route is not yet scanned, the record is still persisted keyed by `routeName`; the
  aggregation joins by `local_id`, so POST/scan ordering does not matter.

### 3. COD — aggregation (`route_store.py`)

Add `RouteStore._compute_stats()` and call it at the end of `_rebuild_routes()` (the
"latest route arrives" point) and after a `drive_stats` POST. Result cached on
`self._stats` and persisted into `metadata.json` (`stats` key) so it survives restarts and
is served with zero computation.

```
reference_now = max(create_time over routes)      # newest route's GPS time; 0 if none
week_ago      = reference_now - 7 * 86400
```

For each route, source values with brief-first precedence:

| field       | primary (brief)          | fallback                              |
|-------------|--------------------------|---------------------------------------|
| distance_m  | `drive_stats.distance_m` | route `distance_m` (GPS-integrated)   |
| minutes     | `drive_stats.duration_s`/60 | `len(_segments)` (~1 min/segment)  |
| engaged_m   | `drive_stats.engaged_m`  | — (excluded from ratio if absent)     |
| total_m_eng | `drive_stats.distance_m` | — (excluded from ratio if absent)     |

- A route contributes to the engaged ratio **only** if it has brief `drive_stats`
  (`engaged_m` and `distance_m > 0`) — same "engaged-eligible" intent as today, now driven
  by presence of a brief record rather than presence of enrichment files.
- `week` bucket includes a route iff `create_time >= week_ago`.
- Engaged % = `Σ engaged_m / Σ distance_m` (distance-based), per bucket.

### 4. COD — `handle_device_stats`

Reduces to: `await store.async_scan()`; return the cached `store._stats` (recomputing once
if the cache is empty). No `time.time()`, no `_route_engaged_distance`. Same JSON shape as
today (`{"all": {...}, "week": {...}}`), so `drive_stats.py` needs no change.

## Data flow

```
drive ends → DriveTracker._save() computes brief stats
          → POST /v1/routes/<local_id>/drive_stats
          → COD stores drive_stats in metadata.json[route]
          → COD._compute_stats() (GPS-anchored) → self._stats + metadata.json[stats]
offroad home → drive_stats.py GET /v1.1/devices/<id>/stats → cached self._stats
```

## Error handling

- Device POST failure: swallowed; `.last_drive.json` still written; retried on next
  offroad transition. No user-visible error.
- Malformed POST body: `400`, no state change.
- No routes / empty store: `reference_now = 0`, both buckets zero — never divides by zero
  (`total_m_eng == 0 → "—"`, preserved from current renderer).
- Routes lacking brief stats: counted for distance/minutes where known, excluded from the
  engaged ratio. As devices accumulate POSTed drives, coverage becomes complete.

## Testing

- `_compute_stats` with mocked `time.time()` set to a stale build-time value: assert the
  result is **independent** of the wall clock, `week != all`, and `week` contains only
  routes with `create_time` within 7 days of the newest route's GPS time. This is the
  regression test for the reported bug.
- Distance-based engaged %: `Σengaged_m / Σdistance_m` over the bucket, brief records only.
- Empty store and single-route store: no divide-by-zero, buckets sane.
- `POST /v1/routes/{routeName}/drive_stats`: persists to metadata, updates `self._stats`,
  order-independent vs scan, `400` on bad body.

## Out of scope (YAGNI)

- Retry/queue for failed POSTs (best-effort; next drive re-POSTs).
- Backfilling brief stats for historical routes (they age out of the 7-day window; all-time
  engaged ratio converges as new drives are POSTed).
- Configurable window length (fixed 7 days).
```
