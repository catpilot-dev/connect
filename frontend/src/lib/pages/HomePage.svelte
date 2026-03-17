<script>
  import { onMount, onDestroy } from 'svelte'
  import { dongleId, isMetric } from '../stores.js'
  import { fetchSoftware, fetchStorage, fetchDeviceStats } from '../api.js'

  // ── REST data ──────────────────────────────────────────────────────────────
  let software = $state(null)
  let storage   = $state(null)
  let stats     = $state(null)
  let loading   = $state(true)

  // ── WebSocket live status ──────────────────────────────────────────────────
  let deviceStatus = $state(null)
  let gpsStatus    = $state(null)
  let ws = null
  let wsRetryTimer = null
  let refreshTimer = null

  async function load() {
    try {
      const id = $dongleId
      const [sw, st, dr] = await Promise.allSettled([
        fetchSoftware(),
        fetchStorage(),
        id ? fetchDeviceStats(id) : Promise.reject('no dongle'),
      ])
      if (sw.status === 'fulfilled') software = sw.value
      if (st.status === 'fulfilled') storage = st.value
      if (dr.status === 'fulfilled') stats = dr.value
    } catch {}
    loading = false
  }

  function connectWs() {
    try {
      const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
      ws = new WebSocket(`${proto}//${location.host}/ws/home`)
      ws.onmessage = (e) => {
        try {
          const msg = JSON.parse(e.data)
          if (msg.type === 'device') deviceStatus = msg
          else if (msg.type === 'gps') gpsStatus = msg
        } catch {}
      }
      ws.onclose = () => {
        ws = null
        wsRetryTimer = setTimeout(connectWs, 2000)
      }
    } catch {}
  }

  onMount(() => {
    load()
    refreshTimer = setInterval(load, 30_000)
    connectWs()
  })

  onDestroy(() => {
    clearInterval(refreshTimer)
    clearTimeout(wsRetryTimer)
    if (ws) { ws.onclose = null; ws.close() }
  })

  // ── Derived: REST ─────────────────────────────────────────────────────────
  const version = $derived(
    software?.UpdaterCurrentDescription
      ? software.UpdaterCurrentDescription
      : software?.GitBranch
        ? software.GitBranch
        : 'dev'
  )
  const updateAvailable   = $derived(software?.UpdateAvailable === true)
  const storageUsedGb     = $derived(storage ? (storage.used / 1e9).toFixed(1) : null)
  const storageTotalGb    = $derived(storage ? (storage.total / 1e9).toFixed(0) : null)
  const storagePct        = $derived(storage ? Math.round((storage.used / storage.total) * 100) : 0)
  const weekRoutes        = $derived(stats?.week?.routes ?? 0)
  const weekDistance      = $derived(stats?.week?.distance ?? 0)
  const weekDistanceDisplay = $derived(
    $isMetric
      ? `${weekDistance.toFixed(0)} km`
      : `${(weekDistance * 0.621371).toFixed(0)} mi`
  )
  const allRoutes = $derived(stats?.all?.routes ?? 0)

  // ── Derived: live ─────────────────────────────────────────────────────────
  const maxTemp       = $derived(deviceStatus?.maxTempC ?? null)
  const cpuUsage      = $derived(deviceStatus?.cpuUsagePct ?? null)
  const gpuUsage      = $derived(deviceStatus?.gpuUsagePct ?? null)
  const memUsage      = $derived(deviceStatus?.memoryUsagePct ?? null)
  const networkType   = $derived(deviceStatus?.networkType ?? 'none')
  const netStrength   = $derived(deviceStatus?.networkStrength ?? 'unknown')
  const hasFix        = $derived(gpsStatus?.hasFix ?? false)
  const gpsAccuracy   = $derived(gpsStatus?.accuracy ?? null)
  const isLive        = $derived(deviceStatus !== null)

  // ── Canvas helpers ────────────────────────────────────────────────────────
  function sizeCanvas(canvas) {
    const dpr = window.devicePixelRatio || 1
    const w = canvas.offsetWidth
    const h = canvas.offsetHeight
    if (canvas.width !== w * dpr || canvas.height !== h * dpr) {
      canvas.width  = w * dpr
      canvas.height = h * dpr
    }
    const ctx = canvas.getContext('2d')
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
    return { ctx, w, h }
  }

  // ── Temperature arc gauge (270°) ──────────────────────────────────────────
  let tempCanvas = $state(null)

  $effect(() => {
    if (!tempCanvas) return
    const temp = maxTemp
    const { ctx, w, h } = sizeCanvas(tempCanvas)

    const cx = w / 2
    const cy = h * 0.60
    const r  = Math.min(w * 0.44, h * 0.66)
    const lw = r * 0.115
    const startAngle = Math.PI * 0.75
    const sweep = Math.PI * 1.5

    ctx.clearRect(0, 0, w, h)

    // Zone bands behind track
    const zones = [
      { from: 0,    to: 0.42, color: 'rgba(0,229,255,0.06)' },
      { from: 0.42, to: 0.72, color: 'rgba(255,179,0,0.06)' },
      { from: 0.72, to: 1.0,  color: 'rgba(255,61,61,0.06)' },
    ]
    for (const z of zones) {
      ctx.beginPath()
      ctx.arc(cx, cy, r, startAngle + sweep * z.from, startAngle + sweep * z.to)
      ctx.strokeStyle = z.color
      ctx.lineWidth = lw
      ctx.lineCap = 'butt'
      ctx.stroke()
    }

    // Track base
    ctx.beginPath()
    ctx.arc(cx, cy, r, startAngle, startAngle + sweep)
    ctx.strokeStyle = 'rgba(255,255,255,0.05)'
    ctx.lineWidth = lw
    ctx.lineCap = 'butt'
    ctx.stroke()

    // Tick marks
    for (let i = 0; i <= 10; i++) {
      const frac = i / 10
      const a = startAngle + sweep * frac
      const isMajor = (i % 5 === 0)
      const inner = r - lw * 1.15
      const outer = r + lw * (isMajor ? 0.75 : 0.35)
      ctx.beginPath()
      ctx.moveTo(cx + Math.cos(a) * inner, cy + Math.sin(a) * inner)
      ctx.lineTo(cx + Math.cos(a) * outer, cy + Math.sin(a) * outer)
      ctx.strokeStyle = isMajor ? 'rgba(255,255,255,0.22)' : 'rgba(255,255,255,0.08)'
      ctx.lineWidth = isMajor ? 1.5 : 0.75
      ctx.stroke()
    }

    if (temp !== null) {
      const t = Math.max(0, Math.min(100, temp))
      const frac = t / 100
      const fillEnd = startAngle + sweep * frac
      const col = t < 50 ? '#00e5ff' : t < 75 ? '#ffb300' : '#ff3d3d'

      // Glow pass
      ctx.save()
      ctx.shadowColor = col
      ctx.shadowBlur = 16
      ctx.beginPath()
      ctx.arc(cx, cy, r, startAngle, fillEnd)
      ctx.strokeStyle = col + '55'
      ctx.lineWidth = lw * 2
      ctx.lineCap = 'round'
      ctx.stroke()
      ctx.restore()

      // Fill arc
      ctx.beginPath()
      ctx.arc(cx, cy, r, startAngle, fillEnd)
      ctx.strokeStyle = col
      ctx.lineWidth = lw
      ctx.lineCap = 'round'
      ctx.stroke()

      // Tip glow
      const tx = cx + Math.cos(fillEnd) * r
      const ty = cy + Math.sin(fillEnd) * r
      const tg = ctx.createRadialGradient(tx, ty, 0, tx, ty, lw * 1.8)
      tg.addColorStop(0,   col + 'ff')
      tg.addColorStop(0.5, col + '70')
      tg.addColorStop(1,   col + '00')
      ctx.beginPath()
      ctx.arc(tx, ty, lw * 1.8, 0, Math.PI * 2)
      ctx.fillStyle = tg
      ctx.fill()

      // Value
      ctx.textAlign = 'center'
      ctx.fillStyle = '#edf6fa'
      ctx.font = `700 ${r * 0.75}px 'Share Tech Mono', monospace`
      ctx.textBaseline = 'alphabetic'
      ctx.fillText(`${Math.round(t)}`, cx, cy + r * 0.06)

      ctx.fillStyle = col
      ctx.font = `600 ${r * 0.26}px 'Share Tech Mono', monospace`
      ctx.fillText('°C', cx + r * 0.34, cy - r * 0.28)

      ctx.fillStyle = 'rgba(255,255,255,0.22)'
      ctx.font = `600 ${r * 0.16}px 'Rajdhani', sans-serif`
      ctx.textBaseline = 'top'
      ctx.fillText('MAX TEMP', cx, cy + r * 0.16)

      const zone = t < 50 ? 'COOL' : t < 75 ? 'WARM' : 'HOT'
      ctx.fillStyle = col + 'bb'
      ctx.font = `700 ${r * 0.14}px 'Rajdhani', sans-serif`
      ctx.fillText(zone, cx, cy + r * 0.32)
    } else {
      ctx.textAlign = 'center'
      ctx.textBaseline = 'middle'
      ctx.fillStyle = 'rgba(255,255,255,0.1)'
      ctx.font = `700 ${r * 0.48}px 'Share Tech Mono', monospace`
      ctx.fillText('---', cx, cy)
      ctx.fillStyle = 'rgba(255,255,255,0.14)'
      ctx.font = `600 ${r * 0.14}px 'Rajdhani', sans-serif`
      ctx.textBaseline = 'top'
      ctx.fillText('AWAITING SIGNAL', cx, cy + r * 0.22)
    }
  })

  // ── Mini ring gauges ──────────────────────────────────────────────────────
  let cpuCanvas = $state(null)
  let gpuCanvas = $state(null)
  let memCanvas = $state(null)

  function drawRing(canvas, pct, color, label) {
    if (!canvas) return
    const { ctx, w, h } = sizeCanvas(canvas)
    const cx = w / 2
    const cy = h * 0.47
    const r  = Math.min(w, h) * 0.33
    const lw = r * 0.24
    const start = -Math.PI / 2

    ctx.clearRect(0, 0, w, h)

    // Outer decorative ring
    ctx.beginPath()
    ctx.arc(cx, cy, r + lw * 0.95, 0, Math.PI * 2)
    ctx.strokeStyle = 'rgba(255,255,255,0.04)'
    ctx.lineWidth = 0.75
    ctx.stroke()

    // BG track
    ctx.beginPath()
    ctx.arc(cx, cy, r, 0, Math.PI * 2)
    ctx.strokeStyle = 'rgba(255,255,255,0.07)'
    ctx.lineWidth = lw
    ctx.lineCap = 'butt'
    ctx.stroke()

    if (pct !== null && pct !== undefined) {
      const frac = Math.max(0, Math.min(100, pct)) / 100
      if (frac > 0) {
        ctx.save()
        ctx.shadowColor = color
        ctx.shadowBlur = 10
        ctx.beginPath()
        ctx.arc(cx, cy, r, start, start + Math.PI * 2 * frac)
        ctx.strokeStyle = color + '45'
        ctx.lineWidth = lw * 1.6
        ctx.lineCap = 'round'
        ctx.stroke()
        ctx.restore()

        ctx.beginPath()
        ctx.arc(cx, cy, r, start, start + Math.PI * 2 * frac)
        ctx.strokeStyle = color
        ctx.lineWidth = lw
        ctx.lineCap = 'round'
        ctx.stroke()
      }

      ctx.textAlign = 'center'
      ctx.textBaseline = 'middle'
      ctx.fillStyle = '#edf6fa'
      ctx.font = `700 ${r * 0.64}px 'Share Tech Mono', monospace`
      ctx.fillText(`${Math.round(pct)}`, cx, cy)
    } else {
      ctx.textAlign = 'center'
      ctx.textBaseline = 'middle'
      ctx.fillStyle = 'rgba(255,255,255,0.14)'
      ctx.font = `700 ${r * 0.5}px 'Share Tech Mono', monospace`
      ctx.fillText('--', cx, cy)
    }

    ctx.fillStyle = 'rgba(255,255,255,0.28)'
    ctx.font = `700 ${r * 0.28}px 'Rajdhani', sans-serif`
    ctx.textBaseline = 'middle'
    ctx.fillText(label, cx, cy + r * 0.8 + lw * 0.55)
  }

  $effect(() => { drawRing(cpuCanvas, cpuUsage, '#00e5ff', 'CPU') })
  $effect(() => { drawRing(gpuCanvas, gpuUsage, '#bf6dff', 'GPU') })
  $effect(() => { drawRing(memCanvas, memUsage, '#00ff87', 'MEM') })

  // ── Network signal bars ───────────────────────────────────────────────────
  let netCanvas = $state(null)
  const STRENGTH_BARS = { unknown: 0, poor: 1, moderate: 2, good: 3, great: 4 }

  $effect(() => {
    if (!netCanvas) return
    const type = networkType
    const strength = netStrength
    const { ctx, w, h } = sizeCanvas(netCanvas)
    ctx.clearRect(0, 0, w, h)

    if (type === 'none' || !type) {
      ctx.strokeStyle = 'rgba(255,255,255,0.15)'
      ctx.lineWidth = 1.5
      ctx.lineCap = 'round'
      const m = w * 0.2
      ctx.beginPath(); ctx.moveTo(m, m); ctx.lineTo(w - m, h * 0.62); ctx.stroke()
      ctx.beginPath(); ctx.moveTo(w - m, m); ctx.lineTo(m, h * 0.62); ctx.stroke()
      return
    }

    const active = STRENGTH_BARS[strength] ?? 0
    const n = 4
    const barW = w * 0.14
    const gap = (w - n * barW) / (n + 1)
    const maxH = h * 0.72
    const col = active >= 3 ? '#00ff87' : active >= 2 ? '#ffb300' : '#ff3d3d'

    for (let i = 0; i < n; i++) {
      const barH = maxH * (0.22 + 0.26 * i)
      const x = gap + i * (barW + gap)
      const y = maxH - barH
      const on = i < active
      ctx.save()
      if (on) { ctx.shadowColor = col; ctx.shadowBlur = 5 }
      if (ctx.roundRect) {
        ctx.beginPath()
        ctx.roundRect(x, y, barW, barH, 2)
        ctx.fillStyle = on ? col : 'rgba(255,255,255,0.08)'
        ctx.fill()
      } else {
        ctx.fillStyle = on ? col : 'rgba(255,255,255,0.08)'
        ctx.fillRect(x, y, barW, barH)
      }
      ctx.restore()
    }

    const typeLabel = { wifi: 'WiFi', cell4G: '4G', cell5G: '5G', cell3G: '3G', cell2G: '2G', ethernet: 'ETH' }[type] ?? ''
    if (typeLabel) {
      ctx.fillStyle = 'rgba(255,255,255,0.28)'
      ctx.font = `600 ${h * 0.17}px 'Rajdhani', sans-serif`
      ctx.textAlign = 'center'
      ctx.textBaseline = 'bottom'
      ctx.fillText(typeLabel, w / 2, h)
    }
  })

  // ── GPS accuracy ring ─────────────────────────────────────────────────────
  let gpsCanvas = $state(null)

  $effect(() => {
    if (!gpsCanvas) return
    const fix = hasFix
    const acc = gpsAccuracy
    const { ctx, w, h } = sizeCanvas(gpsCanvas)
    ctx.clearRect(0, 0, w, h)

    const cx = w / 2, cy = h * 0.47
    const r  = Math.min(w, h) * 0.32
    const lw = r * 0.22

    ctx.beginPath()
    ctx.arc(cx, cy, r, 0, Math.PI * 2)
    ctx.strokeStyle = 'rgba(255,255,255,0.07)'
    ctx.lineWidth = lw
    ctx.stroke()

    if (fix && acc !== null) {
      const frac = acc <= 3 ? 1.0 : acc <= 10 ? 0.85 : acc <= 25 ? 0.6 : acc <= 50 ? 0.35 : 0.12
      const col = acc <= 10 ? '#00ff87' : acc <= 25 ? '#ffb300' : '#ff3d3d'
      ctx.save()
      ctx.shadowColor = col
      ctx.shadowBlur = 8
      ctx.beginPath()
      ctx.arc(cx, cy, r, -Math.PI / 2, -Math.PI / 2 + Math.PI * 2 * frac)
      ctx.strokeStyle = col
      ctx.lineWidth = lw
      ctx.lineCap = 'round'
      ctx.stroke()
      ctx.restore()

      ctx.textAlign = 'center'
      ctx.textBaseline = 'middle'
      ctx.fillStyle = '#edf6fa'
      ctx.font = `700 ${r * 0.62}px 'Share Tech Mono', monospace`
      ctx.fillText(acc < 100 ? `${Math.round(acc)}` : '99+', cx, cy)
    } else {
      ctx.textAlign = 'center'
      ctx.textBaseline = 'middle'
      ctx.fillStyle = 'rgba(255,255,255,0.13)'
      ctx.font = `700 ${r * 0.48}px 'Share Tech Mono', monospace`
      ctx.fillText('--', cx, cy)
    }

    ctx.fillStyle = fix ? '#00ff87bb' : 'rgba(255,255,255,0.22)'
    ctx.font = `700 ${r * 0.27}px 'Rajdhani', sans-serif`
    ctx.textBaseline = 'middle'
    ctx.fillText(fix ? 'GPS m' : 'NO FIX', cx, cy + r * 0.8 + lw * 0.55)
  })
