<script>
  import { onMount, onDestroy } from 'svelte'
  import Hls from 'hls.js'
  import { spriteUrl, cameraUrl, mjpegUrl } from '../api.js'
  import HudOverlay from './HudOverlay.svelte'

  /**
   * Route video player with four modes:
   * 1. MJPEG stream (universal, no codec needed) — default when HEVC unsupported
   * 2. HLS video (qcamera.m3u8) — for browsers with HLS/HEVC support
   * 3. HD camera video (fcamera/ecamera/dcamera per-segment MP4) — HEVC browsers
   * 4. HUD overlay canvas (lane lines, path, speed from rlog data)
   */

  let {
    route,
    files,
    hdSource = null,
    frozen = false,
    selectionStart = 0,
    selectionEnd = 0,
    currentTime = $bindable(0),
    duration = $bindable(0),
    onTimeUpdate,
    onDurationChange,
    onPlay,
    onPause,
    useMjpeg = false,
    hudFrames = [],
    showHud = false,
    onHudToggle,
    onHudDownload,
    onHevcFailed,
  } = $props()

  let videoEl = $state(null)
  let hdVideoEl = $state(null)
  let mjpegEl = $state(null)
  let hls = null
  let isPlaying = $state(false)
  let isMuted = $state(true)
  let buffering = $state(true)
  let userWantsPause = false

  // HD (fcamera) state
  let hdSegment = $state(-1)

  // Track which video is active
  const showingHd = $derived(!!hdSource)
  const showingMjpeg = $derived(useMjpeg && !showingHd)
  const activeVideo = $derived(showingHd ? hdVideoEl : videoEl)

  const posterUrl = $derived(route ? spriteUrl(route, 0) : null)

  // ── HLS/MJPEG initialization ──────────────────────────────

  function initPlayer() {
    if (!videoEl || !files?.qcameras) return

    // MJPEG mode: skip HLS, set duration from segment count
    if (useMjpeg) {
      cleanupHls()
      const segCount = files.qcameras?.length || 0
      if (segCount > 0) {
        duration = segCount * 60
        onDurationChange?.(duration)
      }
      buffering = false
      return
    }

    buffering = true
    cleanupHls()

    const routeName = route.local_id || route.fullname
    const hlsManifestUrl = `/v1/route/${routeName}/qcamera.m3u8`

    if (videoEl.canPlayType('application/vnd.apple.mpegurl')) {
      videoEl.src = hlsManifestUrl
      videoEl.addEventListener('loadedmetadata', () => {
        if (!frozen) videoEl.play().catch(() => {})
      }, { once: true })
    } else if (Hls.isSupported()) {
      hls = new Hls({
        enableWorker: true,
        lowLatencyMode: false,
        progressive: true,
        startLevel: 0,
        maxBufferLength: 90,
        maxMaxBufferLength: 300,
        backBufferLength: 60,
        maxBufferHole: 0.1,
        nudgeOffset: 0.05,
        nudgeMaxRetry: 10,
        startFragPrefetch: true,
        highBufferWatchdogPeriod: 1,
        abrEwmaDefaultEstimate: 10_000_000,
        testBandwidth: false,
        forceKeyFrameOnDiscontinuity: false,
        fragLoadingTimeOut: 30000,
        fragLoadingMaxRetry: 5,
        levelLoadingTimeOut: 10000,
      })
      hls.loadSource(hlsManifestUrl)
      hls.attachMedia(videoEl)
      hls.on(Hls.Events.MANIFEST_PARSED, () => {
        if (!frozen) videoEl.play().catch(() => {})
      })
      hls.on(Hls.Events.ERROR, (_event, data) => {
        if (data.fatal) {
          if (data.type === Hls.ErrorTypes.MEDIA_ERROR) hls.recoverMediaError()
          else if (data.type === Hls.ErrorTypes.NETWORK_ERROR) hls.startLoad()
        }
      })
    }
  }

  function cleanupHls() {
    if (hls) { hls.destroy(); hls = null }
  }

  // ── MJPEG playback ────────────────────────────────────────

  let mjpegStartTime = 0
  let mjpegStartWall = 0
  let mjpegTimer = null
  let mjpegPlaying = $state(false)
  let mjpegRate = 1.0

  function startMjpeg(t = 0) {
    if (!mjpegEl || !route) return
    clearInterval(mjpegTimer)
    const segAligned = Math.floor(t / 60) * 60
    mjpegEl.src = mjpegUrl(route.local_id, t, 20, 5) + `&speed=${mjpegRate}&_=${Date.now()}`
    mjpegStartTime = segAligned
    mjpegStartWall = Date.now() / 1000
    currentTime = segAligned
    mjpegPlaying = true
    isPlaying = true
    onPlay?.()
    buffering = false

    mjpegTimer = setInterval(() => {
      const elapsed = Date.now() / 1000 - mjpegStartWall
      currentTime = mjpegStartTime + elapsed * mjpegRate
      onTimeUpdate?.(currentTime)
    }, 200)
  }

  function stopMjpeg() {
    clearInterval(mjpegTimer)
    mjpegTimer = null
    if (mjpegEl) mjpegEl.src = ''
    mjpegPlaying = false
    isPlaying = false
    onPause?.()
  }

  function cleanupMjpeg() {
    clearInterval(mjpegTimer)
    mjpegTimer = null
    if (mjpegEl) mjpegEl.src = ''
    mjpegPlaying = false
  }

  // ── HLS event handlers ────────────────────────────────────

  function handleSdTimeUpdate() {
    if (!videoEl || showingHd || showingMjpeg) return
    currentTime = videoEl.currentTime
    onTimeUpdate?.(videoEl.currentTime)
  }

  function handleSdDurationChange() {
    if (!videoEl || showingMjpeg) return
    duration = videoEl.duration
    onDurationChange?.(videoEl.duration)
  }

  function handleSdPlay() {
    if (showingHd || showingMjpeg) return
    if (frozen) { videoEl?.pause(); return }
    if (userWantsPause) { videoEl?.pause(); return }
    isPlaying = true
    onPlay?.()
  }

  function handleSdPause() {
    if (showingHd || showingMjpeg) return
    isPlaying = false
    onPause?.()
  }

  // ── HD (fcamera) playback ─────────────────────────────────

  const maxSegment = $derived(files?.qcameras ? files.qcameras.length - 1 : 0)

  function loadHdSegment(seg, seekOffset = 0, autoPlay = false) {
    if (!hdVideoEl || !route || !hdSource) return
    if (seg < 0 || seg > maxSegment) return

    hdSegment = seg
    const url = cameraUrl(route.local_id, hdSource, seg)
    hdVideoEl.src = url
    hdVideoEl.load()
    hdVideoEl.addEventListener('loadedmetadata', () => {
      if (seekOffset > 0) hdVideoEl.currentTime = seekOffset
      if (autoPlay) hdVideoEl.play().catch(() => {})
    }, { once: true })
    hdVideoEl.addEventListener('error', () => {
      onHevcFailed?.()
    }, { once: true })
  }

  function handleHdTimeUpdate() {
    if (!hdVideoEl || !showingHd) return
    const t = hdSegment * 60 + hdVideoEl.currentTime
    currentTime = t
    onTimeUpdate?.(t)
  }

  function handleHdEnded() {
    if (!showingHd) return
    const segEndTime = (hdSegment + 1) * 60
    if (selectionEnd > 0 && segEndTime >= selectionEnd) {
      onTimeUpdate?.(segEndTime)
      return
    }
    const nextSeg = hdSegment + 1
    if (nextSeg <= maxSegment) {
      loadHdSegment(nextSeg, 0, true)
    } else {
      isPlaying = false
      onPause?.()
    }
  }

  function handleHdPlay() {
    if (!showingHd) return
    if (frozen) { hdVideoEl?.pause(); return }
    isPlaying = true
    onPlay?.()
  }

  function handleHdPause() {
    if (!showingHd) return
    isPlaying = false
    onPause?.()
  }

  // ── Effects ───────────────────────────────────────────────

  // HD source transitions
  let prevHdSource = null
  $effect(() => {
    const entering = !!hdSource && !prevHdSource
    const switching = !!hdSource && !!prevHdSource && hdSource !== prevHdSource
    const leaving = !hdSource && !!prevHdSource
    prevHdSource = hdSource

    if ((entering || switching) && hdVideoEl) {
      videoEl?.pause()
      userWantsPause = true
      isPlaying = false
      onPause?.()
      const seg = Math.floor(currentTime / 60)
      const offset = currentTime % 60
      loadHdSegment(seg, offset)
    } else if (leaving && hdVideoEl) {
      hdVideoEl.pause()
      hdVideoEl.removeAttribute('src')
      hdVideoEl.load()
      hdSegment = -1
      userWantsPause = true
      isPlaying = false
      onPause?.()
      if (videoEl) {
        videoEl.pause()
        videoEl.currentTime = currentTime
      }
    }
  })

  // MJPEG mode cleanup
  let prevUseMjpeg = false
  $effect(() => {
    if (useMjpeg) {
      prevUseMjpeg = true
    } else if (prevUseMjpeg) {
      cleanupMjpeg()
      prevUseMjpeg = false
    }
  })

  // Freeze
  $effect(() => {
    if (frozen) {
      if (videoEl && !videoEl.paused) videoEl.pause()
      if (hdVideoEl && !hdVideoEl.paused) hdVideoEl.pause()
    }
  })

  // Route switch
  $effect(() => {
    if (files) initPlayer()
  })

  function cleanup() {
    cleanupHls()
    cleanupMjpeg()
  }

  onDestroy(cleanup)

  // ── Exported controls ─────────────────────────────────────

  export function seek(time) {
    if (showingMjpeg) {
      currentTime = time
      if (mjpegPlaying) startMjpeg(time)
      return
    }
    if (showingHd && hdVideoEl) {
      const seg = Math.floor(time / 60)
      const offset = time % 60
      if (seg !== hdSegment) {
        const wasPlaying = !hdVideoEl.paused
        loadHdSegment(seg, offset, wasPlaying)
      } else {
        hdVideoEl.currentTime = offset
      }
      return
    }
    if (videoEl) {
      userWantsPause = false
      const wasPlaying = !videoEl.paused
      videoEl.currentTime = time
      if (wasPlaying) videoEl.play().catch(() => {})
    }
  }

  export function play() {
    if (showingMjpeg) { startMjpeg(currentTime); return }
    userWantsPause = false
    activeVideo?.play().catch(() => {})
  }

  export function pause() {
    if (showingMjpeg) { stopMjpeg(); return }
    userWantsPause = true
    activeVideo?.pause()
  }

  export function toggle() {
    if (showingMjpeg) {
      if (mjpegPlaying) stopMjpeg()
      else startMjpeg(currentTime)
      return
    }
    const v = activeVideo
    if (!v) return
    if (v.paused) { userWantsPause = false; v.play().catch(() => {}) }
    else { userWantsPause = true; v.pause() }
  }

  export function setPlaybackRate(rate) {
    if (showingMjpeg) {
      mjpegRate = rate
      if (mjpegPlaying) startMjpeg(currentTime)
      return
    }
    if (videoEl) videoEl.playbackRate = rate
  }

  export function toggleMute() {
    isMuted = !isMuted
    if (videoEl) videoEl.muted = isMuted
    return isMuted
  }

  export function getMuted() {
    return isMuted
  }
