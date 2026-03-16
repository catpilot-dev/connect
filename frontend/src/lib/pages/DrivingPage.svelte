<script>
  import { onMount, onDestroy } from 'svelte'
  import { isMetric } from '../stores.js'
  import { liveStreamOffer } from '../api.js'

  // ── State ──
  let videoEl = $state(null)
  let canvasEl = $state(null)
  let pc = $state(null)
  let ws = $state(null)
  let connected = $state(false)
  let wsConnected = $state(false)
  let error = $state(null)
  let fullscreen = $state(false)

  // Telemetry from dashboard WS
  let telemetry = $state({
    vEgo: 0,
    steeringAngleDeg: 0,
    gasPressed: false,
    brakePressed: false,
    cruiseSpeed: 0,
    cruiseEnabled: false,
    sdState: '',
    sdEnabled: false,
    alertText1: '',
    alertText2: '',
    alertType: '',
  })

  // Model data for HUD overlay
  let modelData = $state(null)

  // ── WebRTC: live camera feed ──
  async function connectWebRTC() {
    error = null

    const rtc = new RTCPeerConnection({
      iceServers: [{ urls: 'stun:stun.l.google.com:19302' }],
    })
    pc = rtc

    rtc.onconnectionstatechange = () => {
      console.log('[Driving] WebRTC state:', rtc.connectionState)
      connected = rtc.connectionState === 'connected'
      if (rtc.connectionState === 'failed') {
        error = 'WebRTC connection failed'
      }
    }

    // Receive-only video
    rtc.addTransceiver('video', { direction: 'recvonly' })

    rtc.ontrack = (ev) => {
      console.log('[Driving] ontrack:', ev.track.kind)
      const stream = ev.streams[0] || new MediaStream([ev.track])
      if (videoEl) {
        videoEl.srcObject = stream
        videoEl.play().catch(e => console.error('[Driving] play error:', e))
      }
    }

    // Create offer and gather ICE candidates
    const offer = await rtc.createOffer()
    await rtc.setLocalDescription(offer)

    // Wait for ICE gathering (LAN = instant, timeout 3s)
    await new Promise((resolve) => {
      if (rtc.iceGatheringState === 'complete') { resolve(); return }
      const timer = setTimeout(resolve, 3000)
      rtc.onicegatheringstatechange = () => {
        if (rtc.iceGatheringState === 'complete') { clearTimeout(timer); resolve() }
      }
    })

    // Exchange SDP with webrtcd via COD proxy
    try {
      const answer = await liveStreamOffer(rtc.localDescription.sdp)
      await rtc.setRemoteDescription(new RTCSessionDescription(answer))
    } catch (e) {
      error = `WebRTC signaling failed: ${e.message}`
      console.error('[Driving] signaling error:', e)
    }
  }

  function disconnectWebRTC() {
    if (pc) {
      pc.close()
      pc = null
    }
    connected = false
  }

  // ── WebSocket: telemetry + model data ──
  function connectTelemetry() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws'
    const socket = new WebSocket(`${proto}://${location.host}/ws/driving`)
    socket.onopen = () => { wsConnected = true }
    socket.onclose = () => {
      wsConnected = false
      // Reconnect after 2s
      setTimeout(connectTelemetry, 2000)
    }
    socket.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data)
        if (msg.type === 'telemetry') {
          telemetry = msg.data
        } else if (msg.type === 'model') {
          modelData = msg.data
          requestAnimationFrame(renderHud)
        }
      } catch {}
    }
    ws = socket
  }

  function disconnectTelemetry() {
    if (ws) { ws.close(); ws = null }
    wsConnected = false
  }

  // ── HUD Canvas Rendering ──
  function renderHud() {
    if (!canvasEl || !videoEl) return
    const ctx = canvasEl.getContext('2d')
    const w = canvasEl.width
    const h = canvasEl.height

    ctx.clearRect(0, 0, w, h)

    // Speed display (center-bottom)
    const speed = $isMetric
      ? (telemetry.vEgo * 3.6)          // m/s → km/h
      : (telemetry.vEgo * 2.23694)       // m/s → mph
    const speedUnit = $isMetric ? 'km/h' : 'mph'

    // Speed text
    ctx.save()
    ctx.fillStyle = 'rgba(255, 255, 255, 0.95)'
    ctx.font = `bold ${Math.round(h * 0.12)}px system-ui, -apple-system, sans-serif`
    ctx.textAlign = 'center'
    ctx.textBaseline = 'bottom'
    ctx.shadowColor = 'rgba(0,0,0,0.6)'
    ctx.shadowBlur = 6
    ctx.fillText(Math.round(speed), w * 0.5, h * 0.88)

    ctx.font = `${Math.round(h * 0.04)}px system-ui, -apple-system, sans-serif`
    ctx.fillStyle = 'rgba(255, 255, 255, 0.7)'
    ctx.fillText(speedUnit, w * 0.5, h * 0.92)
    ctx.restore()

    // Engagement status (top-center)
    const engaged = telemetry.sdEnabled
    ctx.save()
    const statusColor = engaged ? 'rgba(23, 200, 84, 0.9)' : 'rgba(255, 255, 255, 0.5)'
    const statusText = engaged ? 'ENGAGED' : telemetry.sdState || 'OFF'
    ctx.fillStyle = statusColor
    ctx.font = `bold ${Math.round(h * 0.04)}px system-ui, -apple-system, sans-serif`
    ctx.textAlign = 'center'
    ctx.textBaseline = 'top'
    ctx.shadowColor = 'rgba(0,0,0,0.5)'
    ctx.shadowBlur = 4
    ctx.fillText(statusText.toUpperCase(), w * 0.5, h * 0.04)
    ctx.restore()

    // Cruise speed (top-right)
    if (telemetry.cruiseEnabled) {
      const cruiseKmh = $isMetric
        ? telemetry.cruiseSpeed * 3.6
        : telemetry.cruiseSpeed * 2.23694
      ctx.save()
      ctx.fillStyle = 'rgba(255, 255, 255, 0.8)'
      ctx.font = `bold ${Math.round(h * 0.06)}px system-ui, -apple-system, sans-serif`
      ctx.textAlign = 'right'
      ctx.textBaseline = 'top'
      ctx.shadowColor = 'rgba(0,0,0,0.5)'
      ctx.shadowBlur = 4
      ctx.fillText(Math.round(cruiseKmh), w * 0.95, h * 0.04)

      ctx.font = `${Math.round(h * 0.03)}px system-ui, -apple-system, sans-serif`
      ctx.fillStyle = 'rgba(255, 255, 255, 0.5)'
      ctx.fillText('SET', w * 0.95, h * 0.11)
      ctx.restore()
    }

    // Alerts (center)
    if (telemetry.alertText1) {
      ctx.save()
      // Alert color based on type
      let alertColor = 'rgba(255, 255, 255, 0.95)'
      if (telemetry.alertType === 'critical') alertColor = 'rgba(255, 59, 48, 0.95)'
      else if (telemetry.alertType === 'warning') alertColor = 'rgba(255, 149, 0, 0.95)'

      ctx.fillStyle = alertColor
      ctx.font = `bold ${Math.round(h * 0.06)}px system-ui, -apple-system, sans-serif`
      ctx.textAlign = 'center'
      ctx.textBaseline = 'middle'
      ctx.shadowColor = 'rgba(0,0,0,0.7)'
      ctx.shadowBlur = 8
      ctx.fillText(telemetry.alertText1, w * 0.5, h * 0.4)

      if (telemetry.alertText2) {
        ctx.font = `${Math.round(h * 0.04)}px system-ui, -apple-system, sans-serif`
        ctx.fillText(telemetry.alertText2, w * 0.5, h * 0.48)
      }
      ctx.restore()
    }

    // Lane lines (from model data)
    if (modelData?.laneLines) {
      drawLaneLines(ctx, w, h, modelData.laneLines, modelData.laneLineProbs)
    }

    // Path prediction
    if (modelData?.path) {
      drawPath(ctx, w, h, modelData.path, engaged)
    }

    // Lead car indicator
    if (modelData?.lead) {
      drawLead(ctx, w, h, modelData.lead)
    }
  }

  // Draw lane lines projected onto canvas
  function drawLaneLines(ctx, w, h, lines, probs) {
    const colors = [
      'rgba(255, 255, 255, 0.6)',   // left outer
      'rgba(255, 200, 50, 0.8)',    // left inner
      'rgba(255, 200, 50, 0.8)',    // right inner
      'rgba(255, 255, 255, 0.6)',   // right outer
    ]

    for (let i = 0; i < lines.length && i < 4; i++) {
      const line = lines[i]
      const prob = probs?.[i] ?? 1.0
      if (prob < 0.3 || !line || line.length < 2) continue

      ctx.save()
      ctx.strokeStyle = colors[i]
      ctx.lineWidth = Math.max(2, w * 0.003)
      ctx.globalAlpha = prob
      ctx.beginPath()

      for (let j = 0; j < line.length; j++) {
        const pt = line[j]
        // pt = {x, y} in canvas-normalized coords (0-1)
        const cx = pt.x * w
        const cy = pt.y * h
        if (j === 0) ctx.moveTo(cx, cy)
        else ctx.lineTo(cx, cy)
      }
      ctx.stroke()
      ctx.restore()
    }
  }

  // Draw predicted path
  function drawPath(ctx, w, h, path, engaged) {
    if (!path || path.length < 2) return

    ctx.save()
    const color = engaged ? 'rgba(23, 200, 84, 0.4)' : 'rgba(100, 149, 237, 0.3)'

    ctx.fillStyle = color
    ctx.beginPath()

    // Draw as filled polygon (left edge forward, right edge backward)
    const leftEdge = path.map(p => ({ x: (p.x - p.width * 0.5) * w, y: p.y * h }))
    const rightEdge = path.map(p => ({ x: (p.x + p.width * 0.5) * w, y: p.y * h }))

    ctx.moveTo(leftEdge[0].x, leftEdge[0].y)
    for (const p of leftEdge) ctx.lineTo(p.x, p.y)
    for (let i = rightEdge.length - 1; i >= 0; i--) ctx.lineTo(rightEdge[i].x, rightEdge[i].y)
    ctx.closePath()
    ctx.fill()
    ctx.restore()
  }

  // Draw lead car indicator
  function drawLead(ctx, w, h, lead) {
    if (!lead || lead.dRel <= 0) return

    ctx.save()
    // Chevron at lead car position
    const cx = (lead.x ?? 0.5) * w
    const cy = (lead.y ?? 0.35) * h
    const size = Math.max(10, w * 0.03)

    ctx.strokeStyle = lead.dRel < 15 ? 'rgba(255, 59, 48, 0.9)' : 'rgba(23, 200, 84, 0.9)'
    ctx.lineWidth = Math.max(2, w * 0.004)
    ctx.lineCap = 'round'
    ctx.lineJoin = 'round'

    ctx.beginPath()
    ctx.moveTo(cx - size, cy + size * 0.5)
    ctx.lineTo(cx, cy - size * 0.5)
    ctx.lineTo(cx + size, cy + size * 0.5)
    ctx.stroke()

    // Distance text
    ctx.fillStyle = 'rgba(255, 255, 255, 0.8)'
    ctx.font = `${Math.round(h * 0.03)}px system-ui, -apple-system, sans-serif`
    ctx.textAlign = 'center'
    ctx.fillText(`${Math.round(lead.dRel)}m`, cx, cy + size)
    ctx.restore()
  }

  // ── Fullscreen ──
  function enterFullscreen() {
    const el = document.documentElement
    if (document.fullscreenElement || document.webkitFullscreenElement) return
    const p = el.requestFullscreen?.() || el.webkitRequestFullscreen?.()
    if (p && p.then) p.then(() => { fullscreen = true }).catch(() => {})
    else fullscreen = true
  }

  function toggleFullscreen() {
    if (document.fullscreenElement || document.webkitFullscreenElement) {
      document.exitFullscreen?.() || document.webkitExitFullscreen?.()
      fullscreen = false
    } else {
      enterFullscreen()
    }
  }

  // Tap on video area → enter fullscreen (user gesture required on iOS)
  function handleVideoTap() {
    if (!fullscreen) enterFullscreen()
  }

  // Resize canvas to match video
  function resizeCanvas() {
    if (!canvasEl || !videoEl) return
    const rect = videoEl.getBoundingClientRect()
    canvasEl.width = rect.width
    canvasEl.height = rect.height
  }

  let resizeObserver
  let animFrame

  onMount(() => {
    connectWebRTC()
    connectTelemetry()

    // Keep canvas sized to video
    resizeObserver = new ResizeObserver(resizeCanvas)
    if (videoEl) resizeObserver.observe(videoEl)

    // Continuous HUD render loop (for telemetry updates without model data)
    function hudLoop() {
      renderHud()
      animFrame = requestAnimationFrame(hudLoop)
    }
    animFrame = requestAnimationFrame(hudLoop)

    // Lock to landscape on mobile
    screen.orientation?.lock?.('landscape').catch(() => {})

    // Track fullscreen changes (e.g. user presses Escape)
    function onFsChange() {
      fullscreen = !!(document.fullscreenElement || document.webkitFullscreenElement)
    }
    document.addEventListener('fullscreenchange', onFsChange)
    document.addEventListener('webkitfullscreenchange', onFsChange)

    return () => {
      screen.orientation?.unlock?.()
      document.removeEventListener('fullscreenchange', onFsChange)
      document.removeEventListener('webkitfullscreenchange', onFsChange)
    }
  })

  onDestroy(() => {
    disconnectWebRTC()
    disconnectTelemetry()
    if (resizeObserver) resizeObserver.disconnect()
    if (animFrame) cancelAnimationFrame(animFrame)
  })
