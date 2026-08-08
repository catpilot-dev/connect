# Connect on Device — API Reference

Base URL: `http://<device-ip>:8082`

All endpoints return JSON unless noted otherwise. Route names use `|` as separator in URLs (e.g., `dongleId|2026-02-20--10-47-46`).

---

## Auth

### `GET /v1/me/`
User profile. Returns static local user.

**Response:**
```json
{
  "id": "local",
  "email": "local@device",
  "prime": false,
  "username": "local"
}
```

### `POST /v2/auth/`
Auth token. Returns a dummy token for local use.

**Response:**
```json
{"access_token": "local-device-token"}
```

---

## Device

### `GET /v1/me/devices/`
List devices. Forces a route rescan.

**Response:** Array with one device object.

### `GET /v1.1/devices/{dongleId}/`
Single device info.

**Response:**
```json
{
  "dongle_id": "abc123",
  "device_type": "three",
  "agnos_version": "0.9.11",
  "is_paired": true,
  "is_owner": true,
  "prime": false
}
```

### `GET /v1.1/devices/{dongleId}/stats`
Driving statistics with engagement breakdown.

Engagement is measured by GPS distance: `engaged_miles` is the sum of cumulative distance traveled while `selfdriveState.enabled` was true, computed by interpolating the cached `coords.json` track at each engagement on/off transition. `total_miles_with_engagement` is the sum of route distance across routes that had any engagement, providing the denominator for engaged %.

**Response:**
```json
{
  "all":  {"distance": 1234.5, "minutes": 420, "routes": 15, "engaged_miles": 1050.2, "total_miles_with_engagement": 1180.0},
  "week": {"distance": 120.0,  "minutes": 45,  "routes": 3,  "engaged_miles": 95.7,   "total_miles_with_engagement": 110.0}
}
```

### `GET /v1/devices/{dongleId}/location`
Last known GPS position from the most recent route with GPS data.

**Response:**
```json
{
  "dongle_id": "abc123",
  "lat": 31.2304,
  "lng": 121.4737,
  "time": 1740000000
}
```

### `GET /v1/storage`
Disk usage and route management stats.

**Response:**
```json
{
  "total": 53687091200,
  "used": 32000000000,
  "free": 21687091200,
  "route_count": 15,
  "preserved_count": 3,
  "hidden_count": 1
}
```

---

## Routes

### `GET /v1/devices/{dongleId}/routes`
Paginated route list, sorted by route counter (newest first).

**Query params:**
| Param | Default | Description |
|-------|---------|-------------|
| `limit` | 25 | Max routes to return |
| `before_counter` | 999999999 | Pagination cursor (route counter) |

**Response:** Array of route objects with `route_counter` and `is_preserved` fields.

### `GET /v1/devices/{dongleId}/routes_segments`
Route detail with segment timing data. Triggers on-demand enrichment when filtering by route.

**Query params:**
| Param | Default | Description |
|-------|---------|-------------|
| `route_str` | _(none)_ | Filter to specific route fullname |
| `limit` | 100 | Max routes |

**Response:** Array of route objects with additional segment fields:
```json
{
  "fullname": "abc123/2026-02-20--10-47-46",
  "segment_numbers": [0, 1, 2, 3],
  "segment_start_times": [1740000000000, ...],
  "segment_end_times":   [1740000060000, ...],
  "start_time_utc_millis": 1740000000000,
  "end_time_utc_millis": 1740000240000,
  "is_preserved": false
}
```

### `GET /v1/devices/{dongleId}/routes/preserved`
List all preserved (starred) routes.

**Response:** Array of route objects with `is_preserved: true`.

---

## Route Detail

### `GET /v1/route/{routeName}/`
Single route with full metadata. Triggers on-demand enrichment (GPS extraction, engagement computation, reverse geocoding).

**Response:**
```json
{
  "fullname": "abc123/2026-02-20--10-47-46",
  "dongle_id": "abc123",
  "create_time": 1740000000,
  "start_time": "2026-02-20T10:47:46",
  "end_time": "2026-02-20T10:51:46",
  "maxqlog": 3,
  "distance": 5.2,
  "start_lat": 31.2304,
  "start_lng": 121.4737,
  "end_lat": 31.2350,
  "end_lng": 121.4800,
  "start_address": "Zhongshan Rd",
  "end_address": "Nanjing Rd",
  "engagement_pct": 85,
  "git_commit": "abc1234",
  "device_type": "tici",
  "is_preserved": false,
  "url": "http://192.168.1.100:8082/connectdata/abc123/2026-02-20--10-47-46",
  "bookmarks": [
    {"time": 45.2, "type": "altButton2", "segment": 0, "offset": 45.2}
  ]
}
```

`engagement_pct` is distance-based: `round(engaged_meters / total_route_meters * 100)`.

### `DELETE /v1/route/{routeName}/`
Soft-delete (hide) a route. Can be undone by removing from hidden list.

