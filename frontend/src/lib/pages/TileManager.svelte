<script>
  import { onMount, onDestroy } from 'svelte'
  import { fetchTileList, startTileDownload, fetchTileProgress, cancelTileDownload, deleteTile } from '../api.js'
  import { wgs84ToGcj02 } from '../gcj02.js'
  import { tileLabel, tileName } from '../tile-names.js'
  import { getTileSource, TILE_SOURCES } from '../tileSource.js'

  let mapContainer = $state(null)
  let map = null
  let L = null
  let gridLayers = []
  let resizeObserver = null
  const tileConfig = TILE_SOURCES[getTileSource()] || TILE_SOURCES.amap

  // State
  let downloaded = $state([])
  let selected = $state([])
  let selectedForDelete = $state([])
  let storage = $state({ total_mb: 0, tile_count: 0 })
  let progress = $state({ active: false, total: 0, done: 0, current: null, error: null })
  let loading = $state(true)
  let error = $state(null)
  let pollTimer = null
  let highlightedTile = $state(null) // {lat, lon} of tile hovered in list
  let gridLayerMap = {}  // "lat,lon" -> Leaflet rectangle layer

  // Grid range for eastern China (covers most driving areas)
  const LAT_MIN = 18, LAT_MAX = 54
  const LON_MIN = 72, LON_MAX = 136
  const GRID_STEP = 2

  // Presets
  const PRESETS = [
    { name: 'Shanghai', tiles: [{ lat: 30, lon: 120 }] },
    { name: 'Yangtze Delta', tiles: [
      { lat: 30, lon: 118 }, { lat: 30, lon: 120 }, { lat: 30, lon: 122 },
      { lat: 32, lon: 118 }, { lat: 32, lon: 120 }, { lat: 32, lon: 122 },
      { lat: 28, lon: 118 }, { lat: 28, lon: 120 }, { lat: 28, lon: 122 },
    ]},
    { name: 'Beijing', tiles: [{ lat: 38, lon: 116 }, { lat: 40, lon: 116 }] },
    { name: 'Guangdong', tiles: [
      { lat: 22, lon: 112 }, { lat: 22, lon: 114 },
      { lat: 24, lon: 112 }, { lat: 24, lon: 114 },
    ]},
  ]

  function tileKey(lat, lon) { return `${lat},${lon}` }

  function isDownloaded(lat, lon) {
    return downloaded.some(t => t.lat === lat && t.lon === lon)
  }

  function isSelected(lat, lon) {
    return selected.some(t => t.lat === lat && t.lon === lon)
  }

  function isSelectedForDelete(lat, lon) {
    return selectedForDelete.some(t => t.lat === lat && t.lon === lon)
  }

  function toggleTile(lat, lon) {
    if (progress.active) return
    if (isDownloaded(lat, lon)) {
      // Toggle delete selection for downloaded tiles
      const idx = selectedForDelete.findIndex(t => t.lat === lat && t.lon === lon)
      if (idx >= 0) {
        selectedForDelete = selectedForDelete.filter((_, i) => i !== idx)
      } else {
        selectedForDelete = [...selectedForDelete, { lat, lon }]
      }
    } else {
      // Toggle download selection for empty tiles
      const idx = selected.findIndex(t => t.lat === lat && t.lon === lon)
      if (idx >= 0) {
        selected = selected.filter((_, i) => i !== idx)
      } else {
        selected = [...selected, { lat, lon }]
      }
    }
    updateGrid()
  }

  function applyPreset(preset) {
    if (progress.active) return
    // Add preset tiles that aren't already downloaded or selected
    const newTiles = preset.tiles.filter(t => !isDownloaded(t.lat, t.lon) && !isSelected(t.lat, t.lon))
    selected = [...selected, ...newTiles]
    updateGrid()
  }

  function clearSelection() {
    selected = []
    selectedForDelete = []
    updateGrid()
  }

  const TILE_WEIGHT = 1

  function tileStyle(lat, lon) {
    if (isSelectedForDelete(lat, lon)) return { color: '#ef4444', weight: TILE_WEIGHT, fillOpacity: 0.28 }
    if (isDownloaded(lat, lon)) return { color: '#22c55e', weight: TILE_WEIGHT, fillOpacity: 0.14 }
    if (isSelected(lat, lon)) return { color: '#3b82f6', weight: TILE_WEIGHT, fillOpacity: 0.20 }
    return { color: '#3a4254', weight: TILE_WEIGHT, fillOpacity: 0.01 }
  }

  function highlightTileOnMap(lat, lon) {
    highlightedTile = { lat, lon }
    const key = tileKey(lat, lon)
    const layer = gridLayerMap[key]
    if (layer) {
      layer.setStyle({ color: '#ef4444', weight: TILE_WEIGHT, fillColor: '#ef4444', fillOpacity: 0.35 })
      layer.bringToFront()
    }
  }

  function clearHighlight() {
    if (!highlightedTile) return
    const key = tileKey(highlightedTile.lat, highlightedTile.lon)
    const layer = gridLayerMap[key]
    if (layer) {
      const style = tileStyle(highlightedTile.lat, highlightedTile.lon)
      layer.setStyle({ color: style.color, weight: style.weight, fillColor: style.color, fillOpacity: style.fillOpacity })
    }
    highlightedTile = null
  }

  function updateGrid() {
    if (!L || !map) return

    // Remove old grid layers
    for (const layer of gridLayers) map.removeLayer(layer)
    gridLayers = []
    gridLayerMap = {}

    for (let lat = LAT_MIN; lat < LAT_MAX; lat += GRID_STEP) {
      for (let lon = LON_MIN; lon < LON_MAX; lon += GRID_STEP) {
        // Transform grid corners for tile alignment
        const sw = tileConfig.gcj02 ? wgs84ToGcj02(lat, lon) : [lat, lon]
        const ne = tileConfig.gcj02 ? wgs84ToGcj02(lat + GRID_STEP, lon + GRID_STEP) : [lat + GRID_STEP, lon + GRID_STEP]
        const bounds = [sw, ne]
        const style = tileStyle(lat, lon)

        const rect = L.rectangle(bounds, {
          color: style.color,
          weight: style.weight,
          fillColor: style.color,
          fillOpacity: style.fillOpacity,
          className: 'tile-rect',
        })

        rect.on('click', () => {
          toggleTile(lat, lon)
        })

        // Tooltip
        const dl = isDownloaded(lat, lon)
        const size = dl ? downloaded.find(t => t.lat === lat && t.lon === lon)?.size_mb : null
        const name = tileName(lat, lon)
        let label = name ? `${name}` : `${lat}, ${lon}`
        if (dl) label += ` — ${size ?? '?'} MB`
        else if (isSelected(lat, lon)) label += ' — selected'
        rect.bindTooltip(label, { sticky: true, className: 'tile-tooltip' })

        rect.addTo(map)
        gridLayers.push(rect)
        gridLayerMap[tileKey(lat, lon)] = rect
      }
    }

    // Bring selected tiles to front so borders aren't overlapped by neighbors
    for (const t of selected) {
      gridLayerMap[tileKey(t.lat, t.lon)]?.bringToFront()
    }
    for (const t of selectedForDelete) {
      gridLayerMap[tileKey(t.lat, t.lon)]?.bringToFront()
    }
  }

  async function refresh() {
    try {
      const data = await fetchTileList()
      downloaded = data.tiles
      storage = data.storage
      updateGrid()
    } catch (e) {
      error = e.message
    }
  }

  async function startDownload() {
    if (selected.length === 0 || progress.active) return
    error = null
    try {
      await startTileDownload(selected)
      startPolling()
    } catch (e) {
      error = e.message
    }
  }

  // Re-downloading every tile is a large transfer, so state the size and make
  // the user confirm or abort rather than firing on a single click.
  let confirmRefresh = $state(false)

  function armRefresh() {
    if (downloaded.length === 0 || progress.active) return
    confirmRefresh = true
  }

  async function refreshDownloaded() {
    confirmRefresh = false
    if (downloaded.length === 0 || progress.active) return
    error = null
    try {
      // download_tile always fetches and overwrites, so this repairs stale or
      // corrupt tiles in place — no need to delete them first.
      await startTileDownload(downloaded.map(t => ({ lat: t.lat, lon: t.lon })))
      startPolling()
    } catch (e) {
      error = e.message
    }
  }

  async function cancelDownload() {
    try {
      await cancelTileDownload()
    } catch (e) {
      error = e.message
    }
  }

  async function handleDelete(lat, lon) {
    try {
      await deleteTile(lat, lon)
      await refresh()
    } catch (e) {
      error = e.message
    }
  }

  async function handleDeleteSelected() {
    if (selectedForDelete.length === 0) return
    error = null
    try {
      for (const t of selectedForDelete) {
        await deleteTile(t.lat, t.lon)
      }
      selectedForDelete = []
      await refresh()
    } catch (e) {
      error = e.message
    }
  }

  function startPolling() {
    stopPolling()
    pollTimer = setInterval(async () => {
      try {
        progress = await fetchTileProgress()
        if (!progress.active) {
          stopPolling()
          await refresh()
          // Remove successfully downloaded tiles from selection (after refresh updates downloaded list)
          selected = selected.filter(t => !isDownloaded(t.lat, t.lon))
          updateGrid()
        }
      } catch (e) {
        console.warn('poll error:', e)
      }
    }, 1000)
  }

  function stopPolling() {
    if (pollTimer) {
      clearInterval(pollTimer)
      pollTimer = null
    }
  }

  onMount(async () => {
    // Dynamic Leaflet import
    const leaflet = await import('leaflet')
    L = leaflet.default || leaflet

    await import('leaflet/dist/leaflet.css')

    if (!mapContainer) return

    // Center on China for AMap, wider view for CartoDB
    const center = tileConfig.gcj02 ? wgs84ToGcj02(32, 118) : [32, 118]
    map = L.map(mapContainer, {
      zoomControl: true,
      attributionControl: false,
      center,
      zoom: 5,
    })

    L.tileLayer(tileConfig.url, {
      maxZoom: tileConfig.maxZoom,
      subdomains: tileConfig.subdomains,
      className: tileConfig.className || undefined,
    }).addTo(map)

    // Leaflet measures its container once, at init, but the surrounding flex
    // layout settles afterwards. Observe the container instead of guessing at
    // delays, so the map re-measures whenever it actually gets a size.
    resizeObserver = new ResizeObserver(() => map?.invalidateSize())
    resizeObserver.observe(mapContainer)

    try {
      const data = await fetchTileList()
      downloaded = data.tiles
      storage = data.storage

      // Check if download is active
      progress = await fetchTileProgress()
      if (progress.active) startPolling()
    } catch (e) {
      error = e.message
    }

    loading = false
    updateGrid()
  })

  onDestroy(() => {
    resizeObserver?.disconnect()
    stopPolling()
    if (map) {
      map.remove()
      map = null
    }
  })

  const progressPct = $derived(
    progress.total > 0 ? Math.round((progress.done / progress.total) * 100) : 0
  )

  const currentTileLabel = $derived(
    progress.current
      ? tileLabel(...progress.current.split(',').map(Number))
      : '...'
  )

