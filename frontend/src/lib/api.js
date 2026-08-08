// API layer for connect-on-device
// All fetch functions target the comma-compatible /v1 endpoints

/** Route identifier for API URLs — local_id is the canonical key (e.g. "00000123--ecd17bc154") */
const routeId = (name) => name

// ── Device & auth ───────────────────────────────────────────

export async function fetchDevices() {
  const res = await fetch('/v1/me/devices/')
  if (!res.ok) throw new Error(`fetchDevices: ${res.status}`)
  return res.json()
}

export async function fetchDeviceStats(dongleId) {
  const res = await fetch(`/v1.1/devices/${dongleId}/stats`)
  if (!res.ok) throw new Error(`fetchDeviceStats: ${res.status}`)
  return res.json()
}

export async function fetchStorage() {
  const res = await fetch('/v1/storage')
  if (!res.ok) throw new Error(`fetchStorage: ${res.status}`)
  return res.json()
}

export async function fetchHealth() {
  const res = await fetch('/health')
  if (!res.ok) return null
  return res.json()
}

// ── Routes ──────────────────────────────────────────────────

export async function fetchRoutes(dongleId, { limit = 5, offset, beforeCounter, filter, afterGps, beforeGps } = {}) {
  let url = `/v1/devices/${dongleId}/routes?limit=${limit}`
  if (offset != null) url += `&offset=${offset}`
  if (beforeCounter != null) url += `&before_counter=${beforeCounter}`
  if (filter) url += `&filter=${filter}`
  if (afterGps != null) url += `&after_gps=${afterGps}`
  if (beforeGps != null) url += `&before_gps=${beforeGps}`
  const res = await fetch(url)
  if (!res.ok) throw new Error(`fetchRoutes: ${res.status}`)
  return res.json()
}

export async function fetchPreservedRoutes(dongleId) {
  const res = await fetch(`/v1/devices/${dongleId}/routes/preserved`)
  if (!res.ok) throw new Error(`fetchPreservedRoutes: ${res.status}`)
  return res.json()
}

export async function fetchRoute(routeName) {
  const res = await fetch(`/v1/route/${routeId(routeName)}/`)
  if (!res.ok) throw new Error(`fetchRoute: ${res.status}`)
  return res.json()
}

export async function fetchRouteFiles(routeName) {
  const res = await fetch(`/v1/route/${routeId(routeName)}/files`)
  if (!res.ok) throw new Error(`fetchRouteFiles: ${res.status}`)
  return res.json()
}

export async function enrichRoute(routeName) {
  const res = await fetch(`/v1/route/${routeId(routeName)}/enrich`, { method: 'POST' })
  if (!res.ok) throw new Error(`enrichRoute: ${res.status}`)
  return res.json()
}

export async function scanRoute(localId) {
  const res = await fetch(`/v1/route/${localId}/scan`, { method: 'POST' })
  if (!res.ok) throw new Error(`scanRoute: ${res.status}`)
  return res.json()
}

// ── Route actions ───────────────────────────────────────────

export async function preserveRoute(routeName) {
  const res = await fetch(`/v1/route/${routeId(routeName)}/preserve`, { method: 'POST' })
  if (!res.ok) throw new Error(`preserveRoute: ${res.status}`)
  return res.json()
}

export async function unpreserveRoute(routeName) {
  const res = await fetch(`/v1/route/${routeId(routeName)}/preserve`, { method: 'DELETE' })
  if (!res.ok) throw new Error(`unpreserveRoute: ${res.status}`)
  return res.json()
}

export async function deleteRoute(routeName) {
  const res = await fetch(`/v1/route/${routeId(routeName)}/`, { method: 'DELETE' })
  if (!res.ok) throw new Error(`deleteRoute: ${res.status}`)
  return res.json()
}

export async function saveNote(routeName, note) {
  const res = await fetch(`/v1/route/${routeId(routeName)}/note`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ note }),
  })
  if (!res.ok) throw new Error(`saveNote: ${res.status}`)
  return res.json()
}

export async function addBookmark(routeName, timeSec, label) {
  const res = await fetch(`/v1/route/${routeId(routeName)}/bookmark`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ time_sec: timeSec, label }),
  })
  if (!res.ok) throw new Error(`addBookmark: ${res.status}`)
  return res.json()
}

