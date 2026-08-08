<script>
  import { onMount, onDestroy } from 'svelte'
  import { selectedRoute, dongleId, isMetric } from '../stores.js'
  import { fetchRoute, fetchRouteFiles, fetchAllCoords, fetchAllEventsWithProgress, enrichRoute, prerenderHud, hudProgress, hudVideoUrl, cancelHudRender, saveNote, addBookmark, updateBookmark, deleteBookmark, takeScreenshot, fetchScreenshotByTime, fetchDashboardTelemetry, fetchHudData } from '../api.js'
  import { formatDate, formatDistance, formatDuration, getRouteDurationMs, formatAbsoluteTimeHM } from '../format.js'
  import { buildTimelineEvents } from '../derived.js'
  import snarkdown from 'snarkdown'
  import VideoPlayer from '../components/VideoPlayer.svelte'
  import VideoTimeline from '../components/VideoTimeline.svelte'
  import VideoControls from '../components/VideoControls.svelte'
  import RouteMap from '../components/RouteMap.svelte'
  import RouteActions from '../components/RouteActions.svelte'
  import { Tabs } from 'bits-ui'
  import WidgetCard from '../components/dashboard/WidgetCard.svelte'
  import DigitsWidget from '../components/dashboard/DigitsWidget.svelte'
  import SortableGrid from '../components/dashboard/SortableGrid.svelte'
  import GaugeWidget from '../components/dashboard/GaugeWidget.svelte'
  import SteeringWidget from '../components/dashboard/SteeringWidget.svelte'
  import GasBrakeWidget from '../components/dashboard/GasBrakeWidget.svelte'
  import EngagementWidget from '../components/dashboard/EngagementWidget.svelte'
  import SparklineWidget from '../components/dashboard/SparklineWidget.svelte'
  import SparklineMultiWidget from '../components/dashboard/SparklineMultiWidget.svelte'
  import { WIDGET_REGISTRY, loadLayout, saveLayout, resolveWidgetDef } from '../components/dashboard/registry.js'
  import WidgetPicker from '../components/dashboard/WidgetPicker.svelte'

  let route = $state(null)
  let files = $state(null)
  let coords = $state([])
  let timelineEvents = $state([])
  let loading = $state(true)
  let error = $state(null)

  let currentTime = $state(0)
  let duration = $state(0)
  let isPlaying = $state(false)
  // HUD download (pre-render to MP4)
  let dlRendering = $state(false)
  let dlReady = $state(false)
  let dlError = $state(null)
  let dlElapsed = $state(0)
  let dlTotal = $state(0)
  let dlPhase = $state('')
  let dlFrame = $state(0)
  let dlTotalFrames = $state(0)
  let dlRemainingSec = $state(0)
  let dlPostElapsed = $state(0)
  let dlPostTotal = $state(0)
  let dlRenderStart = $state(0)
  let dlPollTimer = null

  // Quality slider: controls resolution continuously (0-100)
  // Single-pass render at 0.2x replay speed → 25fps unique route content from 5fps capture
  let dlQualityPct = $state(100)
  const DL_RECORD_SPEED = 0.2
  const DL_FPS = 20
  // Resolution: slider interpolates width from 540 to 2160 (height is half)
  // Round to even — libx264 requires dimensions divisible by 2
  const dlWidth = $derived(Math.round((540 + (dlQualityPct / 100) * 1620) / 2) * 2)   // 540 → 2160
  const dlHeight = $derived(Math.round((dlWidth / 2) / 2) * 2)                         // 270 → 1080
  // Use scale filter when below native (2160x1080)
  const dlScale = $derived(dlQualityPct < 100 ? `${dlWidth}:${dlHeight}` : null)
  // Bitrate scales roughly with pixel count (quadratic with width)
  const dlBitrate = $derived(0.4 + 2.6 * Math.pow(dlQualityPct / 100, 2))   // ~0.4 → 3.0 Mbps
  const dlDurationSec = $derived(
    (selectionEnd > 0 ? selectionEnd : duration || ((route?.maxqlog ?? 0) + 1) * 60) - (selectionStart || 0)
  )
  const dlEstimatedMB = $derived(Math.round(dlDurationSec * dlBitrate / 8))
  // Single-pass render at 0.2x speed + setup overhead
  const dlEstimatedRenderSec = $derived(Math.round(dlDurationSec / DL_RECORD_SPEED + 30))

  // HUD mode: null (normal), 'stream', 'download'
  let hudMode = $state(null)

  let screenshotBusy = $state(false)
  let isMuted = $state(true)
  let hevcSupported = $state(null)  // null=checking, true/false
  let hdSource = $state(null)
  let isFullscreen = $state(false)

  // HUD overlay state
  let showHud = $state(false)
  let hudFrames = $state([])
  let hudLoading = $state(false)
  let hudLoadedRange = $state(null)  // {start, end} of loaded segment range

  let noteText = $state('')
  let editingNote = $state(false)
  let metaBookmarks = $state([])  // [{time_sec, label}] from metadata
  let addingBookmark = $state(false)
  let newBookmarkLabel = $state('')
  let editingBookmarkIdx = $state(-1)
  let editingBookmarkLabel = $state('')

  // Restore selection from URL path: /{dongleId}/{localId}/{start}/{end}
  const _urlParts = location.pathname.split('/').filter(Boolean)
  let selectionStart = $state(parseInt(_urlParts[2]) || 0)
  let selectionEnd = $state(parseInt(_urlParts[3]) || 0)
  const selectionTimeRange = $derived.by(() => {
    const st = route?.start_time
    if (!st) return ''
    const effEnd = selectionEnd > 0 ? selectionEnd : duration || ((route?.maxqlog ?? 0) + 1) * 60
    const s = formatAbsoluteTimeHM(st, selectionStart || 0)
    const e = formatAbsoluteTimeHM(st, effEnd)
    if (!s) return ''
    return e ? `@ ${s} - ${e}` : `@ ${s}`
  })
  // Sync selection to URL path: /{dongleId}/{localId}/{start}/{end}
  $effect(() => {
    const d = route?.dongle_id
    const lid = route?.local_id || $selectedRoute
    if (!d || !lid) return
    let path = `/${d}/${lid}`
    if (selectionStart > 0 || selectionEnd > 0) {
      path += `/${Math.round(selectionStart)}/${Math.round(selectionEnd)}`
    }
    if (location.pathname !== path) {
      history.replaceState(null, '', path)
    }
  })

  let enrichDone = $state(0)
  let enrichTotal = $state(0)
  let enrichNeeded = $state(false)  // true only when events.json not yet cached on server
  const enriching = $derived(enrichNeeded && enrichTotal > 0 && enrichDone < enrichTotal)

  let videoPlayer = $state(null)

  const durationMs = $derived(route ? getRouteDurationMs(route) : 0)
  const durationMin = $derived(route?.maxqlog != null ? route.maxqlog + 1 : null)
  // HD sources only available when HEVC is supported
  const sources = $derived.by(() => {
    if (!files || !hevcSupported) return []
    const s = []
    if (files.cameras?.some(u => u)) s.push({ id: 'fcamera', label: 'Road' })
    if (files.ecameras?.some(u => u)) s.push({ id: 'ecamera', label: 'Wide' })
    if (files.dcameras?.some(u => u)) s.push({ id: 'dcamera', label: 'Driver' })
    return s
  })

  // Auto-select fcamera when HEVC supported and sources become available
  let autoSourceDone = false
  $effect(() => {
    if (!autoSourceDone && hevcSupported && sources.length > 0 && hdSource === null) {
      autoSourceDone = true
      if (sources.some(s => s.id === 'fcamera')) hdSource = 'fcamera'
    }
  })

  function onFullscreenChange() {
    const container = document.querySelector('[data-video-container]')
    isFullscreen = !!document.fullscreenElement || !!container?.classList.contains('ios-fullscreen')
    // Auto-rotate to landscape on mobile when entering fullscreen
    try {
      if (isFullscreen) {
        screen.orientation?.lock('landscape').catch(() => {})
      } else {
        screen.orientation?.unlock()
      }
    } catch {}
  }

  onMount(async () => {
    document.addEventListener('fullscreenchange', onFullscreenChange)
    document.addEventListener('webkitfullscreenchange', onFullscreenChange)

    // Check HEVC support once at page level
    const v = document.createElement('video')
    hevcSupported = !!(v.canPlayType('video/mp4; codecs="hev1.1.6.L93.B0"') ||
                       v.canPlayType('video/mp4; codecs="hvc1.1.6.L93.B0"'))

    const localId = $selectedRoute
    if (!localId) return

    // Pre-warm HLS cache: fire-and-forget fetch triggers server-side ffmpeg
    // segmentation (~1-2s). By the time VideoPlayer initializes, it's cached.
    fetch(`/v1/route/${localId}/qcamera.m3u8`).catch(() => {})

    try {
      const [r, f] = await Promise.all([
        fetchRoute(localId),
        fetchRouteFiles(localId),
      ])
      route = r
      files = f
      noteText = r.notes || ''
      metaBookmarks = r.bookmarks || []

      // Seek to selection start from URL (after video player mounts)
      if (selectionStart > 0) {
        currentTime = selectionStart
        await new Promise(r => setTimeout(r, 100))
        videoPlayer?.seek(selectionStart)
      }

      // Fetch coords in background (non-blocking)
      fetchAllCoords(r).then(c => coords = c).catch(() => {})

      // Only auto-load events if already cached on server (no progress bar needed)
      if (r.events_cached) {
        enrichNeeded = false
        enrichTotal = (r.maxqlog ?? 0) + 1
        fetchAllEventsWithProgress(r, () => {}).then(raw => {
          timelineEvents = buildTimelineEvents(raw, getRouteDurationMs(r))
        }).catch(() => {})
      } else {
        enrichNeeded = true
        // enrichTotal stays 0 so enriching derived stays false → shows Enrich button
      }
    } catch (e) {
      error = e.message
    } finally {
      loading = false
    }
  })

  onDestroy(() => {
    document.removeEventListener('fullscreenchange', onFullscreenChange)
    document.removeEventListener('webkitfullscreenchange', onFullscreenChange)
    if (dlPollTimer) { clearInterval(dlPollTimer); dlPollTimer = null }
  })

  function goBack() {
    selectedRoute.set(null)
  }

  function handleSeek(t) {
    currentTime = t
    videoPlayer?.seek(t)
  }

  function handleToggle() {
    videoPlayer?.toggle()
  }

  function handleRate(r) {
    videoPlayer?.setPlaybackRate(r)
  }

  function handleStepFrame(delta) {
    // 20fps = 0.05s per frame
    const step = delta * (1 / 20)
    const t = Math.max(0, Math.min(duration, currentTime + step))
    currentTime = t
    videoPlayer?.seek(t)
  }

  function handleTimeUpdate(t) {
    // Loop within selection range
    if (selectionEnd > 0 && t >= selectionEnd) {
      videoPlayer?.seek(selectionStart)
      currentTime = selectionStart
      return
    }
    currentTime = t
    // Ensure HUD data is loaded for current position
    if (showHud) ensureHudData(t)
  }

  function handlePlay() {
    isPlaying = true
  }

  function handlePause() {
    isPlaying = false
  }

  function handleMuteToggle() {
    isMuted = videoPlayer?.toggleMute() ?? !isMuted
  }

  function handleSourceChange(sourceId) {
    hdSource = sourceId
  }

  async function toggleHudOverlay() {
    showHud = !showHud
    if (showHud && !hudFrames.length && route) {
      // Load HUD data for the visible range (or full route)
      hudLoading = true
      const start = selectionStart || 0
      const end = selectionEnd > 0 ? selectionEnd : duration || ((route.maxqlog + 1) * 60)
      try {
        hudFrames = await fetchHudData(route.local_id, start, end)
        hudLoadedRange = { start, end }
      } catch (e) {
        console.warn('HUD data load failed:', e)
      }
      hudLoading = false
    }
  }

  // Load more HUD data when playback moves outside loaded range
  async function ensureHudData(t) {
    if (!showHud || !route || hudLoading) return
    if (hudLoadedRange && t >= hudLoadedRange.start && t <= hudLoadedRange.end) return

    // Load the segment containing t (± 1 segment buffer)
    const seg = Math.floor(t / 60)
    const start = Math.max(0, (seg - 1)) * 60
    const end = Math.min((seg + 2) * 60, (route.maxqlog + 1) * 60)
    hudLoading = true
    try {
      const newFrames = await fetchHudData(route.local_id, start, end)
      // Merge with existing frames, dedup by time
      const existing = new Set(hudFrames.map(f => f.t))
      hudFrames = [...hudFrames, ...newFrames.filter(f => !existing.has(f.t))].sort((a, b) => a.t - b.t)
      if (hudLoadedRange) {
        hudLoadedRange = {
          start: Math.min(hudLoadedRange.start, start),
          end: Math.max(hudLoadedRange.end, end),
        }
      } else {
        hudLoadedRange = { start, end }
      }
    } catch {}
    hudLoading = false
  }

  async function handleScreenshot() {
    if (!route || screenshotBusy) return
    screenshotBusy = true
    try {
      const res = await takeScreenshot(route.local_id, currentTime, hdSource || 'qcamera')
      const blob = await res.blob()
      // Extract filename from Content-Disposition header, or build fallback
      const cd = res.headers.get('Content-Disposition') || ''
      const match = cd.match(/filename="?([^"]+)"?/)
      const filename = match ? match[1] : 'screenshot.jpg'
      const a = document.createElement('a')
      a.href = URL.createObjectURL(blob)
      a.download = filename
      a.click()
      URL.revokeObjectURL(a.href)
    } catch (e) {
      console.error('Screenshot failed:', e)
    } finally {
      screenshotBusy = false
    }
  }

  function restoreVideoDefaults() {
    videoPlayer?.setPlaybackRate(1.0)
    videoPlayer?.pause()
    isPlaying = false
  }

  function formatRemaining(sec) {
    if (!sec || sec <= 0) return ''
    const m = Math.floor(sec / 60)
    const s = Math.round(sec % 60)
    return m > 0 ? `${m}m ${s}s` : `${s}s`
  }

  async function startDownload() {
    if (dlRendering || dlReady) return
    dlRendering = true
    dlReady = false
    dlError = null
    dlElapsed = 0
    dlTotal = 0
    dlPhase = ''
    dlFrame = 0
    dlTotalFrames = 0
    dlRemainingSec = 0
    dlPostElapsed = 0
    dlPostTotal = 0
    dlRenderStart = selectionStart || 0

    try {
      const start = selectionStart || 0
      const end = selectionEnd > 0 ? selectionEnd : duration || ((route.maxqlog + 1) * 60)
      const res = await prerenderHud(route.local_id, start, end, {
        scale: dlScale,
        fps: DL_FPS,
      })

      if (res.status === 'complete') {
        dlRendering = false
        dlReady = true
        return
      }

      dlTotal = res.total_sec || (end - start)

      // Seek video to render start and freeze (static red border during preparation)
      currentTime = start
      videoPlayer?.seek(start)

      // Poll for progress — seek video preview to follow render position
      dlPollTimer = setInterval(async () => {
        try {
          const p = await hudProgress(route.local_id)
          dlElapsed = p.elapsed_sec || 0
          dlTotal = p.total_sec || dlTotal
          dlPhase = p.phase || ''
          dlFrame = p.frame || 0
          dlTotalFrames = p.total_frames || dlTotalFrames
          dlRemainingSec = p.remaining_sec ?? dlRemainingSec
          dlPostElapsed = p.post_elapsed_sec ?? dlPostElapsed
          dlPostTotal = p.post_total_sec ?? dlPostTotal

          // Update playhead to follow render position
          if (p.elapsed_sec != null && p.status === 'rendering') {
            currentTime = dlRenderStart + (p.elapsed_sec || 0)
          }

          if (p.status === 'complete') {
            clearInterval(dlPollTimer)
            dlPollTimer = null
            dlRendering = false
            dlReady = true
            restoreVideoDefaults()
          } else if (p.status === 'error') {
            clearInterval(dlPollTimer)
            dlPollTimer = null
            dlRendering = false
            dlError = p.error || 'Render failed'
            restoreVideoDefaults()
          }
        } catch { /* ignore poll errors */ }
      }, 2000)
    } catch (e) {
      dlRendering = false
      dlError = e.message
      restoreVideoDefaults()
    }
  }

  async function cancelDownload() {
    if (dlPollTimer) { clearInterval(dlPollTimer); dlPollTimer = null }
    dlRendering = false
    dlReady = false
    dlError = null
    hudMode = null
    restoreVideoDefaults()
    try {
      if (route) await cancelHudRender(route.local_id)
    } catch { /* ignore */ }
  }

  async function saveNoteHandler() {
    editingNote = false
    if (route) await saveNote(route.local_id, noteText)
  }

  function addBookmarkHandler() {
    if (!route || !newBookmarkLabel.trim()) return
    const bm = { time_sec: Math.round(currentTime * 10) / 10, label: newBookmarkLabel.trim() }
    metaBookmarks = [...metaBookmarks, bm].sort((a, b) => a.time_sec - b.time_sec)
    addBookmark(route.local_id, bm.time_sec, bm.label).catch(() => {})
    newBookmarkLabel = ''
    addingBookmark = false
  }

  function updateBookmarkHandler(index) {
    if (!route || !editingBookmarkLabel.trim()) return
    metaBookmarks[index] = { ...metaBookmarks[index], label: editingBookmarkLabel.trim() }
    metaBookmarks = metaBookmarks
    updateBookmark(route.local_id, index, editingBookmarkLabel.trim()).catch(() => {})
    editingBookmarkIdx = -1
  }

  function deleteBookmarkHandler(index) {
    if (!route) return
    metaBookmarks = metaBookmarks.filter((_, i) => i !== index)
    deleteBookmark(route.local_id, index).catch(() => {})
  }

  let bookmarkExportBusy = $state(-1)  // index of bookmark being exported, -1 = none

  async function exportBookmarkFrame(index) {
    if (!route || bookmarkExportBusy >= 0) return
    bookmarkExportBusy = index
    try {
      const bm = metaBookmarks[index]
      const epoch = (route.create_time || 0) + bm.time_sec

      // Try HUD screenshot first (captured live with overlay)
      let res, filename
      try {
        res = await fetchScreenshotByTime(epoch)
        const cd = res.headers.get('Content-Disposition') || ''
        const match = cd.match(/filename="?([^"]+)"?/)
        filename = match ? match[1] : `hud_${Math.round(bm.time_sec)}s.png`
      } catch {
        // No HUD screenshot — fall back to plain fcamera JPEG with EXIF
        res = await takeScreenshot(route.local_id, bm.time_sec, 'fcamera')
        const cd = res.headers.get('Content-Disposition') || ''
        const match = cd.match(/filename="?([^"]+)"?/)
        filename = match ? match[1] : 'screenshot.jpg'
      }

      const blob = await res.blob()
      const a = document.createElement('a')
      a.href = URL.createObjectURL(blob)
      a.download = filename
      a.click()
      URL.revokeObjectURL(a.href)
    } catch (e) {
      console.error('Bookmark export failed:', e)
    } finally {
      bookmarkExportBusy = -1
    }
  }

  let activeTab = $state('map')

  // ── Dashboard replay state (progressive per-segment loading) ──
  let dashSegData = $state({})       // { segNum: [...samples] }
  let dashLoadedSegs = $state(new Set())
  let dashLoadingCount = $state(0)   // number of in-flight requests
  let dashTotalSegs = $state(0)
  let dashStarted = $state(false)    // has progressive loading been kicked off?
  let dashError = $state(null)
  let dashLayout = $state(loadLayout())
  let showWidgetPicker = $state(false)

  function dashRemoveWidget(id) {
    dashLayout = dashLayout.filter(w => w !== id)
    saveLayout(dashLayout)
  }

  function dashAddWidget(id) {
    if (!dashLayout.includes(id)) {
      dashLayout = [...dashLayout, id]
      saveLayout(dashLayout)
    }
  }

  // Flattened sorted samples from all loaded segments
  const dashSamples = $derived.by(() => {
    const segs = Object.keys(dashSegData).map(Number).sort((a, b) => a - b)
    const flat = segs.flatMap(s => dashSegData[s])
    return flat.length > 0 ? flat : (dashStarted ? [] : null)
  })

  // Auto-enter note edit mode when Notes tab selected with empty note
  let noteTextarea = $state(null)
  $effect(() => {
    if (activeTab === 'note' && !noteText && !editingNote) {
      editingNote = true
    }
  })
  $effect(() => {
    if (editingNote && noteTextarea) {
      noteTextarea.focus()
    }
  })

  // Load telemetry when dashboard tab first selected
  $effect(() => {
    if (activeTab === 'dashboard' && !dashStarted && route) {
      loadDashProgressive()
    }
  })

  // Memoize current segment so seek-ahead only fires on segment change, not every frame
  const dashCurrentSeg = $derived(
    dashTotalSegs > 0 ? Math.min(Math.floor(currentTime / 60), dashTotalSegs - 1) : -1
  )

  // Seek-ahead: when user seeks to an unloaded segment, prioritize it
  $effect(() => {
    if (activeTab !== 'dashboard' || dashCurrentSeg < 0) return
    if (!dashLoadedSegs.has(dashCurrentSeg)) {
      loadDashSegment(dashCurrentSeg)
    }
  })

  async function loadDashSegment(seg) {
    if (dashLoadedSegs.has(seg)) return
    dashLoadedSegs = new Set([...dashLoadedSegs, seg])
    dashLoadingCount++
    try {
      const samples = await fetchDashboardTelemetry(route.local_id, String(seg))
      dashSegData = { ...dashSegData, [seg]: samples }
    } catch (e) {
      // Remove from loaded set so it can retry
      const s = new Set(dashLoadedSegs); s.delete(seg)
      dashLoadedSegs = s
    } finally {
      dashLoadingCount--
    }
  }

  async function loadDashProgressive() {
    if (!route) return
    dashStarted = true
    dashError = null
    const maxSeg = route.maxqlog ?? 0
    dashTotalSegs = maxSeg + 1
    const startSeg = Math.min(Math.floor(currentTime / 60), maxSeg)

    try {
      // Load current segment first (instant widget feedback)
      await loadDashSegment(startSeg)

      // Then expand outward, 2 concurrent requests at a time
      for (let offset = 1; offset <= maxSeg; offset++) {
        const promises = []
        if (startSeg + offset <= maxSeg) promises.push(loadDashSegment(startSeg + offset))
        if (startSeg - offset >= 0) promises.push(loadDashSegment(startSeg - offset))
        if (promises.length) await Promise.all(promises)
      }
    } catch (e) {
      dashError = e.message
    }
  }

  // Binary search: find the sample closest to currentTime
  function dashSampleAt(t) {
    const s = dashSamples
    if (!s || !s.length) return null
    let lo = 0, hi = s.length - 1
    while (lo < hi) {
      const mid = (lo + hi) >> 1
      if (s[mid].t < t) lo = mid + 1
      else hi = mid
    }
    // Pick whichever of lo-1 or lo is closer
    if (lo > 0 && Math.abs(s[lo - 1].t - t) < Math.abs(s[lo].t - t)) lo--
    return s[lo]
  }

  // Current telemetry sample synced to video playhead
  const dashTelemetry = $derived.by(() => {
    if (!dashSamples || !dashSamples.length) return {
      t: 0, coolantTemp: 0, oilTemp: 0, voltage: 0,
      vEgo: 0, steeringAngleDeg: 0,
      gasPressed: false, brakePressed: false,
      steerCmd: 0, accelCmd: 0,
      sdState: '', sdEnabled: false, cruiseSpeed: 0,
    }
    return dashSampleAt(currentTime) ?? dashSamples[0]
  })

  // History window: last 300 samples up to currentTime for sparklines
  const DASH_HISTORY_SIZE = 300
  const dashHistory = $derived.by(() => {
    const s = dashSamples
    if (!s || !s.length) return []
    // Find end index (sample at or just before currentTime)
    let end = s.length - 1
    let lo = 0, hi = s.length - 1
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1
      if (s[mid].t <= currentTime) lo = mid
      else hi = mid - 1
    }
    end = lo
    const start = Math.max(0, end - DASH_HISTORY_SIZE + 1)
    return s.slice(start, end + 1)
  })

  function dashFieldHistory(field) {
    return dashHistory.map(h => ({ t: h.t, v: h[field] ?? 0 }))
  }

  function dashMultiFieldHistories(fields) {
    return fields.map(f => dashFieldHistory(f))
  }

  function getWidgetDef(id) {
    const def = WIDGET_REGISTRY.find(w => w.id === id)
    return resolveWidgetDef(def, $isMetric)
  }

  // Engagement border color derived from timeline events at current playhead
  const engagementColor = $derived.by(() => {
    if (!timelineEvents.length) return null
    const tMs = currentTime * 1000
    let engaged = false, overriding = false, alertStatus = 0
    for (const ev of timelineEvents) {
      if (ev.end_route_offset_millis == null || ev.end_route_offset_millis <= tMs) continue
      if (ev.route_offset_millis > tMs) continue
      if (ev.type === 'engaged') engaged = true
      else if (ev.type === 'overriding') overriding = true
      else if (ev.type === 'alert') alertStatus = Math.max(alertStatus, ev.alertStatus ?? 1)
    }
    if (alertStatus === 2) return '#C92231'
    if (overriding) return '#919B95'
    if (alertStatus >= 1) return '#FE8C34'
    if (engaged) return '#178644'
    return '#173349'
  })

  let reEnriching = $state(false)

  async function reEnrich() {
    if (!route || reEnriching) return
    reEnriching = true
    enrichNeeded = false  // Hide the Enrich button, show progress bar
    try {
      // Clear cached derived files on server and re-enrich metadata
      await enrichRoute(route.local_id)
      // Re-fetch events (regenerates events.json from rlogs)
      enrichDone = 0
      enrichTotal = (route.maxqlog ?? 0) + 1
      const raw = await fetchAllEventsWithProgress(route, (done, total) => {
        enrichDone = done
        enrichTotal = total
      })
      timelineEvents = buildTimelineEvents(raw, getRouteDurationMs(route))
      // Re-fetch route to get updated metadata (engagement %, addresses, enriched flag)
      route = await fetchRoute(route.local_id)
      // Re-fetch coords too
      fetchAllCoords(route).then(c => coords = c).catch(() => {})
    } catch (e) {
      console.error('Re-enrich failed:', e)
    } finally {
      reEnriching = false
    }
  }

  function handleHudDownload() {
    hudMode = 'download'
  }

  let dlDownloading = $state(false)

  async function openDownload() {
    if (!route || dlDownloading) return
    dlDownloading = true
    try {
      const res = await fetch(hudVideoUrl(route.local_id) + `?t=${Date.now()}`)
      const blob = await res.blob()
      const a = document.createElement('a')
      a.href = URL.createObjectURL(blob)
      a.download = `${route.fullname.split('/').pop()}_hud.mp4`
      a.click()
      URL.revokeObjectURL(a.href)
    } catch (e) {
      console.error('Download failed:', e)
    } finally {
      dlDownloading = false
    }
  }