</script>

<svelte:head>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin="">
  <link href="https://fonts.googleapis.com/css2?family=Rajdhani:wght@500;600;700&family=Share+Tech+Mono&display=swap" rel="stylesheet">
</svelte:head>

<div class="home-page">

  <!-- Ambient depth layer -->
  <div class="ambient" aria-hidden="true"></div>

  <!-- ── Header ────────────────────────────────────────────────────────────── -->
  <header class="home-header">
    <div class="brand">
      <span class="brand-name">CATEYE</span>
      <span class="brand-ver">{version}</span>
    </div>
    <div class="header-actions">
      {#if updateAvailable}
        <span class="update-badge">UPDATE</span>
      {/if}
      <a href="/settings" class="icon-btn" aria-label="Settings">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
          <circle cx="12" cy="12" r="3"/>
          <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>
        </svg>
      </a>
    </div>
  </header>

  <!-- ── Main ──────────────────────────────────────────────────────────────── -->
  <main class="home-content">

    {#if loading}
      <div class="loading-state">
        <div class="spinner"></div>
        <span class="loading-label">INITIALIZING</span>
      </div>
    {:else}

      <!-- Status strip -->
      <div class="status-strip">
        <div class="status-ready">
          <span class="ready-dot"></span>
          <span class="ready-label">READY</span>
        </div>
        <div class="status-indicators">
          <canvas bind:this={netCanvas} class="indicator-canvas net-canvas" aria-label="Network"></canvas>
          <canvas bind:this={gpsCanvas} class="indicator-canvas gps-canvas" aria-label="GPS"></canvas>
        </div>
      </div>

      <!-- Instrument cluster -->
      {#if isLive}
        <div class="instrument-panel">
          <div class="panel-eyebrow">
            <span class="eyebrow-line"></span>
            <span class="eyebrow-text">SYSTEM MONITOR</span>
            <span class="eyebrow-line"></span>
          </div>
          <div class="instruments">
            <div class="temp-wrap">
              <canvas bind:this={tempCanvas} class="temp-canvas" aria-label="Temperature"></canvas>
            </div>
            <div class="rings-col">
              <canvas bind:this={cpuCanvas} class="ring-canvas" aria-label="CPU"></canvas>
              <canvas bind:this={gpuCanvas} class="ring-canvas" aria-label="GPU"></canvas>
              <canvas bind:this={memCanvas} class="ring-canvas" aria-label="MEM"></canvas>
            </div>
          </div>
        </div>
      {/if}

      <!-- Stats -->
      {#if stats}
        <div class="stats-panel">
          <div class="stats-eyebrow">THIS WEEK</div>
          <div class="stats-row">
            <div class="stat-block">
              <span class="stat-val">{weekRoutes}</span>
              <span class="stat-lbl">DRIVES</span>
            </div>
            <div class="stat-sep"></div>
            <div class="stat-block">
              <span class="stat-val">{weekDistanceDisplay}</span>
              <span class="stat-lbl">DISTANCE</span>
            </div>
            <div class="stat-sep"></div>
            <div class="stat-block">
              <span class="stat-val">{allRoutes}</span>
              <span class="stat-lbl">ALL TIME</span>
            </div>
          </div>
        </div>
      {/if}

      <!-- Storage -->
      {#if storage}
        <div class="storage-panel">
          <div class="storage-head">
            <span class="storage-lbl">STORAGE</span>
            <span class="storage-num">{storageUsedGb} <span class="storage-unit">/ {storageTotalGb} GB</span></span>
            <span class="storage-pct" class:warn={storagePct > 80} class:crit={storagePct > 95}>{storagePct}%</span>
          </div>
          <div class="storage-track">
            <div
              class="storage-fill"
              class:warn={storagePct > 80}
              class:crit={storagePct > 95}
              style="width: {storagePct}%"
            ></div>
            <div class="storage-mark" style="left: 25%"></div>
            <div class="storage-mark" style="left: 50%"></div>
            <div class="storage-mark" style="left: 75%"></div>
          </div>
        </div>
      {/if}

    {/if}
  </main>

  <!-- ── Nav ───────────────────────────────────────────────────────────────── -->
  <nav class="home-nav">
    <a href="/driving" class="nav-btn nav-primary">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
        <circle cx="12" cy="12" r="10"/>
        <polygon points="10,8 16,12 10,16" fill="currentColor" stroke="none"/>
      </svg>
      DRIVING
    </a>
    <a href="/" class="nav-btn">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round">
        <polyline points="22,12 18,12 15,21 9,3 6,12 2,12"/>
      </svg>
      ROUTES
    </a>
    <a href="/settings" class="nav-btn">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
        <circle cx="12" cy="12" r="3"/>
        <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>
      </svg>
      SETTINGS
    </a>
  </nav>

</div>

<style>
  /* ── Tokens ──────────────────────────────────────────────────────────────── */
  :root {
    --bg:        #06090f;
    --surface:   #0c1422;
    --border:    rgba(0, 229, 255, 0.09);
    --cyan:      #00e5ff;
    --green:     #00ff87;
    --amber:     #ffb300;
    --red:       #ff3d3d;
    --purple:    #bf6dff;
    --txt:       #edf6fa;
    --muted:     rgba(255,255,255,0.38);
    --dim:       rgba(255,255,255,0.14);
    --font-ui:   'Rajdhani', system-ui, sans-serif;
    --font-data: 'Share Tech Mono', 'Consolas', monospace;
  }

  /* ── Page shell ──────────────────────────────────────────────────────────── */
  .home-page {
    display: flex;
    flex-direction: column;
    min-height: 100dvh;
    background: var(--bg);
    color: var(--txt);
    font-family: var(--font-ui);
    position: relative;
    overflow: hidden;
  }

  /* Ambient radial glow — gives depth without distraction */
  .ambient {
    position: fixed;
    inset: 0;
    pointer-events: none;
    z-index: 0;
    background:
      radial-gradient(ellipse 70% 35% at 50% -5%, rgba(0,229,255,0.05) 0%, transparent 70%),
      radial-gradient(ellipse 45% 55% at 92% 85%, rgba(191,109,255,0.04) 0%, transparent 60%);
  }

  /* Subtle scanlines — automotive instrument feel */
  .ambient::after {
    content: '';
    position: absolute;
    inset: 0;
    background-image: repeating-linear-gradient(
      0deg,
      transparent 0px,
      transparent 3px,
      rgba(0,0,0,0.12) 3px,
      rgba(0,0,0,0.12) 4px
    );
  }

  /* ── Header ──────────────────────────────────────────────────────────────── */
  .home-header {
    position: relative;
    z-index: 1;
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0.9rem 1.125rem 0.7rem;
    border-bottom: 1px solid rgba(0,229,255,0.07);
  }

  .brand {
    display: flex;
    align-items: baseline;
    gap: 0.55rem;
  }

  .brand-name {
    font-family: var(--font-ui);
    font-size: 1.1rem;
    font-weight: 700;
    letter-spacing: 0.12em;
    color: var(--cyan);
    text-shadow: 0 0 20px rgba(0,229,255,0.35);
  }

  .brand-ver {
    font-family: var(--font-data);
    font-size: 0.65rem;
    color: var(--muted);
    letter-spacing: 0.04em;
  }

  .header-actions {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }

  .update-badge {
    font-size: 0.6rem;
    font-weight: 700;
    letter-spacing: 0.08em;
    color: #000;
    background: var(--cyan);
    padding: 0.15rem 0.45rem;
    border-radius: 2px;
    animation: badge-pulse 2s ease-in-out infinite;
  }
  @keyframes badge-pulse {
    0%, 100% { opacity: 1; }
    50%       { opacity: 0.7; }
  }

  .icon-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 2.1rem;
    height: 2.1rem;
    border-radius: 6px;
    color: var(--muted);
    background: rgba(255,255,255,0.04);
    border: 1px solid rgba(255,255,255,0.06);
    text-decoration: none;
    transition: color 0.15s, background 0.15s;
  }
  .icon-btn svg { width: 1rem; height: 1rem; }
  .icon-btn:active { background: rgba(0,229,255,0.1); color: var(--cyan); }

  /* ── Main ────────────────────────────────────────────────────────────────── */
  .home-content {
    position: relative;
    z-index: 1;
    flex: 1;
    display: flex;
    flex-direction: column;
    gap: 0.6rem;
    padding: 0.75rem 0.875rem 0.5rem;
    overflow-y: auto;
  }

  /* ── Loading ─────────────────────────────────────────────────────────────── */
  .loading-state {
    flex: 1;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 1rem;
  }

  .spinner {
    width: 2rem;
    height: 2rem;
    border: 1.5px solid rgba(0,229,255,0.12);
    border-top-color: var(--cyan);
    border-radius: 50%;
    animation: spin 0.9s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  .loading-label {
    font-size: 0.65rem;
    letter-spacing: 0.2em;
    color: var(--dim);
    font-weight: 600;
  }

  /* ── Status strip ────────────────────────────────────────────────────────── */
  .status-strip {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    padding: 0.6rem 0.875rem;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
  }

  .status-ready {
    display: flex;
    align-items: center;
    gap: 0.45rem;
    flex: 1;
  }

  .ready-dot {
    display: block;
    width: 0.55rem;
    height: 0.55rem;
    border-radius: 50%;
    background: var(--green);
    box-shadow: 0 0 6px var(--green);
    animation: ready-pulse 2.8s ease-in-out infinite;
    flex-shrink: 0;
  }
  @keyframes ready-pulse {
    0%, 100% { box-shadow: 0 0 4px var(--green); opacity: 1; }
    50%       { box-shadow: 0 0 12px var(--green); opacity: 0.85; }
  }

  .ready-label {
    font-size: 0.82rem;
    font-weight: 700;
    letter-spacing: 0.1em;
    color: var(--green);
  }

  .status-indicators {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    flex-shrink: 0;
  }

  .indicator-canvas { display: block; }
  .net-canvas { width: 42px; height: 46px; }
  .gps-canvas { width: 42px; height: 46px; }

  /* ── Instrument panel ────────────────────────────────────────────────────── */
  .instrument-panel {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 0.7rem 0.75rem 0.6rem;
  }

  .panel-eyebrow {
    display: flex;
    align-items: center;
    gap: 0.6rem;
    margin-bottom: 0.55rem;
  }

  .eyebrow-line {
    flex: 1;
    height: 1px;
    background: rgba(0,229,255,0.12);
  }

  .eyebrow-text {
    font-size: 0.6rem;
    font-weight: 700;
    letter-spacing: 0.18em;
    color: rgba(0,229,255,0.45);
    white-space: nowrap;
  }

  .instruments {
    display: flex;
    gap: 0.5rem;
    align-items: center;
  }

  .temp-wrap {
    flex: 1;
    min-width: 0;
  }

  .temp-canvas {
    display: block;
    width: 100%;
    aspect-ratio: 1 / 0.88;
  }

  .rings-col {
    display: flex;
    flex-direction: column;
    gap: 0.3rem;
    flex-shrink: 0;
  }

  .ring-canvas {
    display: block;
    width: 76px;
    height: 82px;
  }

  /* ── Stats panel ─────────────────────────────────────────────────────────── */
  .stats-panel {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 0.65rem 0.875rem;
  }

  .stats-eyebrow {
    font-size: 0.58rem;
    font-weight: 700;
    letter-spacing: 0.16em;
    color: var(--muted);
    margin-bottom: 0.45rem;
  }

  .stats-row {
    display: flex;
    align-items: center;
    gap: 0;
  }

  .stat-block {
    flex: 1;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 0.15rem;
  }

  .stat-val {
    font-family: var(--font-data);
    font-size: 1.35rem;
    color: var(--txt);
    line-height: 1;
    letter-spacing: -0.02em;
  }

  .stat-lbl {
    font-size: 0.56rem;
    font-weight: 600;
    letter-spacing: 0.12em;
    color: var(--dim);
  }

  .stat-sep {
    width: 1px;
    height: 2rem;
    background: rgba(0,229,255,0.1);
    flex-shrink: 0;
  }

  /* ── Storage panel ───────────────────────────────────────────────────────── */
  .storage-panel {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 0.65rem 0.875rem;
  }

  .storage-head {
    display: flex;
    align-items: baseline;
    gap: 0.4rem;
    margin-bottom: 0.55rem;
  }

  .storage-lbl {
    font-size: 0.6rem;
    font-weight: 700;
    letter-spacing: 0.16em;
    color: var(--muted);
    flex: 1;
  }

  .storage-num {
    font-family: var(--font-data);
    font-size: 0.78rem;
    color: var(--txt);
  }
  .storage-unit { color: var(--muted); }

  .storage-pct {
    font-family: var(--font-data);
    font-size: 0.68rem;
    color: var(--cyan);
    min-width: 2.5rem;
    text-align: right;
  }
  .storage-pct.warn { color: var(--amber); }
  .storage-pct.crit { color: var(--red); }

  .storage-track {
    position: relative;
    height: 5px;
    background: rgba(255,255,255,0.06);
    border-radius: 2px;
    overflow: visible;
  }

  .storage-fill {
    height: 100%;
    background: var(--cyan);
    border-radius: 2px;
    box-shadow: 0 0 6px rgba(0,229,255,0.4);
    transition: width 0.6s ease;
  }
  .storage-fill.warn { background: var(--amber); box-shadow: 0 0 6px rgba(255,179,0,0.4); }
  .storage-fill.crit { background: var(--red);   box-shadow: 0 0 6px rgba(255,61,61,0.4); }

  .storage-mark {
    position: absolute;
    top: -2px;
    width: 1px;
    height: 9px;
    background: rgba(0,0,0,0.6);
    transform: translateX(-50%);
    pointer-events: none;
  }

  /* ── Navigation ──────────────────────────────────────────────────────────── */
  .home-nav {
    position: relative;
    z-index: 1;
    display: grid;
    grid-template-columns: 1fr 1fr 1fr;
    gap: 0.45rem;
    padding: 0.6rem 0.875rem calc(0.6rem + env(safe-area-inset-bottom, 0px));
    border-top: 1px solid rgba(0,229,255,0.07);
    background: rgba(6,9,15,0.95);
  }

  .nav-btn {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 0.25rem;
    padding: 0.55rem 0.4rem;
    border-radius: 8px;
    font-family: var(--font-ui);
    font-size: 0.62rem;
    font-weight: 700;
    letter-spacing: 0.1em;
    color: var(--muted);
    background: rgba(255,255,255,0.03);
    border: 1px solid rgba(255,255,255,0.05);
    text-decoration: none;
    transition: background 0.15s, color 0.15s;
  }
  .nav-btn svg { width: 1.15rem; height: 1.15rem; }
  .nav-btn:active { background: rgba(255,255,255,0.09); }

  .nav-primary {
    color: var(--cyan);
    background: rgba(0,229,255,0.06);
    border-color: rgba(0,229,255,0.18);
    text-shadow: 0 0 12px rgba(0,229,255,0.4);
  }
  .nav-primary svg { filter: drop-shadow(0 0 4px rgba(0,229,255,0.5)); }
  .nav-primary:active { background: rgba(0,229,255,0.14); }
</style>
