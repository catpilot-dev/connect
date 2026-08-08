# COD — Design & Implementation

COD is a single aiohttp server (`server.py`) running on the comma device,
serving a comma-API-compatible REST surface plus a prebuilt Svelte 5 SPA from
`static/`. It binds port 80 directly (`setup_service.sh` grants
`cap_net_bind_service` to the venv Python) and is advertised over mDNS as
`catpilot.local`. Everything is offline: routes are read from
`/data/media/0/realdata`, and no request ever leaves the device except
explicit update checks and map-tile downloads.

The API is shaped like comma's cloud API (`/v1/...`) so the frontend — and
any tooling written against connect — works against local data. `API.md`
documents every endpoint.

## Project structure

```
connect-on-device/
├── server.py               # aiohttp entry point, route table, lifecycle
├── route_store.py          # Route discovery, metadata cache, enrichment
├── route_helpers.py        # Derived route data (bookmarks, engagement)
├── log_parser.py           # qlog/rlog parsing: metadata, coords, events,
│                           #   frame times, bookmark epochs
├── storage_management.py   # Disk reclaim phases + cache cleanups
├── screenshot_worker.py    # Offroad extraction of onroad screen-capture taps
├── render_clip_headless.py # Headless HUD renderer (clip + screenshot modes)
├── render_clip_drm.py      # Legacy DRM render fallback
├── cod_recorder.py         # ffmpeg frame encoder for HUD clip rendering
├── screencast.py           # Live C3 display casting
├── tile_manager.py         # Offline OSM tile download/delete
├── health.py               # Startup + periodic self-checks
├── handlers/               # REST API handlers
│   ├── middleware.py       # CORS + onroad safety guard
│   ├── routes.py           # Route CRUD, enrichment, bookmarks
│   ├── media.py            # Video streaming, frame extraction, frame_times
│   ├── hud.py              # HUD prerender/progress/video, screencast
│   ├── screenshots.py      # Captures list/serve/delete, by-time matching
│   ├── signals.py          # CAN/cereal signal browser
│   ├── updates.py          # Channel-aware COD + plugins update flow
│   ├── software.py         # openpilot update lifecycle
│   ├── models.py           # Driving model management
│   ├── mapd.py             # mapd binary + tile endpoints
│   ├── params.py           # Device params & toggles
│   └── ssh_keys.py         # SSH key management
├── frontend/               # Svelte 5 + Vite + Tailwind (source)
├── static/                 # Built frontend (gitignored; served by aiohttp)
├── tests/                  # pytest suite (isolated params via conftest)
├── make_release.sh         # Channel release tarball builder
├── deploy_dev.sh           # Build frontend + rsync to device + restart
└── setup_service.sh        # On-device service bootstrap (port 80, mDNS)
```

## Route store & enrichment

`route_store.py` scans `realdata` segment directories and keeps route
metadata in `.route_metadata.json` next to them. Enrichment is on-demand:
`enrich_new_routes()` (triggered by the first route-list view) parses each
new route's seg-0 qlog head for initData + first GPS fix, and
`ensure_enriched()` runs the full parse when a route is opened. Enrichment
rebuilds a route's metadata entry from the fresh log parse, so fields owned
by the user or background workers — `notes`, `bookmarks`, `drive_stats`,
`hud_capture_state`, `drive_bookmarks_imported` — are explicitly carried
over (`_preserve_user_fields`). Anything added to worker/user state must be
listed there or enrichment will eat it.

`create_time` is the epoch of the route's **first GPS fix**, not the true
route start — anything needing exact wall-clock times must convert
mono→wall against a GPS reference itself (see frame times and bookmark
epochs in `log_parser.py`).

## Timebase: frame-exact video ↔ log alignment

The muxed HEVC streams are fixed 20 fps, so video-time → frame-index is
exact by construction. `log_parser.extract_frame_times()` maps each encoded
frame to a wall-clock epoch via `roadEncodeIdx` → frameId →
`roadCameraState.timestampSof`, converted mono→wall with the segment's GPS
fix. The player, scrubber, and HUD overlays all share this timebase; no fps
assumptions anywhere else.

## HUD rendering

`render_clip_headless.py` wraps catpilot's `tools/clip/run.py clip()` with a
patched raylib (GLFW null platform) so it renders without DRM master while
the live UI keeps running. COD owns the frame encoder (`cod_recorder.py`) and
registers its own `ui.post_end_drawing` capture hook; plugin UI overlays are
loaded from an allow-list of hooks so rendered video matches what the driver
saw. `--screenshot-at <sec>` is the single-frame mode: replay a ~3 s window,
PNG-export the render texture at the target frame, no ffmpeg, no burned-in
clip overlays.

## Screen-capture extraction

Onroad taps of the screen_capture plugin publish only a bookmark; COD's
`screenshot_worker.py` produces the PNGs afterwards — offroad-only cycles,
latest-route-only qlog scans, serial single-frame renders, exact tap epochs,
drive-local filenames, and per-tap state in route metadata
(`hud_capture_state`). The full contract — including why the onroad path
must never touch GPU or disk — is documented in the plugins repo:
`plugins/screen_capture/DESIGN.md`.

## Onroad safety guard

`handlers/middleware.py` refuses (HTTP 409) mutating requests that could
affect a moving car — reboot/poweroff, software/model/plugin changes,
screencast, HUD prerender — whenever `IsOnroad` is set. The UI hides these
controls too, but the middleware is the enforcement: a page opened while
parked goes stale, and the API is reachable directly. COD shall never affect
a moving car.

## Storage management

`storage_management.run_cleanup()` (5-minute cycle, offroad work only)
reclaims disk in phases: recycle bin first, then normal routes oldest-first
when free space drops below 20 GB, COD-saved routes only in emergencies
(<10 GB). xattr-preserved routes (comma cloud) are never touched. Routes
with bookmarks sort last among normal candidates so tap footage survives
until the screenshot worker has extracted it. Caches (HUD renders, media,
screenshots) have their own size caps.

## Updates & releases

COD self-updates from GitHub releases, **channel-aware**: the checker lists
releases and picks the newest tag matching the local VERSION's `X.Y.Z`
prefix (bootstrap `vX.Y.Z`, rolling `vX.Y.Z-YYYY.MM.DD`) — never the
globally latest release, which may target a different catpilot base.
Plugins update by git against the branch matching catpilot's.
`make_release.sh [--rolling]` builds the release tarball from git-tracked
sources plus a fresh frontend build, in the single-directory layout that
both `first_boot_setup.sh` and the self-update apply path expect. The full
cross-repo process lives in the plugins repo: `docs/RELEASE_PROCESS.md`.

## Development

```bash
# Backend against a local data dir (dev default port differs from device)
python server.py --data-dir ~/driving_data/data --port 8082

# Frontend with hot reload (proxies API to :8082)
cd frontend && npm install && npm run dev

# Tests — conftest isolates device params so nothing touches real hardware
python -m pytest tests/

# Deploy working tree to a device (builds frontend, rsyncs, restarts)
./deploy_dev.sh [host]
```

A pre-push hook runs the full test suite. `deploy_dev.sh` syncs the working
tree (dev iteration); releases are always built from committed sources via
`make_release.sh`.
