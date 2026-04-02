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
    const cy = h * 0.58
    const r  = Math.min(w * 0.44, h * 0.64)
    const lw = r * 0.10
    const startAngle = Math.PI * 0.75
    const sweep = Math.PI * 1.5

    ctx.clearRect(0, 0, w, h)

    // Zone bands behind track
    const zones = [
      { from: 0,    to: 0.42, color: 'rgba(0,255,148,0.07)' },
      { from: 0.42, to: 0.72, color: 'rgba(247,183,49,0.07)' },
      { from: 0.72, to: 1.0,  color: 'rgba(255,72,66,0.07)' },
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
    ctx.strokeStyle = 'rgba(255,255,255,0.06)'
    ctx.lineWidth = lw
    ctx.lineCap = 'butt'
    ctx.stroke()

    // Tick marks
    for (let i = 0; i <= 10; i++) {
      const frac = i / 10
      const a = startAngle + sweep * frac
      const isMajor = (i % 5 === 0)
      const inner = r - lw * 1.1
      const outer = r + lw * (isMajor ? 0.7 : 0.3)
      ctx.beginPath()
      ctx.moveTo(cx + Math.cos(a) * inner, cy + Math.sin(a) * inner)
      ctx.lineTo(cx + Math.cos(a) * outer, cy + Math.sin(a) * outer)
      ctx.strokeStyle = isMajor ? 'rgba(255,255,255,0.20)' : 'rgba(255,255,255,0.07)'
      ctx.lineWidth = isMajor ? 1.5 : 0.75
      ctx.stroke()
    }

    if (temp !== null) {
      const t = Math.max(0, Math.min(100, temp))
      const frac = t / 100
      const fillEnd = startAngle + sweep * frac
      const col = t < 50 ? '#00ff94' : t < 75 ? '#f7b731' : '#ff4842'

      // Glow pass
      ctx.save()
      ctx.shadowColor = col
      ctx.shadowBlur = 20
      ctx.beginPath()
      ctx.arc(cx, cy, r, startAngle, fillEnd)
      ctx.strokeStyle = col + '40'
      ctx.lineWidth = lw * 2.4
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

      // Tip pulse dot
      const tx = cx + Math.cos(fillEnd) * r
      const ty = cy + Math.sin(fillEnd) * r
      const tg = ctx.createRadialGradient(tx, ty, 0, tx, ty, lw * 2.2)
      tg.addColorStop(0,   col + 'ff')
      tg.addColorStop(0.4, col + '80')
      tg.addColorStop(1,   col + '00')
      ctx.beginPath()
      ctx.arc(tx, ty, lw * 2.2, 0, Math.PI * 2)
      ctx.fillStyle = tg
      ctx.fill()

      // Value
      ctx.textAlign = 'center'
      ctx.fillStyle = '#f0f4f8'
      ctx.font = `700 ${r * 0.82}px 'Oxanium', monospace`
      ctx.textBaseline = 'alphabetic'
      ctx.fillText(`${Math.round(t)}`, cx, cy + r * 0.08)

      ctx.fillStyle = col
      ctx.font = `600 ${r * 0.27}px 'Oxanium', monospace`
      ctx.fillText('°C', cx + r * 0.38, cy - r * 0.30)

      ctx.fillStyle = 'rgba(255,255,255,0.20)'
      ctx.font = `600 ${r * 0.155}px 'Rajdhani', sans-serif`
      ctx.textBaseline = 'top'
      ctx.fillText('MAX TEMP', cx, cy + r * 0.18)

      const zone = t < 50 ? 'NOMINAL' : t < 75 ? 'ELEVATED' : 'CRITICAL'
      ctx.fillStyle = col + 'cc'
      ctx.font = `700 ${r * 0.135}px 'Rajdhani', sans-serif`
      ctx.fillText(zone, cx, cy + r * 0.36)
    } else {
      ctx.textAlign = 'center'
      ctx.textBaseline = 'middle'
      ctx.fillStyle = 'rgba(255,255,255,0.08)'
      ctx.font = `700 ${r * 0.5}px 'Oxanium', monospace`
      ctx.fillText('---', cx, cy)
      ctx.fillStyle = 'rgba(255,255,255,0.12)'
      ctx.font = `600 ${r * 0.14}px 'Rajdhani', sans-serif`
      ctx.textBaseline = 'top'
      ctx.fillText('AWAITING DATA', cx, cy + r * 0.22)
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
    const cy = h * 0.44
    const r  = Math.min(w, h) * 0.31
    const lw = r * 0.26
    const start = -Math.PI / 2

    ctx.clearRect(0, 0, w, h)

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
        ctx.shadowBlur = 12
        ctx.beginPath()
        ctx.arc(cx, cy, r, start, start + Math.PI * 2 * frac)
        ctx.strokeStyle = color + '38'
        ctx.lineWidth = lw * 1.8
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
      ctx.fillStyle = '#f0f4f8'
      ctx.font = `700 ${r * 0.68}px 'Oxanium', monospace`
      ctx.fillText(`${Math.round(pct)}`, cx, cy)
    } else {
      ctx.textAlign = 'center'
      ctx.textBaseline = 'middle'
      ctx.fillStyle = 'rgba(255,255,255,0.12)'
      ctx.font = `700 ${r * 0.52}px 'Oxanium', monospace`
      ctx.fillText('--', cx, cy)
    }

    ctx.fillStyle = 'rgba(255,255,255,0.26)'
    ctx.font = `700 ${r * 0.28}px 'Rajdhani', sans-serif`
    ctx.textBaseline = 'middle'
    ctx.fillText(label, cx, cy + r + lw * 0.72)
  }

  $effect(() => { drawRing(cpuCanvas, cpuUsage, '#f7b731', 'CPU') })
  $effect(() => { drawRing(gpuCanvas, gpuUsage, '#b06bff', 'GPU') })
  $effect(() => { drawRing(memCanvas, memUsage, '#00ff94', 'MEM') })

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
      const m = w * 0.22
      ctx.beginPath(); ctx.moveTo(m, m); ctx.lineTo(w - m, h * 0.6); ctx.stroke()
      ctx.beginPath(); ctx.moveTo(w - m, m); ctx.lineTo(m, h * 0.6); ctx.stroke()
      return
    }

    const active = STRENGTH_BARS[strength] ?? 0
    const n = 4
    const barW = w * 0.14
    const gap = (w - n * barW) / (n + 1)
    const maxH = h * 0.70
    const col = active >= 3 ? '#00ff94' : active >= 2 ? '#f7b731' : '#ff4842'

    for (let i = 0; i < n; i++) {
      const barH = maxH * (0.2 + 0.27 * i)
      const x = gap + i * (barW + gap)
      const y = maxH - barH
      const on = i < active
      ctx.save()
      if (on) { ctx.shadowColor = col; ctx.shadowBlur = 6 }
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
      ctx.fillStyle = 'rgba(255,255,255,0.26)'
      ctx.font = `600 ${h * 0.16}px 'Rajdhani', sans-serif`
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

    const cx = w / 2, cy = h * 0.44
    const r  = Math.min(w, h) * 0.30
    const lw = r * 0.24

    ctx.beginPath()
    ctx.arc(cx, cy, r, 0, Math.PI * 2)
    ctx.strokeStyle = 'rgba(255,255,255,0.07)'
    ctx.lineWidth = lw
    ctx.stroke()

    if (fix && acc !== null) {
      const frac = acc <= 3 ? 1.0 : acc <= 10 ? 0.85 : acc <= 25 ? 0.6 : acc <= 50 ? 0.35 : 0.12
      const col = acc <= 10 ? '#00ff94' : acc <= 25 ? '#f7b731' : '#ff4842'
      ctx.save()
      ctx.shadowColor = col
      ctx.shadowBlur = 10
      ctx.beginPath()
      ctx.arc(cx, cy, r, -Math.PI / 2, -Math.PI / 2 + Math.PI * 2 * frac)
      ctx.strokeStyle = col
      ctx.lineWidth = lw
      ctx.lineCap = 'round'
      ctx.stroke()
      ctx.restore()

      ctx.textAlign = 'center'
      ctx.textBaseline = 'middle'
      ctx.fillStyle = '#f0f4f8'
      ctx.font = `700 ${r * 0.65}px 'Oxanium', monospace`
      ctx.fillText(acc < 100 ? `${Math.round(acc)}` : '99+', cx, cy)
    } else {
      ctx.textAlign = 'center'
      ctx.textBaseline = 'middle'
      ctx.fillStyle = 'rgba(255,255,255,0.12)'
      ctx.font = `700 ${r * 0.50}px 'Oxanium', monospace`
      ctx.fillText('--', cx, cy)
    }

    ctx.fillStyle = fix ? '#00ff94bb' : 'rgba(255,255,255,0.20)'
    ctx.font = `700 ${r * 0.28}px 'Rajdhani', sans-serif`
    ctx.textBaseline = 'middle'
    ctx.fillText(fix ? 'GPS m' : 'NO FIX', cx, cy + r + lw * 0.72)
  })