</script>

<style>
  :global(.amap-dark) {
    filter: invert(1) hue-rotate(180deg) brightness(0.95) contrast(1.1);
  }
  :global(.tile-rect) {
    cursor: pointer;
    transition: fill-opacity 0.15s;
  }
  :global(.tile-rect:hover) {
    fill-opacity: 0.5 !important;
  }
  :global(.tile-tooltip) {
    background: #161b28;
    color: #d1d5de;
    border: 1px solid #2a3040;
    font-size: 12px;
    padding: 3px 8px;
    border-radius: 4px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.4);
  }
  :global(.tile-tooltip::before) {
    border-top-color: #2a3040 !important;
  }
  .animate-pulse-border {
    animation: pulse-border 2.5s ease-in-out infinite;
  }
  @keyframes pulse-border {
    0%, 100% { border-color: rgba(100, 116, 139, 0.4); }
    50% { border-color: rgba(100, 116, 139, 1); }
  }
</style>

<div class="flex flex-col flex-1 min-h-0">
  <!-- Map fills all available space -->
  <div class="flex-1 min-h-0 flex flex-col px-2 sm:px-4 pt-2 sm:pt-4">
    <div
      bind:this={mapContainer}
      class="w-full flex-1 min-h-0 rounded-lg overflow-hidden"
    ></div>
  </div>

  <!-- Controls pinned to bottom -->
  <div class="shrink-0 sticky bottom-0 z-[1000] bg-surface-900/95 backdrop-blur px-2 sm:px-4 py-2 space-y-2">
    {#if error}
      <div class="text-engage-red text-xs">{error}</div>
    {/if}
    {#if progress.error}
      <div class="text-engage-red text-xs p-1.5 rounded bg-engage-red/10">{progress.error}</div>
    {/if}

    {#if progress.active}
      <!-- Download progress -->
      <div class="flex items-center gap-3 min-h-[34px]">
        <div class="flex-1 min-w-0">
          <div class="flex items-center justify-between text-xs text-surface-300 mb-1">
            <span class="truncate">{currentTileLabel}</span>
            <span class="shrink-0 ml-2">{progressPct}%</span>
          </div>
          <div class="w-full h-1.5 rounded-full bg-surface-700 overflow-hidden">
            <div
              class="h-full rounded-full bg-engage-blue transition-all duration-300"
              style="width: {progressPct}%"
            ></div>
          </div>
        </div>
        <button
          class="shrink-0 px-3 py-1.5 text-xs rounded-lg bg-engage-red/20 text-engage-red hover:bg-engage-red/30 transition-colors"
          onclick={cancelDownload}
        >Cancel</button>
      </div>
    {:else}
      <!-- Action bar -->
      <div class="flex items-center gap-2 min-h-[34px]">
        {#if confirmRefresh}
          <div class="flex-1 flex items-center justify-center gap-2">
            <span class="text-xs text-surface-200">
              Re-download {downloaded.length} tile{downloaded.length === 1 ? '' : 's'} — {storage.total_mb} MB?
            </span>
            <button
              class="px-3 py-1.5 text-xs rounded-lg bg-engage-blue text-white hover:bg-engage-blue/80 transition-colors"
              onclick={refreshDownloaded}
            >Confirm</button>
            <button
              class="px-2 py-1.5 text-xs text-surface-500 hover:text-surface-300 transition-colors"
              onclick={() => confirmRefresh = false}
            >Abort</button>
          </div>
        {:else if selectedForDelete.length > 0}
          <div class="flex-1 flex items-center justify-center gap-2">
            <button
              class="px-3 py-1.5 text-xs rounded-lg bg-engage-red/20 text-engage-red hover:bg-engage-red/30 transition-colors"
              onclick={handleDeleteSelected}
            >
              Delete {selectedForDelete.length} selected
            </button>
            <button
              class="px-2 py-1.5 text-xs text-surface-500 hover:text-surface-300 transition-colors"
              onclick={() => { selectedForDelete = []; updateGrid() }}
            >Clear</button>
          </div>
        {:else if selected.length > 0}
          <div class="flex-1 flex items-center justify-center gap-2">
            <button
              class="px-3 py-1.5 text-xs rounded-lg bg-engage-blue text-white hover:bg-engage-blue/80 transition-colors"
              onclick={startDownload}
            >
              Download {selected.length} selected
            </button>
            <button
              class="px-2 py-1.5 text-xs text-surface-500 hover:text-surface-300 transition-colors"
              onclick={clearSelection}
            >Clear</button>
          </div>
        {:else}
          <div class="flex-1 flex justify-center">
            <span class="text-sm text-surface-200 px-4 py-1.5 rounded-lg border border-dashed border-surface-500 animate-pulse-border">Click map to select tiles</span>
          </div>
        {/if}

        {#if downloaded.length > 0 && !confirmRefresh}
          <button
            class="shrink-0 px-3 py-1.5 text-xs rounded-lg bg-engage-blue text-white hover:bg-engage-blue/80 transition-colors"
            onclick={armRefresh}
            title="Re-download all {downloaded.length} tiles ({storage.total_mb} MB)"
          >
            Refresh
          </button>
        {/if}

      </div>
    {/if}
  </div>
</div>