**Response:**
```json
{"success": 1}
```

### `POST /v1/route/{routeName}/enrich`
Force re-enrichment: clears cached coords.json/events.json and re-parses from rlog.

**Response:** Route object with `cleared_files` list.

### `GET /v1/route/{routeName}/files`
List available files per segment.

**Response:**
```json
{
  "cameras":  ["http://.../0/fcamera.hevc", "http://.../1/fcamera.hevc"],
  "ecameras": ["http://.../0/ecamera.hevc", ""],
  "dcameras": ["", ""],
  "logs":     ["http://.../0/rlog.zst", "http://.../1/rlog.zst"],
  "qcameras": ["http://.../0/qcamera.ts", "http://.../1/qcamera.ts"],
  "qlogs":    ["http://.../0/qlog.zst", ""]
}
```
Empty string means that file is not available for that segment.

### `GET /v1/route/{routeName}/share_signature`
Dummy share signature for local use.

**Response:**
```json
{"exp": "9999999999", "sig": "local"}
```

---

## Route Actions

### `POST /v1/route/{routeName}/note`
Set or update a route note.

**Body:**
```json
{"note": "Great highway test drive"}
```

**Response:**
```json
{"status": "ok"}
```

### `POST /v1/route/{routeName}/preserve`
Mark route as preserved (starred). Preserved routes are protected from cleanup.

**Response:**
```json
{"success": 1}
```

### `DELETE /v1/route/{routeName}/preserve`
Remove preservation.

**Response:**
```json
{"success": 1}
```

### `GET /v1/route/{routeName}/download`
Download route data as a streaming tar.gz archive.

**Query params:**
| Param | Default | Description |
|-------|---------|-------------|
| `files` | `rlog` | Comma-separated file types: `rlog`, `qcamera`, `fcamera`, `ecamera`, `qlog` |
| `segments` | _(all)_ | Comma-separated segment numbers: `0,1,2` |

**Response:** `application/gzip` — tar.gz archive of requested files.

**Example:** `GET /v1/route/abc123|2026-02-20--10-47-46/download?files=rlog,qcamera&segments=0,1`

---

## Frame Extraction

Both endpoints extract a full-resolution frame (1928x1208) from fcamera.hevc and embed rich EXIF metadata.

### `GET /v1/route/{routeName}/frame?t=123.45`
URL-friendly frame extraction. Open in browser or use in `<img>` tags.

**Query params:**
| Param | Default | Description |
|-------|---------|-------------|
| `t` | `0` | Time in seconds from route start |

**Response:** `image/jpeg` with EXIF metadata. Cached for 24h (`Cache-Control: public, max-age=86400`).

**Example:** `GET /v1/route/abc123|2026-02-20--10-47-46/frame?t=75.30`

### `POST /v1/route/{routeName}/screenshot`
Screenshot with download disposition (triggers browser download).

**Body:**
```json
{"time": 123.45}
```

**Response:** `image/jpeg` with `Content-Disposition: attachment; filename="2026-02-20--10-47-46_02m03s.jpg"`.

### EXIF Metadata

Both frame endpoints embed the following EXIF data:

| Field | Source | Description |
|-------|--------|-------------|
| GPS Latitude/Longitude | coords.json | WGS84 decimal degrees |
| GPS Direction | Computed | True-north bearing from consecutive GPS points |
| GPS Speed | coords.json | km/h |
| DateTimeOriginal | create_time + t | UTC timestamp |
| ImageDescription | Route ref | `{dongle_id}/{route_date}/{local_id}/{segment}/{offset}` |
| UserComment | JSON | Structured metadata (see below) |

**UserComment JSON structure:**
```json
{
  "route": "abc123/2026-02-20--10-47-46/00000042--abcdef1234/2/15.30",
  "camera": {
    "model": "AR0231",
    "width": 1928,
    "height": 1208,
    "focal_length_px": 2648.0,
    "hfov_deg": 40.0,
    "vfov_deg": 25.7
  },
  "pose": {
    "height_m": 1.22,
    "pitch_deg": -1.234,
    "yaw_deg": 0.567,
    "roll_deg": 0.012
  },
  "speed_ms": 16.67,
  "bearing_deg": 135.0
}
```

**Notes:**
- First request for a segment is slower (~1.5s) due to one-time HEVC→MP4 muxing
- Subsequent requests for the same segment seek directly in the cached MP4
- Camera intrinsics are from the tici AR0231 sensor (fixed)
- Camera pose comes from liveCalibration in rlog (cached per segment)

---

## HUD Video Rendering

Pre-render the openpilot HUD overlay to an MP4 video using the device's replay binary.

### `POST /v1/route/{routeName}/hud/prerender`
Start a HUD video render job.

**Body:**
```json
{
  "start": 0,
  "end": 240,
  "quality": "high"
}
```