</script>

<svelte:head>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin="">
  <link href="https://fonts.googleapis.com/css2?family=Rajdhani:wght@500;600;700&family=Oxanium:wght@400;600;700;800&display=swap" rel="stylesheet">
</svelte:head>

<div class="home-page">

  <!-- Layered background -->
  <div class="bg-layer" aria-hidden="true"></div>

  <!-- ── Header ────────────────────────────────────────────────────────────── -->
  <header class="home-header">
    <div class="brand">
      <div class="brand-mark" aria-hidden="true">
        <svg viewBox="0 0 16 10" fill="none">
          <path d="M0 5 L5 1 L8 5 L11 1 L16 5 L11 9 L8 5 L5 9 Z" fill="currentColor" opacity="0.9"/>
        </svg>
      </div>
      <div class="brand-text">
        <span class="brand-name">CATEYE</span>
        <span class="brand-ver">{version}</span>
      </div>
    </div>
    <div class="header-actions">
      {#if updateAvailable}
        <a href="/settings" class="update-badge">
          <span class="update-dot"></span>
          UPDATE
        </a>
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

      <!-- Status bar -->
      <div class="status-bar">
        <div class="status-left">
          <span class="status-pip" class:live={isLive}></span>
          <span class="status-text" class:live={isLive}>{isLive ? 'ONLINE' : 'OFFLINE'}</span>
        </div>
        <div class="status-right">
          <canvas bind:this={netCanvas} class="sig-canvas" aria-label="Network"></canvas>
          <div class="sig-divider"></div>
          <canvas bind:this={gpsCanvas} class="sig-canvas" aria-label="GPS"></canvas>
        </div>
      </div>

      <!-- Instrument cluster -->
      {#if isLive}
        <div class="cluster">
          <!-- Temp: left column, full height -->
          <div class="cluster-temp">
            <canvas bind:this={tempCanvas} class="temp-canvas" aria-label="Temperature"></canvas>
          </div>
          <!-- Resource rings: right column, stacked -->
          <div class="cluster-rings">
            <canvas bind:this={cpuCanvas} class="ring-canvas" aria-label="CPU"></canvas>
            <canvas bind:this={gpuCanvas} class="ring-canvas" aria-label="GPU"></canvas>
            <canvas bind:this={memCanvas} class="ring-canvas" aria-label="MEM"></canvas>
          </div>
        </div>
      {/if}

      <!-- Drive stats -->
      {#if stats}
        <div class="stats-card">
          <div class="stats-label">THIS WEEK</div>
          <div class="stats-grid">
            <div class="stat">
              <span class="stat-val">{weekRoutes}</span>
              <span class="stat-key">DRIVES</span>
            </div>
            <div class="stat-line"></div>
            <div class="stat">
              <span class="stat-val">{weekDistanceDisplay}</span>
              <span class="stat-key">DISTANCE</span>
            </div>
            <div class="stat-line"></div>
            <div class="stat">
              <span class="stat-val">{allRoutes}</span>
              <span class="stat-key">ALL TIME</span>
            </div>
          </div>
        </div>
      {/if}

      <!-- Storage -->
      {#if storage}
        <div class="storage-card">
          <div class="storage-meta">
            <span class="storage-key">STORAGE</span>
            <span class="storage-val">{storageUsedGb}<span class="storage-of"> / {storageTotalGb} GB</span></span>
            <span class="storage-pct" class:warn={storagePct > 80} class:crit={storagePct > 95}>{storagePct}%</span>
          </div>
          <div class="storage-track">
            <div
              class="storage-fill"
              class:warn={storagePct > 80}
              class:crit={storagePct > 95}
              style="width: {storagePct}%"
            ></div>
            <div class="storage-notch" style="left: 25%"></div>
            <div class="storage-notch" style="left: 50%"></div>
            <div class="storage-notch" style="left: 75%"></div>
          </div>
        </div>
      {/if}

    {/if}
  </main>

  <!-- ── Nav ───────────────────────────────────────────────────────────────── -->
  <nav class="home-nav">
    <a href="/driving" class="nav-btn nav-drive">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
        <circle cx="12" cy="12" r="10"/>
        <polygon points="10,8 16,12 10,16" fill="currentColor" stroke="none"/>
      </svg>
      DRIVE
    </a>
    <div class="nav-secondary">
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
    </div>
  </nav>

</div>

<style>
  /* ── Design tokens ───────────────────────────────────────────────────────── */
  :root {
    --bg:        #080b10;
    --surface:   #0e1520;
    --surface2:  #131c2b;
    --border:    rgba(247,183,49,0.10);
    --amber:     #f7b731;
    --green:     #00ff94;
    --red:       #ff4842;
    --purple:    #b06bff;
    --txt:       #f0f4f8;
    --muted:     rgba(255,255,255,0.36);
    --dim:       rgba(255,255,255,0.14);
    --font-ui:   'Rajdhani', system-ui, sans-serif;
    --font-data: 'Oxanium', 'Consolas', monospace;
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

  /* Angled grid background — gives structural depth */
  .bg-layer {
    position: fixed;
    inset: 0;
    pointer-events: none;
    z-index: 0;
    background:
      radial-gradient(ellipse 80% 50% at 50% -10%, rgba(247,183,49,0.04) 0%, transparent 65%),
      radial-gradient(ellipse 40% 60% at 95% 90%,  rgba(176,107,255,0.04) 0%, transparent 55%);
  }

  /* Diagonal grid overlay */
  .bg-layer::before {
    content: '';
    position: absolute;
    inset: 0;
    background-image:
      linear-gradient(rgba(255,255,255,0.018) 1px, transparent 1px),
      linear-gradient(90deg, rgba(255,255,255,0.018) 1px, transparent 1px);
    background-size: 32px 32px;
    mask-image: radial-gradient(ellipse 90% 80% at 50% 40%, black 30%, transparent 80%);
  }

  /* Subtle vignette */
  .bg-layer::after {
    content: '';
    position: absolute;
    inset: 0;
    background: radial-gradient(ellipse 100% 100% at 50% 50%, transparent 50%, rgba(8,11,16,0.65) 100%);
  }

  /* ── Header ──────────────────────────────────────────────────────────────── */
  .home-header {
    position: relative;
    z-index: 1;
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0.8rem 1rem 0.65rem;
    border-bottom: 1px solid rgba(247,183,49,0.08);
  }

  .brand {
    display: flex;
    align-items: center;
    gap: 0.55rem;
  }

  .brand-mark {
    width: 1.5rem;
    height: 0.95rem;
    color: var(--amber);
    filter: drop-shadow(0 0 6px rgba(247,183,49,0.5));
    flex-shrink: 0;
  }
  .brand-mark svg { width: 100%; height: 100%; }

  .brand-text {
    display: flex;
    flex-direction: column;
    gap: 0;
    line-height: 1;
  }

  .brand-name {
    font-family: var(--font-data);
    font-size: 1rem;
    font-weight: 800;
    letter-spacing: 0.14em;
    color: var(--amber);
    text-shadow: 0 0 24px rgba(247,183,49,0.35);
  }

  .brand-ver {
    font-family: var(--font-data);
    font-size: 0.58rem;
    font-weight: 400;
    color: var(--muted);
    letter-spacing: 0.05em;
    margin-top: 1px;
  }

  .header-actions {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }

  .update-badge {
    display: flex;
    align-items: center;
    gap: 0.35rem;
    font-family: var(--font-data);
    font-size: 0.58rem;
    font-weight: 700;
    letter-spacing: 0.1em;
    color: var(--bg);
    background: var(--amber);
    padding: 0.2rem 0.5rem;
    border-radius: 3px;
    text-decoration: none;
    animation: badge-flash 2.5s ease-in-out infinite;
  }
  .update-dot {
    width: 5px;
    height: 5px;
    border-radius: 50%;
    background: rgba(0,0,0,0.5);
    flex-shrink: 0;
  }
  @keyframes badge-flash {
    0%, 100% { opacity: 1; }
    50%       { opacity: 0.72; }
  }

  .icon-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 2rem;
    height: 2rem;
    border-radius: 6px;
    color: var(--muted);
    background: rgba(255,255,255,0.04);
    border: 1px solid rgba(255,255,255,0.06);
    text-decoration: none;
    transition: color 0.12s, background 0.12s;
  }
  .icon-btn svg { width: 0.95rem; height: 0.95rem; }
  .icon-btn:active { background: rgba(247,183,49,0.1); color: var(--amber); }

  /* ── Main ────────────────────────────────────────────────────────────────── */
  .home-content {
    position: relative;
    z-index: 1;
    flex: 1;
    display: flex;
    flex-direction: column;
    gap: 0.55rem;
    padding: 0.65rem 0.875rem 0.4rem;
    overflow-y: auto;
  }

  /* ── Loading ─────────────────────────────────────────────────────────────── */
  .loading-state {
    flex: 1;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 0.9rem;
  }

  .spinner {
    width: 1.8rem;
    height: 1.8rem;
    border: 1.5px solid rgba(247,183,49,0.12);
    border-top-color: var(--amber);
    border-radius: 50%;
    animation: spin 0.9s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  .loading-label {
    font-family: var(--font-data);
    font-size: 0.6rem;
    letter-spacing: 0.22em;
    color: var(--dim);
    font-weight: 600;
  }

  /* ── Status bar ──────────────────────────────────────────────────────────── */
  .status-bar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0.5rem 0.8rem;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 9px;
  }

  .status-left {
    display: flex;
    align-items: center;
    gap: 0.42rem;
  }

  .status-pip {
    display: block;
    width: 0.5rem;
    height: 0.5rem;
    border-radius: 50%;
    background: var(--dim);
    flex-shrink: 0;
    transition: background 0.3s;
  }
  .status-pip.live {
    background: var(--green);
    box-shadow: 0 0 6px var(--green);
    animation: pip-breathe 3s ease-in-out infinite;
  }
  @keyframes pip-breathe {
    0%, 100% { box-shadow: 0 0 4px var(--green); }
    50%       { box-shadow: 0 0 10px var(--green); }
  }

  .status-text {
    font-family: var(--font-data);
    font-size: 0.72rem;
    font-weight: 700;
    letter-spacing: 0.12em;
    color: var(--muted);
    transition: color 0.3s;
  }
  .status-text.live { color: var(--green); }

  .status-right {
    display: flex;
    align-items: center;
    gap: 0.35rem;
  }

  .sig-canvas { display: block; width: 38px; height: 44px; }
  .sig-divider {
    width: 1px;
    height: 28px;
    background: rgba(255,255,255,0.08);
    flex-shrink: 0;
  }

  /* ── Instrument cluster ──────────────────────────────────────────────────── */
  .cluster {
    display: flex;
    gap: 0.5rem;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 0.6rem 0.6rem 0.45rem;
  }

  .cluster-temp {
    flex: 1.15;
    min-width: 0;
  }

  .temp-canvas {
    display: block;
    width: 100%;
    aspect-ratio: 1 / 0.86;
  }

  .cluster-rings {
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
    justify-content: space-between;
    flex-shrink: 0;
  }

  .ring-canvas {
    display: block;
    width: 74px;
    height: 80px;
  }

  /* ── Stats card ──────────────────────────────────────────────────────────── */
  .stats-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 0.6rem 0.875rem 0.65rem;
  }

  .stats-label {
    font-family: var(--font-data);
    font-size: 0.56rem;
    font-weight: 600;
    letter-spacing: 0.18em;
    color: var(--muted);
    margin-bottom: 0.5rem;
  }

  .stats-grid {
    display: flex;
    align-items: center;
  }

  .stat {
    flex: 1;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 0.12rem;
  }

  .stat-val {
    font-family: var(--font-data);
    font-size: 1.4rem;
    font-weight: 700;
    color: var(--txt);
    line-height: 1;
    letter-spacing: -0.01em;
  }

  .stat-key {
    font-family: var(--font-ui);
    font-size: 0.54rem;
    font-weight: 600;
    letter-spacing: 0.13em;
    color: var(--dim);
  }

  .stat-line {
    width: 1px;
    height: 2.2rem;
    background: rgba(247,183,49,0.1);
    flex-shrink: 0;
  }

  /* ── Storage card ────────────────────────────────────────────────────────── */
  .storage-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 0.6rem 0.875rem;
  }

  .storage-meta {
    display: flex;
    align-items: baseline;
    gap: 0.4rem;
    margin-bottom: 0.52rem;
  }

  .storage-key {
    font-family: var(--font-data);
    font-size: 0.56rem;
    font-weight: 600;
    letter-spacing: 0.16em;
    color: var(--muted);
    flex: 1;
  }

  .storage-val {
    font-family: var(--font-data);
    font-size: 0.76rem;
    font-weight: 600;
    color: var(--txt);
  }
  .storage-of { color: var(--muted); font-weight: 400; }

  .storage-pct {
    font-family: var(--font-data);
    font-size: 0.68rem;
    color: var(--amber);
    min-width: 2.4rem;
    text-align: right;
  }
  .storage-pct.warn { color: #ff9500; }
  .storage-pct.crit { color: var(--red); }

  .storage-track {
    position: relative;
    height: 4px;
    background: rgba(255,255,255,0.06);
    border-radius: 2px;
    overflow: visible;
  }

  .storage-fill {
    height: 100%;
    background: var(--amber);
    border-radius: 2px;
    box-shadow: 0 0 7px rgba(247,183,49,0.45);
    transition: width 0.7s ease;
  }
  .storage-fill.warn { background: #ff9500; box-shadow: 0 0 7px rgba(255,149,0,0.45); }
  .storage-fill.crit { background: var(--red); box-shadow: 0 0 7px rgba(255,72,66,0.45); }

  .storage-notch {
    position: absolute;
    top: -2px;
    width: 1px;
    height: 8px;
    background: rgba(0,0,0,0.7);
    transform: translateX(-50%);
    pointer-events: none;
  }

  /* ── Navigation ──────────────────────────────────────────────────────────── */
  .home-nav {
    position: relative;
    z-index: 1;
    display: flex;
    gap: 0.45rem;
    align-items: stretch;
    padding: 0.55rem 0.875rem calc(0.55rem + env(safe-area-inset-bottom, 0px));
    border-top: 1px solid rgba(247,183,49,0.07);
    background: rgba(8,11,16,0.96);
  }

  .nav-drive {
    flex: 1.4;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 0.22rem;
    padding: 0.6rem 0.5rem;
    border-radius: 9px;
    font-family: var(--font-data);
    font-size: 0.65rem;
    font-weight: 800;
    letter-spacing: 0.14em;
    color: var(--bg);
    background: var(--amber);
    border: none;
    text-decoration: none;
    box-shadow: 0 0 18px rgba(247,183,49,0.25), 0 2px 8px rgba(0,0,0,0.4);
    transition: box-shadow 0.15s, transform 0.12s;
  }
  .nav-drive svg { width: 1.15rem; height: 1.15rem; }
  .nav-drive:active { box-shadow: 0 0 26px rgba(247,183,49,0.4); transform: scale(0.97); }

  .nav-secondary {
    flex: 1;
    display: flex;
    flex-direction: column;
    gap: 0.35rem;
  }

  .nav-btn {
    flex: 1;
    display: flex;
    flex-direction: row;
    align-items: center;
    justify-content: center;
    gap: 0.4rem;
    padding: 0.4rem 0.5rem;
    border-radius: 7px;
    font-family: var(--font-ui);
    font-size: 0.64rem;
    font-weight: 700;
    letter-spacing: 0.1em;
    color: var(--muted);
    background: rgba(255,255,255,0.04);
    border: 1px solid rgba(255,255,255,0.06);
    text-decoration: none;
    transition: background 0.12s, color 0.12s;
  }
  .nav-btn svg { width: 0.9rem; height: 0.9rem; flex-shrink: 0; }
  .nav-btn:active { background: rgba(255,255,255,0.09); color: var(--txt); }
</style>
