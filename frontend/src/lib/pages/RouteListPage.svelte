<script>
  import { onMount } from 'svelte'
  import { dongleId, storageInfo } from '../stores.js'
  import { fetchRoutes, fetchStorage, fetchHealth, scanRoute } from '../api.js'
  import { formatBytes } from '../format.js'
  import RouteCard from '../components/RouteCard.svelte'

  let routes = $state([])
  let loading = $state(true)
  let page = $state(0)
  let hasNext = $state(false)
  let storage = $state(null)
  let health = $state(null)
  let error = $state(null)
  let activeTab = $state('recent')
  let dateFrom = $state('')
  let dateTo = $state('')

  const PAGE_SIZE = 20
  const TABS = [
    { id: 'recent', label: 'Recent' },
    { id: 'saved', label: 'Saved' },
    { id: 'all', label: 'Stored' },
    { id: 'recycled', label: 'Recycled' },
  ]

  function todayStr() {
    return new Date().toISOString().slice(0, 10)
  }
  function daysAgo(n) {
    const d = new Date()
    d.setDate(d.getDate() - n)
    return d.toISOString().slice(0, 10)
  }
  function monthsAgo(n) {
    const d = new Date()
    d.setMonth(d.getMonth() - n)
    return d.toISOString().slice(0, 10)
  }

  const TAB_DEFAULTS = {
    recent:   { from: () => daysAgo(30),  to: () => todayStr() },
    saved:    { from: () => '2020-01-01', to: () => todayStr() },
    all:      { from: () => monthsAgo(6), to: () => todayStr() },
    recycled: { from: () => '2020-01-01', to: () => todayStr() },
  }

  function applyTabDefaults(tabId) {
    const def = TAB_DEFAULTS[tabId]
    dateFrom = def.from()
    dateTo = def.to()
  }

  // Set initial defaults for recent tab
  applyTabDefaults('recent')

  async function scanPendingRoutes(routeList) {
    const pending = routeList.filter(r => r.pending)
    for (const pr of pending) {
      try {
        const scanned = await scanRoute(pr.local_id)
        routes = routes.map(r => r.local_id === pr.local_id ? scanned : r)
      } catch {
        routes = routes.filter(r => r.local_id !== pr.local_id)
      }
    }
  }

  function dateToEpoch(dateStr, endOfDay = false) {
    if (!dateStr) return null
    const d = new Date(dateStr + (endOfDay ? 'T23:59:59' : 'T00:00:00'))
    return d.getTime() / 1000
  }

  async function loadRoutes(id) {
    loading = true
    error = null
    try {
      // Over-fetch by one row so we can tell whether a next page exists
      // without a second request or a response-shape change.
      const opts = { limit: PAGE_SIZE + 1, offset: page * PAGE_SIZE, filter: activeTab }
      const afterGps = dateToEpoch(dateFrom)
      const beforeGps = dateToEpoch(dateTo, true)
      if (afterGps) opts.afterGps = afterGps
      if (beforeGps) opts.beforeGps = beforeGps

      const [data, st, hl] = await Promise.all([
        fetchRoutes(id, opts),
        fetchStorage(),
        fetchHealth(),
      ])
      hasNext = data.length > PAGE_SIZE
      routes = data.slice(0, PAGE_SIZE)
      storage = st
      health = hl
      storageInfo.set(st)
      loading = false
      if (activeTab === 'recent' || activeTab === 'all') {
        scanPendingRoutes(routes)
      }
    } catch (e) {
      error = e.message
      loading = false
    }
  }

  function goToPage(next) {
    if (!$dongleId) return
    page = next
    window.scrollTo({ top: 0 })
    loadRoutes($dongleId)
  }

  function switchTab(tabId) {
    if (tabId === activeTab) return
    activeTab = tabId
    loading = true
    routes = []
    page = 0
    hasNext = false
    applyTabDefaults(tabId)
    if ($dongleId) loadRoutes($dongleId)
  }

  function resetDates() {
    page = 0
    applyTabDefaults(activeTab)
    if ($dongleId) loadRoutes($dongleId)
  }

  function onDateChange() {
    page = 0
    if ($dongleId) loadRoutes($dongleId)
  }

  onMount(() => {
    const unsub = dongleId.subscribe((id) => {
      if (id) loadRoutes(id)
    })
    return unsub
  })

  const datesModified = $derived(
    dateFrom !== TAB_DEFAULTS[activeTab].from() || dateTo !== TAB_DEFAULTS[activeTab].to()
  )
  const healthIssues = $derived(
    health?.checks?.filter(c => c.level === 'error' || c.level === 'warn') ?? []
  )
  const usedPct = $derived(storage ? 100 - storage.percent_free : 0)
  const storedColor = $derived(
    usedPct >= 80 ? '!text-red-400' :
    usedPct >= 60 ? '!text-amber-400' :
    '!text-green-400'
  )
  const storedBg = $derived(
    usedPct >= 80 ? 'bg-red-500/15' :
    usedPct >= 60 ? 'bg-amber-500/15' :
    'bg-green-500/15'
  )
  const storedColorDim = $derived(
    usedPct >= 80 ? '!text-red-400/60' :
    usedPct >= 60 ? '!text-amber-400/60' :
    '!text-green-400/60'
  )
  const storedColorHover = $derived(
    usedPct >= 80 ? 'hover:!text-red-400' :
    usedPct >= 60 ? 'hover:!text-amber-400' :
    'hover:!text-green-400'
  )
  const storageTooltip = $derived(
    storage
      ? `Storage: ${formatBytes(storage.total - storage.free)} / ${formatBytes(storage.total)} (${Math.round(usedPct)}% used)`
      : ''
  )