| Param | Default | Description |
|-------|---------|-------------|
| `start` | `0` | Start time in seconds |
| `end` | full route | End time in seconds |
| `quality` | _(none)_ | Preset: `high` (native 2160x1080, 20fps), `medium` (1080x540, 20fps), `low` (1080x540, 10fps) |
| `scale` | _(none)_ | Custom ffmpeg scale filter, e.g. `"1080:540"` |
| `fps` | `20` | Output framerate |

**Response:**
```json
{
  "status": "rendering",
  "elapsed_sec": 0,
  "total_sec": 240,
  "estimated_mb": 90,
  "wall_duration": 1230
}
```

### `GET /v1/route/{routeName}/hud/progress`
Poll render progress.

**Response:**
```json
{
  "status": "rendering",
  "elapsed_sec": 120,
  "total_sec": 240
}
```
Status values: `idle`, `rendering`, `complete`, `error`.

### `POST /v1/route/{routeName}/hud/cancel`
Cancel a running render job.

**Response:**
```json
{"status": "cancelled"}
```

### `GET /v1/route/{routeName}/hud/video`
Download the rendered HUD MP4 video.

**Response:** `video/mp4` file. Returns 404 if render is not complete.

---

## HUD Live Streaming

Stream the openpilot HUD in real-time via HLS. Only available on C3 devices with wayland screenshooter.

### `POST /v1/hud/stream/start`
Start live HUD streaming for a route.

**Body:**
```json
{
  "route": "abc123|2026-02-20--10-47-46",
  "start": 0
}
```

**Response:** Stream status object.

### `POST /v1/hud/stream/stop`
Stop the live stream and restore the compositor.

**Response:**
```json
{"status": "idle"}
```

### `GET /v1/hud/stream/status`
Check streaming pipeline status.

**Response:** Stream status object with `status`, `route`, timing info.

### `GET /v1/hud/stream/{filename}`
Serve HLS playlist (.m3u8) and segment (.ts) files.

- `.m3u8` — `application/vnd.apple.mpegurl`, no-cache
- `.ts` — `video/mp2t`, cached 60s

---

## WebRTC

### `POST /api/webrtc`
Proxy WebRTC signaling to local webrtcd (port 5001) for live camera streaming.

**Body:** WebRTC signaling JSON (forwarded as-is).

**Response:** webrtcd response JSON.

---

## HUD WebSocket

### `WS /ws/hud`
WebSocket endpoint streaming server-rendered HUD overlay images at 20Hz.

**Protocol:** Binary frames (WebP images) sent at 50ms intervals.

---

## Media Files

### `GET /connectdata/{dongleId}/{routeDate}/{segment}/{filename}`
Serve raw media files from route data.

**Allowed files:** `fcamera.hevc`, `ecamera.hevc`, `dcamera.hevc`, `qcamera.ts`, `rlog.zst`, `rlog`, `qlog.zst`, `qlog`, `sprite.jpg`

**Derived files** (generated on demand from rlog, cached to disk):
- `coords.json` — GPS coordinates with timestamps
- `events.json` — Driving events (engagements, disengagements, alerts)

**Special path — HUD frame:**
`GET /connectdata/{dongleId}/{routeDate}/{segment}/hud?t=15000`
Returns a rendered HUD overlay frame at time `t` (milliseconds within segment). Response: `image/webp`.

**Content types:**
| Extension | Type |
|-----------|------|
| `.ts` | `video/mp2t` |
| `.hevc` | `video/hevc` |
| `.zst` | `application/zstd` |
| `.jpg` | `image/jpeg` |

---

## Health

### `GET /health`
Server health check.

**Response:**
```json
{"status": "ok"}
```

---

## Stubs

These endpoints return empty/error responses for frontend compatibility:

| Endpoint | Response |
|----------|----------|
| `GET /v1/devices/{dongleId}/athena_offline_queue` | `[]` |
| `GET /v1/devices/{dongleId}/bootlogs` | `[]` |
| `GET /v1/devices/{dongleId}/crashlogs` | `[]` |
| `GET /v1/devices/{dongleId}/users` | `[]` |
| `GET /v1/devices/{dongleId}/firehose_stats` | 501 |
| `POST /v1/devices/{dongleId}/unpair` | 501 |
| `GET /v1/prime/subscription` | 501 |
| `GET /v1/prime/subscribe_info` | 501 |

---

## Notes

- **CORS**: All responses include `Access-Control-Allow-Origin: *`
- **Route name encoding**: Use `|` instead of `/` in URL path segments (e.g., `abc123|2026-02-20--10-47-46`)
- **SPA fallback**: Any unmatched GET request serves the frontend `index.html`
- **Enrichment**: Routes are enriched on-demand (GPS, geocoding, engagement) when first viewed
- **Thread pool**: rlog parsing uses a single-worker executor to bound memory (one rlog decompression at a time)