</script>

<div class="relative w-full group bg-black" style="aspect-ratio: 1928/1208; contain: strict">
  <!-- HLS video (qcamera) -->
  <video
    bind:this={videoEl}
    class="absolute inset-0 w-full h-full object-cover"
    class:invisible={showingHd}
    class:hidden={showingMjpeg}
    muted
    playsinline
    disablepictureinpicture
    webkit-playsinline
    x5-video-player-type="h5"
    preload="auto"
    ontimeupdate={handleSdTimeUpdate}
    ondurationchange={handleSdDurationChange}
    onplay={handleSdPlay}
    onpause={handleSdPause}
    onended={handleSdPause}
    onwaiting={() => { if (!showingHd) buffering = true }}
    onplaying={() => { if (!showingHd) buffering = false }}
    oncanplay={() => { if (!showingHd) buffering = false }}
  >
    Your browser does not support video playback.
  </video>

  <!-- HD camera video (fcamera/ecamera/dcamera) -->
  <video
    bind:this={hdVideoEl}
    class="absolute inset-0 w-full h-full object-cover"
    class:invisible={!showingHd}
    muted={isMuted}
    playsinline
    disablepictureinpicture
    webkit-playsinline
    x5-video-player-type="h5"
    preload="auto"
    ontimeupdate={handleHdTimeUpdate}
    onplay={handleHdPlay}
    onpause={handleHdPause}
    onended={handleHdEnded}
    onwaiting={() => { if (showingHd) buffering = true }}
    onplaying={() => { if (showingHd) buffering = false }}
    oncanplay={() => { if (showingHd) buffering = false }}
  >
  </video>

  <!-- MJPEG stream (universal fallback) -->
  {#if useMjpeg}
    <img
      bind:this={mjpegEl}
      class="absolute inset-0 w-full h-full object-cover"
      class:hidden={!showingMjpeg}
      alt="Route video"
    />
  {/if}

  <!-- HUD overlay canvas -->
  <HudOverlay frames={hudFrames} {currentTime} visible={showHud} />

  <!-- Loading indicator -->
  {#if !files?.qcameras?.some(u => u)}
    <div class="absolute inset-0 flex items-center justify-center bg-surface-900">
      <p class="text-surface-400 text-sm">No video available</p>
    </div>
  {:else if buffering}
    <div class="absolute inset-0 z-10 flex items-center justify-center pointer-events-none">
      <div class="w-8 h-8 border-3 border-white/30 border-t-white rounded-full animate-spin drop-shadow-lg"></div>
    </div>
  {/if}

  <!-- Hover overlay: HUD buttons -->
  {#if !frozen && (onHudToggle || onHudDownload)}
    <div class="absolute inset-0 hidden sm:flex items-center justify-center gap-4
      opacity-0 group-hover:opacity-100 transition-opacity duration-200 pointer-events-none">
      {#if onHudToggle}
        <button
          class="pointer-events-auto flex items-center gap-2 px-4 py-2.5 rounded-lg backdrop-blur-sm transition-colors
            {showHud ? 'bg-blue-600/70 hover:bg-blue-600/90 border border-blue-400' : 'bg-black/50 hover:bg-black/70 border border-transparent hover:border-blue-500'}"
          title={showHud ? 'Hide HUD Overlay' : 'Show HUD Overlay'}
          onclick={onHudToggle}
        >
          <svg class="w-5 h-5 text-white" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
            <path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7S2 12 2 12z"/>
            <circle cx="12" cy="12" r="3"/>
          </svg>
          <span class="text-white text-sm font-medium">{showHud ? 'HUD On' : 'HUD Overlay'}</span>
        </button>
      {/if}
      {#if onHudDownload}
        <button
          class="pointer-events-auto flex items-center gap-2 px-4 py-2.5 rounded-lg bg-black/50 hover:bg-black/70 border border-transparent hover:border-blue-500 backdrop-blur-sm transition-colors"
          title="Download HUD Video"
          onclick={onHudDownload}
        >
          <svg class="w-5 h-5 text-white" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
            <polyline points="7 10 12 15 17 10"/>
            <line x1="12" y1="15" x2="12" y2="3"/>
          </svg>
          <span class="text-white text-sm font-medium">HUD Download</span>
        </button>
      {/if}
    </div>
  {/if}
</div>