export async function updateBookmark(routeName, index, label) {
  const res = await fetch(`/v1/route/${routeId(routeName)}/bookmark/${index}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ label }),
  })
  if (!res.ok) throw new Error(`updateBookmark: ${res.status}`)
  return res.json()
}

export async function deleteBookmark(routeName, index) {
  const res = await fetch(`/v1/route/${routeId(routeName)}/bookmark/${index}`, { method: 'DELETE' })
  if (!res.ok) throw new Error(`deleteBookmark: ${res.status}`)
  return res.json()
}

export async function takeScreenshot(routeName, timeSec, camera = 'fcamera') {
  const res = await fetch(`/v1/route/${routeId(routeName)}/screenshot`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ time: timeSec, camera }),
  })
  if (!res.ok) throw new Error(`screenshot: ${res.status}`)
  return res  // Raw response for blob download
}

/** Build GET URL for a fcamera frame at the given time (opens as JPEG in browser) */
export function frameUrl(routeName, timeSec) {
  return `/v1/route/${routeId(routeName)}/frame?t=${timeSec.toFixed(2)}`
}

/** Build URL for HD camera MP4 (HEVC muxed to container) for a specific segment */
export function cameraUrl(routeName, cameraType, segment) {
  return `/v1/route/${routeId(routeName)}/camera/${cameraType}/${segment}`
}

/** Build MJPEG stream URL for qcamera (universal browser support) */
export function mjpegUrl(routeName, t = 0, fps = 20, q = 5) {
  return `/v1/route/${routeId(routeName)}/mjpeg?t=${t}&fps=${fps}&q=${q}`
}

/** Fetch pre-projected HUD overlay data (model + telemetry) for a time range */
export async function fetchHudData(routeName, start = 0, end = 60) {
  const res = await fetch(`/v1/route/${routeId(routeName)}/hud_data?start=${start}&end=${end}`)
  if (!res.ok) return []
  return res.json()
}

/** Build download URL with file type and segment selection */
export function downloadUrl(routeName, fileTypes = ['rlog'], segments = null) {
  let url = `/v1/route/${routeId(routeName)}/download?files=${fileTypes.join(',')}`
  if (segments && segments.length > 0) url += `&segments=${segments.join(',')}`
  return url
}

// ── HUD pre-render ─────────────────────────────────────────

/**
 * Start HUD video prerender.
 * @param {string} routeName
 * @param {number} start - start time in seconds
 * @param {number} end - end time in seconds
 * @param {object} params - render params {speed, scale, fps}
 */
export async function prerenderHud(routeName, start, end, params = {}) {
  const res = await fetch(`/v1/route/${routeId(routeName)}/hud/prerender`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ start, end, ...params }),
  })
  if (!res.ok) throw new Error(`prerenderHud: ${res.status}`)
  return res.json()
}

export async function cancelHudRender(routeName) {
  const res = await fetch(`/v1/route/${routeId(routeName)}/hud/cancel`, { method: 'POST' })
  if (!res.ok) throw new Error(`cancelHudRender: ${res.status}`)
  return res.json()
}

export async function hudProgress(routeName) {
  const res = await fetch(`/v1/route/${routeId(routeName)}/hud/progress`)
  if (!res.ok) throw new Error(`hudProgress: ${res.status}`)
  return res.json()
}

export function hudVideoUrl(routeName) {
  return `/v1/route/${routeId(routeName)}/hud/video`
}

/** Per-frame wall-clock capture times (epoch seconds) for a segment's fcamera */
export async function fetchFrameTimes(routeName, segment) {
  const res = await fetch(`/v1/route/${routeId(routeName)}/frame_times/${segment}`)
  if (!res.ok) throw new Error(`fetchFrameTimes: ${res.status}`)
  return res.json()
}

// ── Connectdata URL builders ────────────────────────────────

/** Build /connectdata/... URL for media files */
export function connectdataUrl(route, segment, filename) {
  if (route.url) {
    try {
      const u = new URL(route.url)
      return `${u.pathname}/${segment}/${filename}`
    } catch {
      return `${route.url}/${segment}/${filename}`
    }
  }
  const [dongle, date] = route.fullname.split('/')
  return `/connectdata/${dongle}/${date}/${segment}/${filename}`
}

/** Build HUD frame URL */
export function hudUrl(route, segment, offsetMs) {
  return connectdataUrl(route, segment, `hud?t=${Math.round(offsetMs)}`)
}

/** Sprite thumbnail URL for a segment, optionally at a specific second */
export function spriteUrl(route, segment, timeSec) {
  const url = connectdataUrl(route, segment, 'sprite.jpg')
  return timeSec != null ? `${url}?t=${timeSec}` : url
}

// ── Derived data (coords, events) ───────────────────────────

export async function fetchCoords(route, segment) {
  const url = connectdataUrl(route, segment, 'coords.json')
  const res = await fetch(url)
  if (!res.ok) return []
  return res.json()
}

export async function fetchEvents(route, segment) {
  const url = connectdataUrl(route, segment, 'events.json')
  const res = await fetch(url)
  if (!res.ok) return []
  return res.json()
}

/** Fetch coords for all segments in parallel */
export async function fetchAllCoords(route) {
  const count = (route.maxqlog ?? 0) + 1
  const results = await Promise.all(
    Array.from({ length: count }, (_, i) =>
      fetchCoords(route, i).catch(() => [])
    )
  )
  return results.flat()
}

/** Fetch events for all segments in parallel */
export async function fetchAllEvents(route) {
  const count = (route.maxqlog ?? 0) + 1
  const results = await Promise.all(
    Array.from({ length: count }, (_, i) =>
      fetchEvents(route, i).catch(() => [])
    )
  )
  return results.flat()
}

/** Fetch events for all segments with progress callback */
export async function fetchAllEventsWithProgress(route, onProgress) {
  const count = (route.maxqlog ?? 0) + 1
  let done = 0
  const results = new Array(count)
  await Promise.all(
    Array.from({ length: count }, (_, i) =>
      fetchEvents(route, i).catch(() => []).then(data => {
        results[i] = data
        done++
        onProgress?.(done, count)
      })
    )
  )
  return results.flat()
}

// ── Signal browser ───────────────────────────────────────────

export async function fetchSignalCatalog(routeName, segments = null) {
  let url = `/v1/route/${routeId(routeName)}/signals/catalog`
  if (segments) url += `?segments=${segments}`
  const res = await fetch(url)
  if (!res.ok) throw new Error(`fetchSignalCatalog: ${res.status}`)
  return res.json()
}

export async function fetchSignalData(routeName, msgType, segments) {
  const url = `/v1/route/${routeId(routeName)}/signals/data/${msgType}/${segments}`
  const res = await fetch(url)
  if (!res.ok) throw new Error(`fetchSignalData: ${res.status}`)
  return res.json()
}

export async function fetchSignalAll(routeName, segments) {
  const url = `/v1/route/${routeId(routeName)}/signals/all/${segments}`
  const res = await fetch(url)
  if (!res.ok) throw new Error(`fetchSignalAll: ${res.status}`)
  return res.json()
}

// ── Dashboard telemetry ──────────────────────────────────────

export async function fetchDashboardTelemetry(routeName, segments) {
  const res = await fetch(`/v1/dashboard/telemetry/${routeId(routeName)}/${segments}`)
  if (!res.ok) throw new Error(`fetchDashboardTelemetry: ${res.status}`)
  return res.json()
}

// ── Software update management ──────────────────────────────

export async function fetchSoftware() {
  const res = await fetch('/v1/software')
  if (!res.ok) throw new Error(`fetchSoftware: ${res.status}`)
  return res.json()
}

export async function softwareCheck() {
  const res = await fetch('/v1/software/check', { method: 'POST' })
  if (!res.ok) throw new Error(`softwareCheck: ${res.status}`)
  return res.json()
}

export async function softwareDownload() {
  const res = await fetch('/v1/software/download', { method: 'POST' })
  if (!res.ok) throw new Error(`softwareDownload: ${res.status}`)
  return res.json()
}

export async function softwareInstall() {
  const res = await fetch('/v1/software/install', { method: 'POST' })
  if (!res.ok) throw new Error(`softwareInstall: ${res.status}`)
  return res.json()
}

export async function softwareBranch(branch) {
  const res = await fetch('/v1/software/branch', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ branch }),
  })
  if (!res.ok) throw new Error(`softwareBranch: ${res.status}`)
  return res.json()
}

export async function softwareUninstall() {
  const res = await fetch('/v1/software/uninstall', { method: 'POST' })
  if (!res.ok) throw new Error(`softwareUninstall: ${res.status}`)
  return res.json()
}

export async function softwarePreparePlugins() {
  const res = await fetch('/v1/software/prepare-plugins', { method: 'POST' })
  if (!res.ok) throw new Error(`softwarePreparePlugins: ${res.status}`)
  return res.json()
}

// ── Lateral delay ───────────────────────────────────────────

export async function fetchLateralDelay() {
  const res = await fetch('/v1/lateral-delay')
  if (!res.ok) throw new Error(`fetchLateralDelay: ${res.status}`)
  return res.json()
}

// ── Device panel ────────────────────────────────────────────

export async function fetchDeviceInfo() {
  const res = await fetch('/v1/device')
  if (!res.ok) throw new Error(`fetchDeviceInfo: ${res.status}`)
  return res.json()
}

export async function deviceReboot() {
  const res = await fetch('/v1/device/reboot', { method: 'POST' })
  if (!res.ok) throw new Error(`deviceReboot: ${res.status}`)
  return res.json()
}

export async function devicePoweroff() {
  const res = await fetch('/v1/device/poweroff', { method: 'POST' })
  if (!res.ok) throw new Error(`devicePoweroff: ${res.status}`)
  return res.json()
}

export async function deviceSetLanguage(language) {
  const res = await fetch('/v1/device/language', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ language }),
  })
  if (!res.ok) throw new Error(`deviceSetLanguage: ${res.status}`)
  return res.json()
}

// ── Toggles panel ───────────────────────────────────────────

export async function fetchToggles() {
  const res = await fetch('/v1/toggles')
  if (!res.ok) throw new Error(`fetchToggles: ${res.status}`)
  return res.json()
}

export async function setToggle(key, value) {
  const res = await fetch('/v1/toggles', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ key, value }),
  })
  if (!res.ok) throw new Error(`setToggle: ${res.status}`)
  return res.json()
}

// ── SSH keys ──────────────────────────────────────────────

export async function fetchSshKeys() {
  const res = await fetch('/v1/ssh-keys')
  if (!res.ok) throw new Error(`fetchSshKeys: ${res.status}`)
  return res.json()
}

export async function setSshKeys(username) {
  const res = await fetch('/v1/ssh-keys', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username }),
  })
  const data = await res.json()
  if (!res.ok) throw new Error(data.error || `setSshKeys: ${res.status}`)
  return data
}

export async function removeSshKeys() {
  const res = await fetch('/v1/ssh-keys', { method: 'DELETE' })
  if (!res.ok) throw new Error(`removeSshKeys: ${res.status}`)
  return res.json()
}

// ── Device state ──────────────────────────────────────────

export async function fetchIsOnroad() {
  const res = await fetch('/v1/device/isOnroad')
  if (!res.ok) return false
  const data = await res.json()
  return data.isOnroad === true
}

// ── BMW params ─────────────────────────────────────────────

export async function fetchParams() {
  const res = await fetch('/v1/params')
  if (!res.ok) throw new Error(`fetchParams: ${res.status}`)
  return res.json()
}

export async function setParam(key, value) {
  const res = await fetch('/v1/params', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ key, value }),
  })
  if (!res.ok) throw new Error(`setParam: ${res.status}`)
  return res.json()
}

// ── Model management ──────────────────────────────────────

export async function fetchModels() {
  const res = await fetch('/v1/models')
  if (!res.ok) throw new Error(`fetchModels: ${res.status}`)
  return res.json()
}

export async function fetchModelsActive() {
  const res = await fetch('/v1/models/active')
  if (!res.ok) throw new Error(`fetchModelsActive: ${res.status}`)
  return res.json()
}

export async function swapModel(type, modelId) {
  const res = await fetch('/v1/models/swap', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ type, model_id: modelId }),
  })
  if (!res.ok) throw new Error(`swapModel: ${res.status}`)
  return res.json()
}

export async function checkModelUpdates() {
  const res = await fetch('/v1/models/check-updates', { method: 'POST' })
  if (!res.ok) throw new Error(`checkModelUpdates: ${res.status}`)
  return res.json()
}

export async function downloadModel(type, modelId) {
  const res = await fetch('/v1/models/download', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ type, model_id: modelId }),
  })
  if (!res.ok) throw new Error(`downloadModel: ${res.status}`)
  return res.json()
}

// ── OSM tile management ────────────────────────────────────

export async function fetchTileList() {
  const res = await fetch('/v1/mapd/tiles')
  if (!res.ok) throw new Error(`fetchTileList: ${res.status}`)
  return res.json()
}

export async function startTileDownload(tiles) {
  const res = await fetch('/v1/mapd/tiles/download', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ tiles }),
  })
  if (!res.ok) throw new Error(`startTileDownload: ${res.status}`)
  return res.json()
}

export async function fetchTileProgress() {
  const res = await fetch('/v1/mapd/tiles/progress')
  if (!res.ok) throw new Error(`fetchTileProgress: ${res.status}`)
  return res.json()
}

export async function cancelTileDownload() {
  const res = await fetch('/v1/mapd/tiles/cancel', { method: 'POST' })
  if (!res.ok) throw new Error(`cancelTileDownload: ${res.status}`)
  return res.json()
}

export async function deleteTile(lat, lon) {
  const res = await fetch(`/v1/mapd/tiles/${lat}/${lon}`, { method: 'DELETE' })
  if (!res.ok) throw new Error(`deleteTile: ${res.status}`)
  return res.json()
}

// ── Screenshots ──────────────────────────────────────────

export async function fetchScreenshots() {
  const res = await fetch('/v1/screenshots')
  if (!res.ok) throw new Error(`fetchScreenshots: ${res.status}`)
  return res.json()
}

export function screenshotUrl(filename) {
  return `/v1/screenshots/${encodeURIComponent(filename)}`
}

export async function deleteScreenshot(filename) {
  const res = await fetch(`/v1/screenshots/${encodeURIComponent(filename)}`, { method: 'DELETE' })
  if (!res.ok) throw new Error(`deleteScreenshot: ${res.status}`)
  return res.json()
}

export async function fetchScreenshotByTime(epoch) {
  const res = await fetch(`/v1/screenshots/at/${epoch.toFixed(3)}`)
  if (!res.ok) throw new Error(`fetchScreenshotByTime: ${res.status}`)
  return res  // raw Response for blob download
}

// ── Plugins ──────────────────────────────────────────────

export async function fetchPlugins() {
  const res = await fetch('/v1/plugins')
  if (!res.ok) throw new Error(`fetchPlugins: ${res.status}`)
  return res.json()
}

export async function togglePlugin(pluginId) {
  const res = await fetch(`/v1/plugins/${pluginId}/toggle`, { method: 'POST' })
  if (!res.ok) throw new Error(`togglePlugin: ${res.status}`)
  return res.json()
}

export async function setPluginParam(pluginId, key, value) {
  const res = await fetch(`/v1/plugins/${pluginId}/param`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ key, value }),
  })
  if (!res.ok) throw new Error(`setPluginParam: ${res.status}`)
  return res.json()
}

export async function fetchPluginRepo() {
  const res = await fetch('/v1/plugins/repo')
  if (!res.ok) throw new Error(`fetchPluginRepo: ${res.status}`)
  return res.json()
}

export async function setPluginRepo(url) {
  const res = await fetch('/v1/plugins/repo', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url }),
  })
  if (!res.ok) throw new Error(`setPluginRepo: ${res.status}`)
  return res.json()
}

export async function installPluginRepo() {
  const res = await fetch('/v1/plugins/repo/install', { method: 'POST' })
  if (!res.ok) {
    const data = await res.json().catch(() => ({}))
    throw new Error(data.output || `installPluginRepo: ${res.status}`)
  }
  return res.json()
}

// ── COD + plugin updates ─────────────────────────────────

export async function fetchUpdates() {
  const res = await fetch('/v1/updates/check')
  if (!res.ok) throw new Error(`fetchUpdates: ${res.status}`)
  return res.json()
}

export async function applyUpdates() {
  const res = await fetch('/v1/updates/apply', { method: 'POST' })
  if (!res.ok) throw new Error(`applyUpdates: ${res.status}`)
  return res.json()
}

// ── Mapd binary update ──────────────────────────────────

export async function mapdCheckUpdate() {
  const res = await fetch('/v1/mapd/check-update', { method: 'POST' })
  if (!res.ok) throw new Error(`mapdCheckUpdate: ${res.status}`)
  return res.json()
}

export async function mapdUpdate() {
  const res = await fetch('/v1/mapd/update', { method: 'POST' })
  if (!res.ok) throw new Error(`mapdUpdate: ${res.status}`)
  return res.json()
}
