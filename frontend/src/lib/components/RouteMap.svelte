<script>
  import { onMount, onDestroy } from 'svelte'
  import { wgs84ToGcj02 } from '../gcj02.js'
  import { getTileSource, TILE_SOURCES } from '../tileSource.js'

  /**
   * Leaflet GPS track map with:
   * - Polyline colored by engagement state (green/blue/grey)
   * - CircleMarker synced to video currentTime
   * - Auto-fit bounds on load
   * - Configurable tile source (AMap for China, CartoDB for overseas)
   */

  /** @type {{ coords: Array, events?: Array, currentTime?: number, durationMs?: number, selectionStart?: number, selectionEnd?: number, visible?: boolean, startLat?: number, startLng?: number }} */
  let { coords = [], events = [], currentTime = 0, durationMs = 0, selectionStart = 0, selectionEnd = 0, visible = true, startLat = null, startLng = null } = $props()

  let mapContainer = $state(null)
  let map = null
  let positionMarker = $state(null)
  let pathLayers = []

  // Dynamically import Leaflet (it accesses `window` on import)
  let L = null
  let tileConfig = TILE_SOURCES[getTileSource()] || TILE_SOURCES.amap

  /** Convert coord to map projection — GCJ-02 for AMap, passthrough for WGS-84 tiles */
  function toMapCoord(lat, lng) {
    return tileConfig.gcj02 ? wgs84ToGcj02(lat, lng) : [lat, lng]
  }

  onMount(async () => {
    // Dynamic import avoids SSR issues and reduces initial bundle
    const leaflet = await import('leaflet')
    L = leaflet.default || leaflet

    // Import Leaflet CSS from npm package (bundled by Vite, no CDN dependency)
    await import('leaflet/dist/leaflet.css')

    if (!mapContainer) return

    // Use route start position if available, fall back to Shanghai
    const initCenter = (startLat && startLng) ? toMapCoord(startLat, startLng) : [31.23, 121.47]
    map = L.map(mapContainer, {
      zoomControl: true,
      attributionControl: false,
      center: initCenter,
      zoom: 13,
    })

    // Tile layer from user preference (AMap for China, CartoDB for overseas)
    L.tileLayer(tileConfig.url, {
      maxZoom: tileConfig.maxZoom,
      subdomains: tileConfig.subdomains,
      className: tileConfig.className || undefined,
    }).addTo(map)

    // Position marker
    positionMarker = L.circleMarker([0, 0], {
      radius: 7,
      fillColor: '#3b82f6',
      fillOpacity: 1,
      color: '#ffffff',
      weight: 2,
      pane: 'markerPane',  // ensure it renders above polylines
    }).addTo(map)

    drawPath()

    // Firefox settles CSS grid layout later than Chromium — staggered invalidateSize
    for (const delay of [100, 300, 600]) {
      setTimeout(() => map?.invalidateSize(), delay)
    }
  })

  function drawPath() {
    if (!L || !map || coords.length === 0) return

    // Clear old layers
    for (const layer of pathLayers) map.removeLayer(layer)
    pathLayers = []

    // Build engagement lookup: time_ms → engaged boolean
    const engagedRanges = events
      .filter(e => e.type === 'engaged' && e.end_route_offset_millis != null)
      .map(e => [e.route_offset_millis, e.end_route_offset_millis])

    const overrideRanges = events
      .filter(e => e.type === 'overriding' && e.end_route_offset_millis != null)
      .map(e => [e.route_offset_millis, e.end_route_offset_millis])

    function getColor(timeMs) {
      for (const [s, e] of engagedRanges) if (timeMs >= s && timeMs <= e) return '#ffffff'
      for (const [s, e] of overrideRanges) if (timeMs >= s && timeMs <= e) return '#999999'
      return '#555555'
    }

    const hasSelection = selectionStart > 0 || (selectionEnd > 0 && selectionEnd < (durationMs / 1000) - 0.5)

    // Build colored segments — group consecutive points by color + selection state
    let currentColor = null
    let currentInSel = null
    let segment = []

    for (const pt of coords) {
      const color = getColor(pt.t * 1000)
      const inSel = !hasSelection || (pt.t >= selectionStart && pt.t <= selectionEnd)

      if ((color !== currentColor || inSel !== currentInSel) && segment.length > 1) {
        const line = L.polyline(segment, {
          color: currentColor,
          weight: currentInSel ? 3 : 2,
          opacity: currentInSel ? 0.8 : 0.2,
        })
        line.addTo(map)
        pathLayers.push(line)
        segment = [segment[segment.length - 1]]
      }
      currentColor = color
      currentInSel = inSel
      segment.push(toMapCoord(pt.lat, pt.lng))
    }
    // Final segment
    if (segment.length > 1) {
      const line = L.polyline(segment, {
        color: currentColor,
        weight: currentInSel ? 3 : 2,
        opacity: currentInSel ? 0.8 : 0.2,
      })
      line.addTo(map)
      pathLayers.push(line)
    }

    // Ensure position marker stays on top of path
    if (positionMarker) positionMarker.bringToFront()

    // Fit bounds to selection if active, otherwise full route
    if (hasSelection) {
      const selPoints = coords
        .filter(c => c.t >= selectionStart && c.t <= selectionEnd)
        .map(c => toMapCoord(c.lat, c.lng))
      if (selPoints.length > 1) {
        map.fitBounds(L.latLngBounds(selPoints), { padding: [20, 20] })
      }
    } else {
      const allPoints = coords.map(c => toMapCoord(c.lat, c.lng))
      if (allPoints.length > 0) {
        map.fitBounds(L.latLngBounds(allPoints), { padding: [20, 20] })
      }
    }
  }

  // Update position marker when currentTime changes
  $effect(() => {
    if (!positionMarker || coords.length === 0 || !L) return

    // Binary search for nearest point
    const timeSec = currentTime
    let lo = 0, hi = coords.length - 1
    while (lo < hi) {
      const mid = (lo + hi) >> 1
      if (coords[mid].t < timeSec) lo = mid + 1
      else hi = mid
    }

    const pt = coords[lo]
    if (pt) {
      positionMarker.setLatLng(toMapCoord(pt.lat, pt.lng))
      positionMarker.bringToFront()
    }
  })

  // Redraw path when coords/events/selection change
  $effect(() => {
    // Track dependencies
    coords; events; selectionStart; selectionEnd;
    if (coords.length > 0 && L) drawPath()
  })

  // Fix Leaflet tile loading when tab becomes visible
  $effect(() => {
    if (visible && map) {
      // Delay to let the DOM layout settle after tab switch
      setTimeout(() => map?.invalidateSize(), 50)
    }
  })

  // ResizeObserver — invalidateSize when container dimensions change (CSS grid settling, etc.)
  let resizeObserver = null
  $effect(() => {
    if (!mapContainer || !map) return
    resizeObserver = new ResizeObserver(() => map?.invalidateSize())
    resizeObserver.observe(mapContainer)
    return () => resizeObserver?.disconnect()
  })

  onDestroy(() => {
    resizeObserver?.disconnect()
    if (map) {
      map.remove()
      map = null
    }
  })
</script>

<style>
  :global(.amap-dark) {
    filter: invert(1) hue-rotate(180deg) brightness(0.95) contrast(1.1) saturate(0.15);
  }
</style>

<div class="overflow-hidden relative md:h-full">
  <div
    bind:this={mapContainer}
    class="aspect-video md:!aspect-auto md:h-full w-full"
    style="min-height: 200px"
  ></div>
</div>