</script>

<div class="mx-auto w-full max-w-3xl px-4 py-4 space-y-3">
  <!-- Storage warning banner -->
  {#if storage && usedPct >= 80}
    <div
      class="rounded-lg px-4 py-3 text-sm flex items-center gap-2 bg-red-500/10 text-red-400"
    >
      <svg class="w-4 h-4 flex-shrink-0" fill="currentColor" viewBox="0 0 20 20">
        <path fill-rule="evenodd" d="M8.485 2.495c.673-1.167 2.357-1.167 3.03 0l6.28 10.875c.673 1.167-.168 2.625-1.516 2.625H3.72c-1.347 0-2.189-1.458-1.515-2.625L8.485 2.495zM10 6a.75.75 0 01.75.75v3.5a.75.75 0 01-1.5 0v-3.5A.75.75 0 0110 6zm0 9a1 1 0 100-2 1 1 0 000 2z" clip-rule="evenodd"/>
      </svg>
      <span>
        Storage low: {formatBytes(storage.free)} free of {formatBytes(storage.total)}
      </span>
    </div>
  {/if}

  <!-- Health check warnings -->
  {#if healthIssues.length > 0}
    <div class="rounded-lg px-4 py-3 text-sm space-y-1
      {health.errors > 0 ? 'bg-red-500/10 text-red-400' : 'bg-amber-500/10 text-amber-400'}">
      <div class="flex items-center gap-2 font-medium">
        <svg class="w-4 h-4 flex-shrink-0" fill="currentColor" viewBox="0 0 20 20">
          <path fill-rule="evenodd" d="M8.485 2.495c.673-1.167 2.357-1.167 3.03 0l6.28 10.875c.673 1.167-.168 2.625-1.516 2.625H3.72c-1.347 0-2.189-1.458-1.515-2.625L8.485 2.495zM10 6a.75.75 0 01.75.75v3.5a.75.75 0 01-1.5 0v-3.5A.75.75 0 0110 6zm0 9a1 1 0 100-2 1 1 0 000 2z" clip-rule="evenodd"/>
        </svg>
        <span>System: {health.errors} error{health.errors !== 1 ? 's' : ''}, {health.warnings} warning{health.warnings !== 1 ? 's' : ''}</span>
      </div>
      {#each healthIssues as issue}
        <div class="ml-6 text-xs opacity-80">
          <span class="font-mono">{issue.name}</span>: {issue.message}
        </div>
      {/each}
    </div>
  {/if}

  <!-- Tabs + date filter -->
  <div class="flex flex-wrap items-center gap-2">
    <!-- Tabs (left) -->
    <div class="flex gap-1 rounded-lg bg-surface-800/50 p-1">
      {#each TABS as tab}
        <button
          class="px-3 py-1.5 rounded-md text-sm font-medium transition-colors duration-150
            {tab.id === 'recycled'
              ? (activeTab === 'recycled' ? 'bg-red-500/15 !text-red-400' : '!text-red-400/60 hover:!text-red-400')
              : tab.id === 'saved'
                ? (activeTab === 'saved' ? 'bg-blue-500/15 !text-blue-400' : '!text-blue-400/60 hover:!text-blue-400')
                : tab.id === 'all' && storage
                  ? (activeTab === 'all' ? `${storedBg} ${storedColor}` : `${storedColorDim} ${storedColorHover}`)
                  : (activeTab === tab.id ? 'bg-surface-700 text-surface-100' : 'text-surface-400 hover:text-surface-200')}"
          onclick={() => switchTab(tab.id)}
          title={tab.id === 'all' && storage ? storageTooltip : ''}
        >
          {tab.label}
        </button>
      {/each}
    </div>

    <!-- Date filter (right, wraps below on small screens) -->
    <div class="flex items-center gap-1.5 sm:ml-auto text-sm text-surface-400">
      <input
        type="date"
        bind:value={dateFrom}
        onchange={onDateChange}
        class="date-input bg-surface-800 border border-surface-600 rounded px-1.5 py-1 text-surface-400 text-xs"
      />
      <span class="text-surface-500">-</span>
      <input
        type="date"
        bind:value={dateTo}
        onchange={onDateChange}
        class="date-input bg-surface-800 border border-surface-600 rounded px-1.5 py-1 text-surface-400 text-xs"
      />
      {#if datesModified}
        <button class="text-xs text-surface-500 hover:text-surface-200" onclick={resetDates}>Reset</button>
      {/if}
    </div>
  </div>

  <!-- Storage bar (Stored tab only) -->
  {#if activeTab === 'all' && storage}
    <div class="flex items-center gap-2">
      <div class="flex-1 h-1.5 rounded-full bg-surface-700 overflow-hidden">
        <div
          class="h-full rounded-full transition-all duration-300
            {usedPct >= 80 ? 'bg-red-500' : usedPct >= 60 ? 'bg-amber-400' : 'bg-green-500'}"
          style="width: {usedPct}%"
        ></div>
      </div>
      <span class="text-xs text-surface-500 whitespace-nowrap">{formatBytes(storage.total - storage.free)} / {formatBytes(storage.total)}</span>
    </div>
  {/if}

  <!-- Loading state -->
  {#if loading}
    <div class="space-y-3">
      {#each Array(5) as _}
        <div class="card w-full animate-pulse">
          <div class="px-3 pt-2.5">
            <div class="h-3 w-40 bg-surface-700 rounded"></div>
          </div>
          <div class="px-3 py-2.5 flex gap-3">
            <div class="h-4 w-32 bg-surface-700 rounded"></div>
            <div class="h-4 w-20 bg-surface-700 rounded ml-auto"></div>
          </div>
        </div>
      {/each}
    </div>
  {:else if error}
    <div class="flex items-center justify-center h-48">
      <p class="text-surface-400">{error}</p>
    </div>
  {:else if routes.length === 0}
    <div class="flex items-center justify-center h-48">
      <p class="text-surface-400">
        {#if activeTab === 'recent'}No recent drives
        {:else if activeTab === 'saved'}No saved routes
        {:else if activeTab === 'recycled'}No recycled routes
        {:else}No routes found
        {/if}
      </p>
    </div>
  {:else}
    {#each routes as route (route.local_id)}
      <RouteCard {route} />
    {/each}

    <!-- Previous / Next pager -->
    {#if page > 0 || hasNext}
      <div class="flex items-center justify-center gap-3 py-4">
        <button
          class="btn-ghost"
          onclick={() => goToPage(page - 1)}
          disabled={page === 0}
        >
          ‹ Previous
        </button>
        <span class="text-sm text-surface-500 tabular-nums">Page {page + 1}</span>
        <button
          class="btn-ghost"
          onclick={() => goToPage(page + 1)}
          disabled={!hasNext}
        >
          Next ›
        </button>
      </div>
    {/if}
  {/if}
</div>

<style>
  .date-input {
    color-scheme: dark;
    position: relative;
  }
  .date-input::-webkit-calendar-picker-indicator {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    opacity: 0;
    cursor: pointer;
  }
</style>
