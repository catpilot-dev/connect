<script>
  import { isMetric } from '../stores.js'

  /**
   * Canvas-based HUD overlay for recorded routes.
   * Draws lane lines, path, lead car, speed, engagement — same as stock UI.
   * Fed by pre-projected data from /v1/route/{routeName}/hud_data.
   *
   * Props:
   *   frames     — array of {t, vEgo, sdEnabled, model: {laneLines, path, lead, ...}}
   *   currentTime — current playback time (seconds)
   *   visible    — show/hide overlay
   *   timeOffset — seconds to add to currentTime for frame lookup (compensate pipeline latency)
   */

  let {
    frames = [],
    currentTime = 0,
    visible = false,
    timeOffset = 0,
  } = $props()

  let canvasEl = $state(null)
  let animFrame = null
  let lastFrame = null  // persist last valid frame to avoid blinks

  // Binary search for closest frame at or before currentTime
  function findFrame(t) {
    if (!frames.length) return lastFrame
    let lo = 0, hi = frames.length - 1
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1
      if (frames[mid].t <= t) lo = mid
      else hi = mid - 1
    }
    const found = frames[lo].t <= t ? frames[lo] : null
    if (found) lastFrame = found
    return lastFrame
  }

  // ── Drawing functions (from DrivingPage — stock UI algorithm) ──

  function drawPolygon(ctx, w, h, points, color, gradient = null) {
    if (!points || points.length < 3) return
    ctx.save()
    if (gradient) {
      // Vertical gradient based on y range of polygon
      let minY = Infinity, maxY = -Infinity
      for (const p of points) {
        const py = p[1] * h
        if (py < minY) minY = py
        if (py > maxY) maxY = py
      }
      const grad = ctx.createLinearGradient(0, maxY, 0, minY)  // bottom → top
      for (let i = 0; i < gradient.colors.length; i++) {
        grad.addColorStop(gradient.stops[i], gradient.colors[i])
      }
      ctx.fillStyle = grad
    } else {
      ctx.fillStyle = color
    }
    ctx.beginPath()
    ctx.moveTo(points[0][0] * w, points[0][1] * h)
    for (let i = 1; i < points.length; i++) {
      ctx.lineTo(points[i][0] * w, points[i][1] * h)
    }
    ctx.closePath()
    ctx.fill()
    ctx.restore()
  }

  function drawLead(ctx, w, h, lead) {
    if (!lead?.pt) return
    const px = lead.pt[0] * w
    const py = lead.pt[1] * h
    const d = lead.dRel

    const sz = Math.max(15, Math.min(30, (25 * 30) / (d / 3 + 30))) * 2.35

    let fillAlpha = 0
    if (d < 40) {
      fillAlpha = 255 * (1.0 - d / 40)
      if (lead.vRel < 0) fillAlpha += 255 * (-lead.vRel / 10)
      fillAlpha = Math.min(255, Math.max(0, fillAlpha))
    }

    const gxo = sz / 5, gyo = sz / 10
    ctx.save()
    ctx.fillStyle = 'rgba(218, 202, 37, 1.0)'
    ctx.beginPath()
    ctx.moveTo(px + sz * 1.35 + gxo, py + sz + gyo)
    ctx.lineTo(px, py - gyo)
    ctx.lineTo(px - sz * 1.35 - gxo, py + sz + gyo)
    ctx.closePath()
    ctx.fill()

    ctx.fillStyle = `rgba(201, 34, 49, ${fillAlpha / 255})`
    ctx.beginPath()
    ctx.moveTo(px + sz * 1.25, py + sz)
    ctx.lineTo(px, py)
    ctx.lineTo(px - sz * 1.25, py + sz)
    ctx.closePath()
    ctx.fill()
    ctx.restore()
  }

  // ── Plugin HUD elements (speedlimitd sign, road ref, BMW temps/emblem) ──
  // Data comes from bus_logger's pluginBusLog record in the rlog.

  function roundRectPath(ctx, x, y, rw, rh, r) {
    ctx.beginPath()
    if (ctx.roundRect) { ctx.roundRect(x, y, rw, rh, r); return }
    ctx.moveTo(x + r, y)
    ctx.arcTo(x + rw, y, x + rw, y + rh, r)
    ctx.arcTo(x + rw, y + rh, x, y + rh, r)
    ctx.arcTo(x, y + rh, x, y, r)
    ctx.arcTo(x, y, x + rw, y, r)
    ctx.closePath()
  }

  function drawSpeedLimitSign(ctx, sx, sy, sl) {
    if (!sl?.limit) return
    // Vienna-style sign below the MAX box; faded while unconfirmed
    const cx = 160 * sx
    const cy = (45 + 204 + 130) * sy
    const r = 85 * sx
    ctx.save()
    ctx.globalAlpha = sl.confirmed ? 1.0 : 0.5
    ctx.beginPath()
    ctx.arc(cx, cy, r, 0, Math.PI * 2)
    ctx.fillStyle = 'white'
    ctx.fill()
    ctx.lineWidth = r * 0.28
    ctx.strokeStyle = 'rgba(201, 34, 49, 1)'
    ctx.beginPath()
    ctx.arc(cx, cy, r - ctx.lineWidth / 2, 0, Math.PI * 2)
    ctx.stroke()
    ctx.fillStyle = 'black'
    ctx.font = `bold ${Math.round(r * 0.95)}px system-ui, -apple-system, sans-serif`
    ctx.textAlign = 'center'
    ctx.textBaseline = 'middle'
    ctx.fillText(String(sl.limit), cx, cy + r * 0.05)
    ctx.restore()
  }

  function drawRoadInfo(ctx, w, h, sx, sy, sl) {
    const text = sl?.wayRef || sl?.roadName
    if (!text) return
    ctx.save()
    ctx.font = `bold ${Math.round(52 * sy)}px system-ui, -apple-system, sans-serif`
    const tw = ctx.measureText(text).width
    const padX = 28 * sx
    const rh = 76 * sy
    const rx = w / 2 - tw / 2 - padX
    const ry = h - rh - 34 * sy
    roundRectPath(ctx, rx, ry, tw + padX * 2, rh, 14 * sx)
    ctx.fillStyle = 'rgba(21, 21, 21, 0.75)'
    ctx.fill()
    ctx.fillStyle = 'rgba(96, 165, 250, 1)'
    ctx.textAlign = 'center'
    ctx.textBaseline = 'middle'
    ctx.fillText(text, w / 2, ry + rh / 2)
    ctx.restore()
  }

  function drawTemps(ctx, w, h, sx, sy, temps) {
    if (!temps) return
    ctx.save()
    ctx.font = `bold ${Math.round(56 * sy)}px ui-monospace, monospace`
    const lines = [`${temps.coolant}°C`, `${temps.oil}°C`]
    const tw = Math.max(...lines.map(t => ctx.measureText(t).width))
    const padX = 22 * sx
    const lineH = 64 * sy
    const rw = tw + padX * 2
    const rh = lineH * 2 + 24 * sy
    const rx = w - rw - 40 * sx
    const ry = h - rh - 40 * sy
    roundRectPath(ctx, rx, ry, rw, rh, 14 * sx)
    ctx.fillStyle = 'rgba(21, 21, 21, 0.7)'
    ctx.fill()
    ctx.fillStyle = 'rgba(74, 222, 128, 1)'
    ctx.textAlign = 'right'
    ctx.textBaseline = 'middle'
    ctx.fillText(lines[0], rx + rw - padX, ry + 12 * sy + lineH * 0.5)
    ctx.fillText(lines[1], rx + rw - padX, ry + 12 * sy + lineH * 1.5)
    ctx.restore()
  }

  function drawBmwEmblem(ctx, w, sx, sy, engaged) {
    const r = 62 * sx
    const cx = w - 60 * sx - r
    const cy = 45 * sy + r
    ctx.save()
    // Outer ring: green when engaged (matches the live HUD accent)
    ctx.beginPath()
    ctx.arc(cx, cy, r, 0, Math.PI * 2)
    ctx.fillStyle = engaged ? 'rgba(34, 197, 94, 1)' : 'rgba(0, 0, 0, 1)'
    ctx.fill()
    ctx.beginPath()
    ctx.arc(cx, cy, r * 0.86, 0, Math.PI * 2)
    ctx.fillStyle = 'black'
    ctx.fill()
    // Quadrants: alternating blue/white roundel
    const qr = r * 0.62
    for (let q = 0; q < 4; q++) {
      ctx.beginPath()
      ctx.moveTo(cx, cy)
      ctx.arc(cx, cy, qr, (q * Math.PI) / 2 - Math.PI / 2, ((q + 1) * Math.PI) / 2 - Math.PI / 2)
      ctx.closePath()
      ctx.fillStyle = q % 2 === 0 ? 'rgba(38, 132, 255, 1)' : 'white'
      ctx.fill()
    }
    ctx.restore()
  }

  function renderFrame(frame) {
    if (!canvasEl) return
    const ctx = canvasEl.getContext('2d')
    const w = canvasEl.width
    const h = canvasEl.height
    if (w === 0 || h === 0) return

    ctx.clearRect(0, 0, w, h)
    if (!frame) return

    const model = frame.model
    const engaged = frame.sdEnabled
    // Scale factor: stock UI is 1928x1208, canvas may be different
    const sx = w / 1928
    const sy = h / 1208

    // ── Header gradient (stock: top 300px, black→transparent) ──
    const grad = ctx.createLinearGradient(0, 0, 0, 300 * sy)
    grad.addColorStop(0, 'rgba(0, 0, 0, 0.45)')
    grad.addColorStop(1, 'rgba(0, 0, 0, 0)')
    ctx.fillStyle = grad
    ctx.fillRect(0, 0, w, 300 * sy)

    // ── Clip model rendering to content area (stock: 30px border inset) ──
    const borderPx = 30 * sx
    ctx.save()
    ctx.beginPath()
    ctx.rect(borderPx, borderPx, w - 2 * borderPx, h - 2 * borderPx)
    ctx.clip()

    // ── Road edges ──
    if (model?.roadEdges) {
      for (let i = 0; i < model.roadEdges.length; i++) {
        const alpha = Math.max(0, Math.min(1, 1.0 - (model.roadEdgeStds?.[i] ?? 1.0)))
        if (alpha > 0.05)
          drawPolygon(ctx, w, h, model.roadEdges[i], `rgba(255, 0, 0, ${alpha})`)
      }
    }

    // ── Lane lines ──
    if (model?.laneLines) {
      for (let i = 0; i < model.laneLines.length && i < 4; i++) {
        const prob = model.laneLineProbs?.[i] ?? 0
        const alpha = Math.max(0, Math.min(0.7, prob))
        if (alpha > 0.05)
          drawPolygon(ctx, w, h, model.laneLines[i], `rgba(255, 255, 255, ${alpha})`)
      }
    }

    // ── Path prediction (stock gradient: near=opaque → far=transparent) ──
    if (model?.path) {
      const pathGradient = engaged
        ? { colors: ['rgba(13, 248, 122, 0.4)', 'rgba(114, 255, 92, 0.35)', 'rgba(114, 255, 92, 0)'], stops: [0, 0.5, 1] }
        : { colors: ['rgba(242, 242, 242, 0.4)', 'rgba(242, 242, 242, 0.35)', 'rgba(242, 242, 242, 0)'], stops: [0, 0.5, 1] }
      drawPolygon(ctx, w, h, model.path, null, pathGradient)
    }

    // ── Lead car chevron ──
    if (model?.lead) {
      drawLead(ctx, w, h, model.lead)
    }

    // End model clipping region
    ctx.restore()

    // ── MAX speed box (stock: top-left, X=60, Y=45, 200x204) ──
    // vCruiseCluster is the cluster display value (already in km/h)
    const vCruise = frame.vCruiseCluster ?? 0
    const cruiseSet = frame.cruiseEnabled && vCruise > 0 && vCruise < 255
    if (cruiseSet || frame.cruiseEnabled) {
      const boxW = ($isMetric ? 200 : 172) * sx
      const boxH = 204 * sy
      const boxX = 60 * sx
      const boxY = 45 * sy

      // Box background (roundRect with fallback for older browsers)
      ctx.save()
      ctx.beginPath()
      const r = 35 * sx
      if (ctx.roundRect) {
        ctx.roundRect(boxX, boxY, boxW, boxH, r)
      } else {
        ctx.moveTo(boxX + r, boxY)
        ctx.arcTo(boxX + boxW, boxY, boxX + boxW, boxY + boxH, r)
        ctx.arcTo(boxX + boxW, boxY + boxH, boxX, boxY + boxH, r)
        ctx.arcTo(boxX, boxY + boxH, boxX, boxY, r)
        ctx.arcTo(boxX, boxY, boxX + boxW, boxY, r)
        ctx.closePath()
      }
      ctx.fillStyle = 'rgba(0, 0, 0, 0.65)'
      ctx.fill()
      ctx.strokeStyle = 'rgba(255, 255, 255, 0.3)'
      ctx.lineWidth = 6 * sx
      ctx.stroke()
      ctx.restore()

      // "MAX" label — color based on engagement
      let maxColor = 'rgba(166, 166, 166, 1)'  // grey disengaged
      if (engaged) maxColor = 'rgba(128, 216, 166, 1)'  // green engaged

      ctx.save()
      ctx.textAlign = 'center'

      // "MAX" label
      ctx.fillStyle = maxColor
      ctx.font = `bold ${Math.round(40 * sy)}px sans-serif`
      ctx.textBaseline = 'middle'
      ctx.fillText('MAX', boxX + boxW / 2, boxY + 47 * sy)

      // Set speed value — vCruiseCluster is already in km/h from the cluster
      const displaySpeed = $isMetric ? vCruise : vCruise * 0.621371
      const hasSpeed = frame.cruiseEnabled && vCruise > 0 && vCruise < 255
      const setSpeedText = hasSpeed ? String(Math.round(displaySpeed)) : '–'
      ctx.fillStyle = hasSpeed ? 'rgba(255, 255, 255, 1)' : 'rgba(114, 114, 114, 1)'
      ctx.font = `bold ${Math.round(90 * sy)}px sans-serif`
      ctx.textBaseline = 'middle'
      ctx.fillText(setSpeedText, boxX + boxW / 2, boxY + 132 * sy)
      ctx.restore()
    }

    // ── Current speed (stock: center X, Y=180) ──
    // Prefer vEgoCluster (instrument cluster speed) over vEgo (raw)
    const vEgo = frame.vEgoCluster || frame.vEgo || 0
    const speed = $isMetric ? vEgo * 3.6 : vEgo * 2.23694
    const speedUnit = $isMetric ? 'km/h' : 'mph'

    ctx.save()
    ctx.fillStyle = 'rgba(255, 255, 255, 0.95)'
    ctx.font = `bold ${Math.round(176 * sy)}px system-ui, -apple-system, sans-serif`
    ctx.textAlign = 'center'
    ctx.textBaseline = 'middle'
    ctx.fillText(Math.round(speed), w * 0.5, 180 * sy)

    // Speed unit (stock: Y=290)
    ctx.font = `500 ${Math.round(66 * sy)}px system-ui, -apple-system, sans-serif`
    ctx.fillStyle = 'rgba(255, 255, 255, 0.78)'
    ctx.fillText(speedUnit, w * 0.5, 290 * sy)
    ctx.restore()

    // ── Plugin HUD elements (from bus_logger's pluginBusLog record) ──
    drawSpeedLimitSign(ctx, sx, sy, frame.sl)
    drawRoadInfo(ctx, w, h, sx, sy, frame.sl)
    drawTemps(ctx, w, h, sx, sy, frame.temps)
    if (frame.temps) drawBmwEmblem(ctx, w, sx, sy, engaged)

    // ── Alerts (stock style: colored background bar at bottom) ──
    // alertSize: 0=none, 1=small(271px), 2=mid(420px), 3=full
    // alertStatus: 0=normal(#151515), 1=userPrompt(#DA6F25), 2=critical(#C92231)
    const alertSize = frame.alertSize ?? 0
    if (alertSize > 0 && frame.alertText1) {
      const ALERT_STATUS_COLORS = {
        0: 'rgba(21, 21, 21, 0.94)',       // normal
        1: 'rgba(218, 111, 37, 0.94)',     // userPrompt
        2: 'rgba(201, 34, 49, 0.94)',      // critical
      }
      const ALERT_HEIGHTS = { 1: 271, 2: 420 }  // small, mid
      const margin = 40 * sx
      const padding = 60 * sx
      const bgColor = ALERT_STATUS_COLORS[frame.alertStatus] ?? ALERT_STATUS_COLORS[0]

      ctx.save()
      if (alertSize === 3) {
        // Full screen alert
        ctx.fillStyle = bgColor
        ctx.fillRect(0, 0, w, h)

        ctx.fillStyle = 'white'
        const isLong = frame.alertText1.length > 15
        ctx.font = `bold ${Math.round((isLong ? 132 : 177) * sy)}px system-ui, -apple-system, sans-serif`
        ctx.textAlign = 'center'
        ctx.textBaseline = 'top'
        ctx.fillText(frame.alertText1, w / 2, (isLong ? 200 : 270) * sy)

        if (frame.alertText2) {
          ctx.font = `${Math.round(88 * sy)}px system-ui, -apple-system, sans-serif`
          ctx.textBaseline = 'bottom'
          ctx.fillText(frame.alertText2, w / 2, h - (isLong ? 361 : 420) * sy + 300 * sy)
        }
      } else {
        // Small or mid alert bar at bottom
        const alertH = (ALERT_HEIGHTS[alertSize] ?? 420) * sy
        const rx = margin
        const ry = h - alertH + margin
        const rw = w - margin * 2
        const rh = alertH - margin * 2
        const radius = 30 * sx

        ctx.beginPath()
        if (ctx.roundRect) {
          ctx.roundRect(rx, ry, rw, rh, radius)
        } else {
          ctx.moveTo(rx + radius, ry)
          ctx.arcTo(rx + rw, ry, rx + rw, ry + rh, radius)
          ctx.arcTo(rx + rw, ry + rh, rx, ry + rh, radius)
          ctx.arcTo(rx, ry + rh, rx, ry, radius)
          ctx.arcTo(rx, ry, rx + rw, ry, radius)
          ctx.closePath()
        }
        ctx.fillStyle = bgColor
        ctx.fill()

        ctx.fillStyle = 'white'
        if (alertSize === 1) {
          // Small: single line, centered
          ctx.font = `bold ${Math.round(74 * sy)}px system-ui, -apple-system, sans-serif`
          ctx.textAlign = 'center'
          ctx.textBaseline = 'middle'
          ctx.fillText(frame.alertText1, w / 2, ry + rh / 2)
        } else {
          // Mid: text1 bold + text2 regular
          ctx.font = `bold ${Math.round(88 * sy)}px system-ui, -apple-system, sans-serif`
          ctx.textAlign = 'center'
          ctx.textBaseline = 'top'
          ctx.fillText(frame.alertText1, w / 2, ry + padding)

          if (frame.alertText2) {
            ctx.font = `${Math.round(66 * sy)}px system-ui, -apple-system, sans-serif`
            ctx.fillText(frame.alertText2, w / 2, ry + padding + 88 * sy + 45 * sy)
          }
        }
      }
      ctx.restore()
    }
  }

  // ── Render loop: sync canvas to currentTime ──

  let lastRenderedTime = -1

  function tick() {
    if (!visible || !canvasEl) {
      animFrame = null
      return
    }
    // Only re-render when time changes enough (avoid redundant draws)
    if (Math.abs(currentTime - lastRenderedTime) > 0.05) {
      const frame = findFrame(currentTime + timeOffset)
      renderFrame(frame)
      lastRenderedTime = currentTime
    }
    animFrame = requestAnimationFrame(tick)
  }

  // Resize canvas to match container
  function resizeCanvas() {
    if (!canvasEl) return
    const rect = canvasEl.parentElement?.getBoundingClientRect()
    if (rect) {
      const dpr = window.devicePixelRatio || 1
      canvasEl.width = rect.width * dpr
      canvasEl.height = rect.height * dpr
      canvasEl.style.width = rect.width + 'px'
      canvasEl.style.height = rect.height + 'px'
      lastRenderedTime = -1  // force re-render
    }
  }

  $effect(() => {
    if (visible && canvasEl) {
      resizeCanvas()
      window.addEventListener('resize', resizeCanvas)
      if (!animFrame) tick()
      return () => {
        window.removeEventListener('resize', resizeCanvas)
        if (animFrame) { cancelAnimationFrame(animFrame); animFrame = null }
      }
    } else if (!visible && animFrame) {
      cancelAnimationFrame(animFrame)
      animFrame = null
    }
  })
</script>

{#if visible}
  <canvas
    bind:this={canvasEl}
    class="absolute inset-0 w-full h-full pointer-events-none"
    style="z-index: 5"
  ></canvas>
{/if}
