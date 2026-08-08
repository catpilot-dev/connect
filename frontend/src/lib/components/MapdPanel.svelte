<script>
  import { onMount } from 'svelte'
  import Spinner from './Spinner.svelte'
  import { fetchParams, mapdCheckUpdate, mapdUpdate, fetchTileList } from '../api.js'
  import { getTileSource, setTileSource as saveTileSource, TILE_SOURCES } from '../tileSource.js'

  let mapdVersion = $state(null)
  let mapdLatest = $state(null)
  let mapdReleaseDate = $state(null)
  let mapdChecking = $state(false)
  let mapdUpdating = $state(false)
  let mapdError = $state(null)
  let tileStorage = $state(null)
  let tileSource = $state(getTileSource())

  onMount(async () => {
    try {
      const params = await fetchParams()
      if (params.MapdVersion) mapdVersion = params.MapdVersion
    } catch { /* ignore */ }
    handleMapdCheck()
    fetchTileList().then(d => { tileStorage = d.storage }).catch(() => {})
  })

  async function handleMapdCheck() {
    mapdChecking = true
    mapdError = null
    try {
      const res = await mapdCheckUpdate()
      if (res.error) {
        mapdError = res.error
      } else {
        mapdVersion = res.current
        mapdLatest = res.latest
        if (res.release_date) mapdReleaseDate = res.release_date
      }
    } catch (e) {
      mapdError = e.message
    } finally {
      mapdChecking = false
    }
  }

  async function handleMapdUpdate() {
    if (!confirm(`Update mapd from ${mapdVersion} to ${mapdLatest}?`)) return
    mapdUpdating = true
    mapdError = null
    try {
      const res = await mapdUpdate()
      if (res.error) {
        mapdError = res.error
      } else {
        mapdVersion = res.version || mapdLatest
        mapdLatest = mapdVersion
      }
    } catch (e) {
      mapdError = e.message
    } finally {
      mapdUpdating = false
    }
  }
</script>

<div class="space-y-4">
  <div class="flex items-center justify-between gap-3">
    <div class="min-w-0">
      <div class="text-sm text-surface-100">
        mapd {mapdVersion || '...'}
        {#if mapdReleaseDate}
          <span class="text-surface-500 text-xs">({mapdReleaseDate})</span>
        {/if}
        {#if mapdLatest && mapdLatest !== mapdVersion}
          <span class="text-surface-500">&rarr;</span>
          <span class="text-engage-green">{mapdLatest}</span>
        {/if}
      </div>
      {#if mapdError}
        <div class="text-xs text-engage-red mt-0.5">{mapdError}</div>
      {/if}
    </div>
    <div class="shrink-0">
      {#if mapdUpdating}
        <div class="flex items-center gap-2 text-xs text-surface-400">
          <Spinner />
          Installing...
        </div>
      {:else if mapdChecking}
        <div class="flex items-center gap-2 text-xs text-surface-400">
          <Spinner />
          Checking
        </div>
      {:else if mapdLatest && mapdLatest !== mapdVersion}
        <button
          class="text-xs px-3 py-1.5 rounded-lg bg-engage-green/15 text-engage-green hover:bg-engage-green/25 transition-colors"
          onclick={handleMapdUpdate}
        >
          Install Update
        </button>
      {:else if mapdLatest}
        <span class="text-xs px-3 py-1.5 text-surface-500">Up to Date</span>
      {:else}
        <button
          class="text-xs px-3 py-1.5 rounded-lg bg-surface-700 text-surface-300 hover:bg-surface-600 transition-colors"
          onclick={handleMapdCheck}
        >Retry</button>
      {/if}
    </div>
  </div>
  <div class="pt-3">
    <button class="w-full flex items-center justify-between group" onclick={() => { window.location.href = '/tiles' }}>
      <div class="text-left">
        <div class="text-sm text-surface-100">Map Tiles Management</div>
        <div class="text-xs text-surface-500 mt-0.5">{#if tileStorage}{tileStorage.tile_count} tiles &middot; {tileStorage.total_mb} MB{:else}Download and manage OSM offline tiles{/if}</div>
      </div>
      <svg class="w-5 h-5 text-surface-500 group-hover:text-surface-300 transition-colors" viewBox="0 0 20 20" fill="currentColor">
        <path fill-rule="evenodd" d="M7.21 14.77a.75.75 0 01.02-1.06L11.168 10 7.23 6.29a.75.75 0 111.04-1.08l4.5 4.25a.75.75 0 010 1.08l-4.5 4.25a.75.75 0 01-1.06-.02z" clip-rule="evenodd" />
      </svg>
    </button>
  </div>
  <div class="pt-3 flex items-center gap-2">
    <div class="text-sm text-surface-100">Tile Source</div>
    <div class="flex-1"></div>
    {#each Object.entries(TILE_SOURCES) as [key, src]}
      <button
        class="px-2.5 py-1 text-xs rounded-full transition-colors {tileSource === key ? 'bg-engage-blue/20 text-engage-blue border border-engage-blue/40' : 'bg-surface-700 text-surface-300 border border-surface-600 hover:border-surface-500'}"
        onclick={() => { tileSource = key; saveTileSource(key) }}
      >
        {src.label}
      </button>
    {/each}
  </div>
</div>