</script>

<!-- svelte-ignore a11y_click_events_have_key_events -->
<!-- svelte-ignore a11y_no_static_element_interactions -->
<div class="driving-container" onclick={handleVideoTap}>
  <!-- Video layer (WebRTC camera feed) -->
  <!-- svelte-ignore a11y_media_has_caption -->
  <video
    bind:this={videoEl}
    class="driving-video"
    autoplay
    muted
    playsinline
  ></video>

  <!-- HUD overlay canvas -->
  <canvas
    bind:this={canvasEl}
    class="driving-canvas"
  ></canvas>

  <!-- Connection status overlay (shown when not connected) -->
  {#if !connected}
    <div class="driving-overlay">
      <div class="status-card">
        {#if error}
          <p class="text-red-400 text-lg">{error}</p>
          <button class="btn-retry" onclick={(e) => { e.stopPropagation(); disconnectWebRTC(); connectWebRTC() }}>
            Retry
          </button>
        {:else}
          <div class="spinner"></div>
          <p class="text-surface-300 mt-3">Connecting to camera...</p>
        {/if}
      </div>
    </div>
  {/if}

  <!-- Tap to fullscreen hint (shown briefly when not fullscreen) -->
  {#if connected && !fullscreen}
    <div class="fullscreen-hint">
      Tap to go fullscreen
    </div>
  {/if}

  <!-- Bottom controls bar -->
  <div class="controls-bar" onclick={(e) => e.stopPropagation()}>
    <!-- Connection indicators -->
    <div class="flex items-center gap-2">
      <div class="indicator" class:indicator-ok={connected} class:indicator-err={!connected}></div>
      <span class="text-xs text-surface-400">{connected ? 'Live' : 'No video'}</span>
    </div>

    <!-- Fullscreen toggle -->
    <button class="btn-control" onclick={toggleFullscreen}>
      {#if fullscreen}
        <svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
          <path d="M9 9L4 4m0 0v4m0-4h4M15 9l5-5m0 0v4m0-4h-4M9 15l-5 5m0 0v-4m0 4h4M15 15l5 5m0 0v-4m0 4h-4"/>
        </svg>
      {:else}
        <svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
          <path d="M4 8V4m0 0h4M4 4l5 5M20 8V4m0 0h-4m4 0l-5 5M4 16v4m0 0h4m-4 0l5-5M20 16v4m0 0h-4m4 0l-5-5"/>
        </svg>
      {/if}
    </button>
  </div>
</div>

<style>
  .driving-container {
    position: relative;
    width: 100vw;
    height: 100dvh;
    background: #000;
    overflow: hidden;
  }

  .driving-video {
    position: absolute;
    top: 0;
    left: 0;
    width: 100%;
    height: 100%;
    object-fit: contain;
    background: #000;
  }

  .driving-canvas {
    position: absolute;
    top: 0;
    left: 0;
    width: 100%;
    height: 100%;
    pointer-events: none;
  }

  .driving-overlay {
    position: absolute;
    top: 0;
    left: 0;
    width: 100%;
    height: 100%;
    display: flex;
    align-items: center;
    justify-content: center;
    background: rgba(0, 0, 0, 0.7);
    z-index: 10;
  }

  .status-card {
    text-align: center;
    padding: 2rem;
  }

  .spinner {
    width: 40px;
    height: 40px;
    border: 3px solid rgba(255, 255, 255, 0.15);
    border-top-color: rgba(255, 255, 255, 0.8);
    border-radius: 50%;
    margin: 0 auto;
    animation: spin 0.8s linear infinite;
  }

  @keyframes spin {
    to { transform: rotate(360deg); }
  }

  .btn-retry {
    margin-top: 1rem;
    padding: 0.5rem 1.5rem;
    background: rgba(255, 255, 255, 0.1);
    color: white;
    border: 1px solid rgba(255, 255, 255, 0.2);
    border-radius: 0.5rem;
    cursor: pointer;
  }
  .btn-retry:hover { background: rgba(255, 255, 255, 0.2); }

  .controls-bar {
    position: absolute;
    bottom: 0;
    left: 0;
    right: 0;
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0.5rem 1rem;
    background: linear-gradient(transparent, rgba(0,0,0,0.6));
    z-index: 5;
  }

  .indicator {
    width: 8px;
    height: 8px;
    border-radius: 50%;
  }
  .indicator-ok { background: #17c854; }
  .indicator-err { background: #ff3b30; }

  .btn-control {
    padding: 0.5rem;
    color: rgba(255, 255, 255, 0.8);
    background: rgba(255, 255, 255, 0.1);
    border: none;
    border-radius: 0.5rem;
    cursor: pointer;
  }
  .btn-control:hover { background: rgba(255, 255, 255, 0.2); }

  .fullscreen-hint {
    position: absolute;
    top: 50%;
    left: 50%;
    transform: translate(-50%, -50%);
    padding: 0.75rem 1.5rem;
    background: rgba(0, 0, 0, 0.6);
    color: rgba(255, 255, 255, 0.8);
    border-radius: 0.75rem;
    font-size: 0.9rem;
    pointer-events: none;
    z-index: 8;
    animation: fadeHint 4s ease-out forwards;
  }

  @keyframes fadeHint {
    0% { opacity: 1; }
    70% { opacity: 1; }
    100% { opacity: 0; }
  }
</style>