</script>

<div class="mx-auto w-full sm:w-[90%] max-w-screen-2xl px-2 sm:px-4 py-2 sm:py-4">
  <!-- Back button -->
  <div class="flex items-center gap-3 mb-3">
    <button class="btn-ghost -ml-2 text-sm" onclick={goBack}>
      <svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
        <path d="M15 19l-7-7 7-7"/>
      </svg>
      Back
    </button>
    {#if route}
      <span class="text-sm text-surface-100 font-medium">{formatDate(route.create_time)} {selectionTimeRange}</span>
    {/if}
  </div>

  {#if loading}
    <div class="space-y-4">
      <div class="card animate-pulse aspect-video bg-surface-800 rounded-lg"></div>
      <div class="h-10 bg-surface-800 rounded-lg animate-pulse"></div>
    </div>
  {:else if error}
    <div class="flex items-center justify-center h-48">
      <p class="text-surface-400">{error}</p>
    </div>
  {:else if route}
    <div class="grid grid-cols-1 lg:grid-cols-[2fr_1fr] gap-2 sm:gap-4">
      <!-- Video player + controls -->
      <div class:space-y-2={!isFullscreen} class:fullscreen-container={isFullscreen} data-video-container>
        {#if !isFullscreen}
          {#if enrichNeeded && !enriching && !reEnriching}
            <button
              class="w-full py-3 rounded-lg bg-amber-500/20 hover:bg-amber-500/30 text-amber-300 text-sm font-medium transition-colors border border-amber-500/30"
              onclick={reEnrich}
            >
              Enrich route data
            </button>
          {:else if enriching || reEnriching}
            <div class="space-y-1">
              <div class="h-2 bg-surface-700 rounded-full overflow-hidden">
                <div
                  class="h-full bg-engage-green rounded-full transition-all duration-300"
                  style="width: {enrichTotal > 0 ? (enrichDone / enrichTotal) * 100 : 0}%"
                ></div>
              </div>
              <p class="text-xs text-surface-400 text-center">Enriching segments {enrichDone}/{enrichTotal}</p>
            </div>
          {:else}
            <VideoTimeline
              {route}
              {currentTime}
              {duration}
              events={timelineEvents}
              {durationMs}
              onSeek={handleSeek}
              bind:selectionStart
              bind:selectionEnd
            />
          {/if}
        {/if}

        <div class="relative overflow-hidden" class:fullscreen-video={isFullscreen}
          class:rounded-xl={!isFullscreen}
          class:recording-border-static={dlRendering && dlPhase !== 'recording'}
          class:recording-border-blink={dlRendering && dlPhase === 'recording'}
          style={!hudMode && engagementColor ? `border: ${isFullscreen ? '6px' : '8px'} solid ${engagementColor}` : ''}>
          <!-- Live render preview (MJPEG) during HUD Download recording -->
          {#if dlRendering && dlPhase === 'recording' && route}
            <img
              src={`/v1/route/${route.local_id}/hud/preview`}
              class="absolute inset-0 w-full h-full object-contain bg-black z-30"
              alt="Render preview"
            />
          {/if}
          <VideoPlayer
            bind:this={videoPlayer}
            {route}
            {files}
            {hdSource}
            frozen={dlRendering}
            useMjpeg={hevcSupported === false && !hdSource}
            {hudFrames}
            {showHud}
            {selectionStart}
            {selectionEnd}
            bind:currentTime
            bind:duration
            onTimeUpdate={handleTimeUpdate}
            onPlay={handlePlay}
            onPause={handlePause}
            onHudToggle={!enriching ? toggleHudOverlay : undefined}
            onHudDownload={!enriching && !hudMode ? handleHudDownload : undefined}
            onHevcFailed={() => { hevcSupported = false; hdSource = null }}
          />
          {#if isFullscreen && !enriching}
            <!-- Floating controls overlay in fullscreen -->
            <div class="fullscreen-controls">
              {#if !hudMode}
                <VideoControls
                  {route}
                  {currentTime}
                  {duration}
                  startTime={route.start_time}
                  onSeek={handleSeek}
                  onToggle={handleToggle}
                  onRate={handleRate}
                  onScreenshot={handleScreenshot}
                  onStepFrame={handleStepFrame}
                  onMuteToggle={handleMuteToggle}
                  onSourceChange={handleSourceChange}
                  {isPlaying}
                  {isMuted}
                  {screenshotBusy}
                  {hdSource}
                  {sources}
                />
              {/if}
            </div>
          {/if}
        </div>

        {#if !isFullscreen && !enriching}
          {#if hudMode === 'download'}
            <!-- HUD Download panel -->
            {#if !dlRendering && !dlReady}
              <div class="flex items-center justify-between h-8 px-2">
                <span class="text-xs text-surface-400">
                  ~{dlEstimatedMB} MB · ~{Math.ceil(dlEstimatedRenderSec / 60)} min render
                </span>
                <div class="flex items-center gap-2">
                  <button class="btn-sm bg-engage-green text-black font-medium px-3 py-1 rounded text-xs"
                    onclick={startDownload}>Start</button>
                  <button class="btn-sm bg-surface-700 text-surface-300 px-3 py-1 rounded text-xs"
                    onclick={cancelDownload}>Close</button>
                </div>
              </div>
            {:else if dlRendering}
              <div class="flex items-center justify-between h-8 px-2">
                <div class="flex items-center gap-2">
                  {#if dlPhase === 'recording'}
                    <span class="dl-rec-dot"></span>
                    <span class="text-xs text-surface-300">Recording {Math.round(dlElapsed)}s / {Math.round(dlTotal)}s{dlRemainingSec > 0 ? ` · ~${formatRemaining(dlRemainingSec)}` : ''}</span>
                  {:else if dlPhase === 'concatenating' || dlPhase === 'post-processing'}
                    <span class="dl-spinner"></span>
                    <span class="text-xs text-surface-300">Post-processing {Math.round(dlPostElapsed)}s / {Math.round(dlPostTotal || dlTotal)}s</span>
                  {:else}
                    <span class="dl-spinner"></span>
                    <span class="text-xs text-surface-300">Preparing{dlPhase ? ` · ${dlPhase}` : '...'}</span>
                  {/if}
                </div>
                <button class="btn-sm bg-red-600 hover:bg-red-500 text-white px-3 py-1 rounded text-xs"
                  onclick={cancelDownload}>Cancel</button>
              </div>
            {:else if dlReady}
              <div class="flex items-center justify-end gap-2 h-8 px-2">
                <button class="btn-sm bg-engage-green text-black font-medium px-3 py-1 rounded text-xs disabled:opacity-50 disabled:cursor-not-allowed"
                  onclick={openDownload} disabled={dlDownloading}>
                  {#if dlDownloading}Downloading...{:else}Download MP4{/if}
                </button>
                <button class="btn-sm bg-surface-700 text-surface-300 px-3 py-1 rounded text-xs"
                  onclick={cancelDownload}>Close</button>
              </div>
            {/if}
            {#if dlError}
              <p class="text-xs text-red-400 px-2">{dlError}</p>
            {/if}
          {:else}
            <!-- Normal video controls -->
            <VideoControls
              {route}
              {currentTime}
              {duration}
              startTime={route.start_time}
              onSeek={handleSeek}
              onToggle={handleToggle}
              onRate={handleRate}
              onScreenshot={handleScreenshot}
              onStepFrame={handleStepFrame}
              onMuteToggle={handleMuteToggle}
              onSourceChange={handleSourceChange}
              {isPlaying}
              {isMuted}
              {screenshotBusy}
              {hdSource}
              {sources}
            />
          {/if}
        {/if}
      </div>

      <!-- Tabbed info panel -->
      <div class="card lg:h-full">
        <Tabs.Root bind:value={activeTab} class="lg:h-full lg:grid lg:grid-rows-[auto_1fr]">
          <Tabs.List class="flex border-b border-surface-700/50">
            {#each [['map','Map'],['route','Route'],['dashboard','Dashboard'],['note','Notes']] as [id, label]}
              <Tabs.Trigger value={id}
                class="flex-1 px-2 py-3 text-xs font-medium text-center transition-colors
                  {activeTab === id ? 'text-surface-100 border-b-2 border-engage-blue' : 'text-surface-500 hover:text-surface-300'}"
              >{label}</Tabs.Trigger>
            {/each}
          </Tabs.List>

          <!-- Map tab -->
          <Tabs.Content value="map" class="pt-2 lg:h-full lg:overflow-hidden">
            <RouteMap
              {coords}
              events={timelineEvents}
              {currentTime}
              {durationMs}
              {selectionStart}
              {selectionEnd}
              visible={activeTab === 'map'}
              startLat={route.start_lat}
              startLng={route.start_lng}
            />
          </Tabs.Content>

          <!-- Route tab -->
          <Tabs.Content value="route" class="pt-2">
            <div class="flex flex-col lg:h-full pt-1">
              <div class="px-4 grid grid-cols-2 gap-2 text-sm">
                {#if route.platform}
                  <div>
                    <p class="text-xs text-surface-500">Car</p>
                    <p class="text-surface-200">{route.platform}</p>
                  </div>
                {/if}
                {#if route.local_id}
                  <div>
                    <p class="text-xs text-surface-500">Route ID</p>
                    <p class="text-surface-200 font-mono truncate">{route.local_id}</p>
                  </div>
                {/if}
                {#if route.start_address}
                  <div>
                    <p class="text-xs text-surface-500">Start</p>
                    <p class="text-surface-200 truncate">{route.start_address}</p>
                  </div>
                  {#if route.end_address && route.end_address !== route.start_address}
                    <div>
                      <p class="text-xs text-surface-500">End</p>
                      <p class="text-surface-200 truncate">{route.end_address}</p>
                    </div>
                  {/if}
                {/if}
                <div>
                  <p class="text-xs text-surface-500">Duration</p>
                  <p class="text-surface-200">{durationMin != null ? formatDuration(durationMin) : '--'}</p>
                </div>
                <div>
                  <p class="text-xs text-surface-500">Distance</p>
                  <p class="text-surface-200">{formatDistance(route.distance_m)}</p>
                </div>
                <div>
                  <p class="text-xs text-surface-500">Device</p>
                  <p class="text-surface-200">{route.device_type === 'tici' ? 'Comma 3' : route.device_type === 'tizi' ? 'Comma 3X' : route.device_type === 'mici' ? 'Comma 4' : route.device_type ?? '--'}</p>
                </div>
                <div>
                  <p class="text-xs text-surface-500">Dongle ID</p>
                  <p class="text-surface-200 font-mono truncate">{route.dongle_id ?? '--'}</p>
                </div>
                {#if route.agnos_version}
                  <div>
                    <p class="text-xs text-surface-500">AGNOS</p>
                    <p class="text-surface-200">{route.agnos_version}</p>
                  </div>
                {/if}
                {#if route.version}
                  <div>
                    <p class="text-xs text-surface-500">Openpilot</p>
                    <p class="text-surface-200">{route.version}</p>
                  </div>
                {/if}
                {#if route.git_branch}
                  <div>
                    <p class="text-xs text-surface-500">Branch</p>
                    <p class="text-surface-200 truncate">{route.git_branch}</p>
                  </div>
                {/if}
                {#if route.git_commit}
                  <div>
                    <p class="text-xs text-surface-500">Commit</p>
                    {#if route.git_remote}
                      {@const repoUrl = route.git_remote.replace(/\.git$/, '')}
                      <a
                        href="{repoUrl}/commit/{route.git_commit}"
                        target="_blank"
                        rel="noopener"
                        class="text-engage-blue font-mono truncate block hover:underline"
                      >{route.git_commit.slice(0, 12)}</a>
                    {:else}
                      <p class="text-surface-200 font-mono truncate">{route.git_commit.slice(0, 12)}</p>
                    {/if}
                  </div>
                {/if}
              </div>
              <div class="px-4 border-t border-surface-700/50 py-3 mt-auto space-y-3">
                <button class="w-full text-left text-sm text-engage-blue hover:underline" onclick={() => {
                  const d = route.dongle_id
                  const lid = route.local_id
                  let url = `/signals/${d}/${lid}`
                  if (selectionStart > 0 || selectionEnd > 0) url += `/${Math.round(selectionStart)}/${Math.round(selectionEnd)}`
                  window.open(url, 'signals', 'width=900,height=700')
                }}>
                  Logs & Signals
                </button>
                <RouteActions {route} onEnrich={reEnrich} enrichBusy={reEnriching || enriching} />
              </div>
            </div>
          </Tabs.Content>

          <!-- Dashboard tab (replay telemetry) -->
          <Tabs.Content value="dashboard" class="pt-2">
            <div class="px-3 pb-3 overflow-hidden">
              {#if dashSamples === null && dashStarted}
                <div class="flex items-center gap-2 justify-center py-8">
                  <div class="w-4 h-4 border-2 border-engage-green border-t-transparent rounded-full animate-spin"></div>
                  <span class="text-sm text-surface-400">Loading telemetry...</span>
                </div>
              {:else if dashError}
                <p class="text-sm text-red-400 text-center py-4">{dashError}</p>
              {:else if dashSamples && dashSamples.length > 0}
                <SortableGrid
                  items={dashLayout}
                  onReorder={(reordered) => { dashLayout = reordered; saveLayout(dashLayout) }}
                  class="grid grid-cols-2 gap-2 [&>*]:min-w-0"
                >
                  {#each dashLayout as widgetId (widgetId)}
                    {@const def = getWidgetDef(widgetId)}
                    {#if def}
                      <WidgetCard label={def.label} onremove={() => dashRemoveWidget(widgetId)}>
                        {#if def.type === 'digits'}
                          <DigitsWidget
                            value={dashTelemetry[def.fields[0]] ?? 0}
                            unit={def.unit}
                            thresholds={def.thresholds ?? []}
                            scale={def.scale ?? 1}
                            offset={def.offset ?? 0}
                            decimals={def.decimals ?? 0}
                          />
                        {:else if def.type === 'gauge'}
                          <GaugeWidget
                            value={dashTelemetry[def.fields[0]] ?? 0}
                            unit={def.unit}
                            range={def.range}
                            zones={def.zones ?? []}
                            scale={def.scale ?? 1}
                            offset={def.offset ?? 0}
                            color={def.color ?? '#3b82f6'}
                            history={dashFieldHistory(def.fields[0]).map(h => h.v)}
                          />
                        {:else if def.type === 'steering_wheel'}
                          <SteeringWidget
                            angle={dashTelemetry.steeringAngleDeg}
                            steerCmd={dashTelemetry.steerCmd}
                          />
                        {:else if def.type === 'gas_brake_bar'}
                          <GasBrakeWidget
                            gasPressed={dashTelemetry.gasPressed}
                            brakePressed={dashTelemetry.brakePressed}
                            accelCmd={dashTelemetry.accelCmd}
                          />
                        {:else if def.type === 'engagement_badge'}
                          <EngagementWidget
                            sdState={dashTelemetry.sdState}
                            sdEnabled={dashTelemetry.sdEnabled}
                            cruiseSpeed={dashTelemetry.cruiseSpeed}
                            isMetric={$isMetric}
                          />
                        {:else if def.type === 'sparkline'}
                          <SparklineWidget
                            history={dashFieldHistory(def.fields[0])}
                            scale={def.scale ?? 1}
                            color={def.color ?? '#3b82f6'}
                            cursorTime={currentTime}
                          />
                        {:else if def.type === 'sparkline_multi'}
                          <SparklineMultiWidget
                            histories={dashMultiFieldHistories(def.fields)}
                            colors={def.colors ?? []}
                            labels={def.labels ?? []}
                            cursorTime={currentTime}
                          />
                        {/if}
                      </WidgetCard>
                    {/if}
                  {/each}
                </SortableGrid>
                {#if dashLoadingCount > 0 && dashTotalSegs > 1}
                  <p class="text-xs text-surface-400 text-center mt-2">
                    Loading {dashLoadedSegs.size}/{dashTotalSegs} segments...
                  </p>
                {/if}
                <button
                  class="mt-2 w-full btn-ghost text-xs text-surface-400 py-1.5"
                  onclick={() => showWidgetPicker = true}
                >
                  <svg class="w-3.5 h-3.5 inline-block mr-1" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
                    <path d="M12 4v16m-8-8h16" />
                  </svg>
                  Add Widget
                </button>
              {:else if dashSamples}
                <p class="text-sm text-surface-500 text-center py-4">No telemetry data available</p>
              {/if}
            </div>
          </Tabs.Content>

          <!-- Notes tab (note + bookmarks) -->
          <Tabs.Content value="note" class="pt-2">
            <div class="px-4 pb-4 space-y-3">
              {#if editingNote}
                <!-- svelte-ignore a11y_autofocus -->
                <textarea
                  bind:this={noteTextarea}
                  class="w-full bg-surface-700 text-surface-100 rounded p-2 text-sm resize-none overflow-hidden outline-none focus:ring-1 focus:ring-surface-500"
                  rows="3"
                  placeholder="Add a note..."
                  bind:value={noteText}
                  onblur={saveNoteHandler}
                  oninput={(e) => { e.target.style.height = 'auto'; e.target.style.height = e.target.scrollHeight + 'px' }}
                  onfocus={(e) => { e.target.style.height = 'auto'; e.target.style.height = e.target.scrollHeight + 'px' }}
                  autofocus
                ></textarea>
              {:else if noteText}
                <button
                  type="button"
                  class="w-full text-left text-sm cursor-pointer text-surface-200 note-rendered line-clamp-3"
                  onclick={() => { editingNote = true }}
                >{@html snarkdown(noteText)}</button>
              {:else}
                <button
                  type="button"
                  class="w-full text-left text-sm cursor-pointer text-surface-500"
                  onclick={() => { editingNote = true }}
                >Add a note...</button>
              {/if}

              <!-- Bookmarks -->
              <div class="border-t border-surface-700/50 pt-3">
                <div class="flex items-center justify-between mb-2">
                  <p class="text-xs text-surface-500">Bookmarks</p>
                  {#if !addingBookmark}
                    <button
                      class="text-xs text-engage-blue hover:text-blue-300 cursor-pointer"
                      onclick={() => { addingBookmark = true; newBookmarkLabel = '' }}
                    >+ Add</button>
                  {/if}
                </div>

                {#if addingBookmark}
                  {@const captureSec = currentTime}
                  {@const capMm = Math.floor(captureSec / 60)}
                  {@const capSs = Math.floor(captureSec % 60).toString().padStart(2, '0')}
                  <div class="flex items-center gap-2 mb-2">
                    <span class="text-xs font-mono text-surface-400 shrink-0">{capMm}:{capSs}</span>
                    <!-- svelte-ignore a11y_autofocus -->
                    <input
                      type="text"
                      class="flex-1 bg-surface-700 text-surface-100 rounded px-2 py-1 text-xs outline-none focus:ring-1 focus:ring-surface-500"
                      placeholder="What's happening here?"
                      bind:value={newBookmarkLabel}
                      onkeydown={(e) => { if (e.key === 'Enter') addBookmarkHandler(); if (e.key === 'Escape') addingBookmark = false }}
                      autofocus
                    />
                    <button
                      class="text-xs text-surface-500 hover:text-surface-300 cursor-pointer"
                      onclick={() => addingBookmark = false}
                    >Cancel</button>
                  </div>
                {/if}

                {#if metaBookmarks.length}
                  <div class="divide-y divide-surface-700/50">
                    {#each metaBookmarks as bm, i}
                      {@const mm = Math.floor(bm.time_sec / 60)}
                      {@const ss = Math.floor(bm.time_sec % 60).toString().padStart(2, '0')}
                      {@const isActive = currentTime >= bm.time_sec - 2 && (i + 1 >= metaBookmarks.length || currentTime < metaBookmarks[i + 1].time_sec - 2)}
                      <div class="flex items-center gap-2 group py-1.5">
                        <button
                          class="shrink-0 px-2 py-1 text-xs font-mono rounded cursor-pointer"
                          class:bg-engage-green={isActive}
                          class:text-black={isActive}
                          class:bg-surface-700={!isActive}
                          class:text-surface-200={!isActive}
                          class:hover:bg-surface-600={!isActive}
                          onclick={() => handleSeek(Math.max(0, bm.time_sec - 2))}
                        >{mm}:{ss}</button>
                        {#if editingBookmarkIdx === i}
                          <!-- svelte-ignore a11y_autofocus -->
                          <textarea
                            class="flex-1 bg-surface-700 text-surface-100 rounded px-2 py-1 text-sm outline-none focus:ring-1 focus:ring-surface-500 resize-none overflow-hidden"
                            rows="1"
                            bind:value={editingBookmarkLabel}
                            onkeydown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); updateBookmarkHandler(i) } if (e.key === 'Escape') { editingBookmarkIdx = -1; e.target.onblur = null } }}
                            onblur={() => { if (editingBookmarkIdx === i) updateBookmarkHandler(i) }}
                            oninput={(e) => { e.target.style.height = 'auto'; e.target.style.height = e.target.scrollHeight + 'px' }}
                            onfocus={(e) => { e.target.style.height = 'auto'; e.target.style.height = e.target.scrollHeight + 'px' }}
                            autofocus
                          ></textarea>
                        {:else}
                          <button
                            type="button"
                            class="flex-1 text-left text-sm text-surface-300 cursor-pointer hover:text-surface-100 whitespace-pre-wrap break-words line-clamp-2"
                            onclick={() => { editingBookmarkIdx = i; editingBookmarkLabel = bm.label }}
                          >{bm.label}</button>
                        {/if}
                        <button
                          class="shrink-0 text-surface-500/40 hover:text-surface-200 opacity-0 group-hover:opacity-100 transition-opacity cursor-pointer p-1 {bookmarkExportBusy === i ? 'animate-pulse' : ''}"
                          title="Export HD frame with EXIF"
                          onclick={() => exportBookmarkFrame(i)}
                          disabled={bookmarkExportBusy >= 0}
                        >
                          <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor" class="w-4 h-4">
                            <path fill-rule="evenodd" d="M1 8a2 2 0 012-2h.93a2 2 0 001.664-.89l.812-1.22A2 2 0 018.07 3h3.86a2 2 0 011.664.89l.812 1.22A2 2 0 0016.07 6H17a2 2 0 012 2v7a2 2 0 01-2 2H3a2 2 0 01-2-2V8zm13.5 3a4.5 4.5 0 11-9 0 4.5 4.5 0 019 0zM10 14a3 3 0 100-6 3 3 0 000 6z" clip-rule="evenodd" />
                          </svg>
                        </button>
                        <button
                          class="shrink-0 text-red-500/40 hover:text-red-400 opacity-0 group-hover:opacity-100 transition-opacity cursor-pointer p-1"
                          title="Delete bookmark"
                          onclick={() => deleteBookmarkHandler(i)}
                        >
                          <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor" class="w-4 h-4">
                            <path fill-rule="evenodd" d="M8.75 1A2.75 2.75 0 006 3.75v.443c-.795.077-1.584.176-2.365.298a.75.75 0 10.23 1.482l.149-.022.841 10.518A2.75 2.75 0 007.596 19h4.807a2.75 2.75 0 002.742-2.53l.841-10.52.149.023a.75.75 0 00.23-1.482A41.03 41.03 0 0014 4.193V3.75A2.75 2.75 0 0011.25 1h-2.5zM10 4c.84 0 1.673.025 2.5.075V3.75c0-.69-.56-1.25-1.25-1.25h-2.5c-.69 0-1.25.56-1.25 1.25v.325C8.327 4.025 9.16 4 10 4zM8.58 7.72a.75.75 0 00-1.5.06l.3 7.5a.75.75 0 101.5-.06l-.3-7.5zm4.34.06a.75.75 0 10-1.5-.06l-.3 7.5a.75.75 0 101.5.06l.3-7.5z" clip-rule="evenodd" />
                          </svg>
                        </button>
                      </div>
                    {/each}
                  </div>
                {:else if !addingBookmark}
                  <p class="text-xs text-surface-600">No bookmarks yet. Pause at a moment and click + Add.</p>
                {/if}
              </div>
            </div>
          </Tabs.Content>

        </Tabs.Root>
      </div>
    </div>
  {/if}
</div>

{#if showWidgetPicker}
  <WidgetPicker
    registry={WIDGET_REGISTRY}
    activeIds={dashLayout}
    onpick={dashAddWidget}
    onclose={() => showWidgetPicker = false}
  />
{/if}

<style>
  .fullscreen-container,
  :global(.ios-fullscreen) {
    display: flex;
    flex-direction: column;
    height: 100vh;
    height: 100dvh; /* dynamic viewport height — excludes mobile browser chrome */
    background: black;
    gap: 0;
  }
  :global(.ios-fullscreen) {
    position: fixed;
    inset: 0;
    z-index: 9999;
  }
  .fullscreen-video {
    flex: 1;
    min-height: 0;
    border-radius: 0 !important;
  }
  .fullscreen-video :global(> div:not(.fullscreen-controls)) {
    /* Override the fixed aspect-ratio so video fills available space */
    aspect-ratio: unset !important;
    height: 100%;
  }
  .fullscreen-controls {
    position: absolute;
    bottom: 8px;
    left: 50%;
    transform: translateX(-50%);
    width: fit-content;
    max-width: 90%;
    padding: 4px 12px;
    background: rgba(0,0,0,0.8);
    border-radius: 8px;
    z-index: 10;
  }
  .hud-corners {
    border-radius: 4.22% / 8.44%;
  }
  .recording-border-static {
    border: 2px solid #ef4444;
  }
  .recording-border-blink {
    border: 2px solid transparent;
    animation: recording-blink 4s ease-in-out infinite;
  }
  @keyframes recording-blink {
    0%, 100% { border-color: transparent; }
    50% { border-color: #ef4444; }
  }
  .note-rendered :global(h1) { font-size: 1.25em; font-weight: 600; margin: 0.4em 0 0.2em; }
  .note-rendered :global(h2) { font-size: 1.1em; font-weight: 600; margin: 0.4em 0 0.2em; }
  .note-rendered :global(h3) { font-size: 1em; font-weight: 600; margin: 0.3em 0 0.2em; }
  .note-rendered :global(p) { margin: 0.25em 0; }
  .note-rendered :global(a) { color: #58a6ff; text-decoration: underline; }
  .note-rendered :global(code) { background: rgba(255,255,255,0.08); padding: 0.1em 0.3em; border-radius: 3px; font-size: 0.85em; }
  .note-rendered :global(strong) { color: #e2e8f0; }
  .note-rendered :global(ul) { padding-left: 1.2em; margin: 0.25em 0; list-style: disc; }
  .note-rendered :global(ol) { padding-left: 1.2em; margin: 0.25em 0; list-style: decimal; }
  .note-rendered :global(li) { margin: 0.1em 0; }
  .dl-spinner {
    width: 12px; height: 12px;
    border: 2px solid rgba(34, 197, 94, 0.3);
    border-top-color: #22c55e;
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
    flex-shrink: 0;
  }
  @keyframes spin { to { transform: rotate(360deg) } }
  .dl-rec-dot {
    width: 10px; height: 10px;
    border-radius: 50%;
    background: #ef4444;
    animation: recording-blink-dot 4s ease-in-out infinite;
    flex-shrink: 0;
  }
  @keyframes recording-blink-dot {
    0%, 100% { opacity: 0; }
    50% { opacity: 1; }
  }
</style>
