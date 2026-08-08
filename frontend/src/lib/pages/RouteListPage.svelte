<script>
  import { onMount } from 'svelte'
  import { dongleId, storageInfo } from '../stores.js'
  import { fetchRoutes, fetchStorage, fetchHealth, scanRoute } from '../api.js'
  import { formatBytes } from '../format.js'
  import RouteCard from '../components/RouteCard.svelte'

  let routes = $state([])
  let loading = $state(true)
  let loadingMore = $state(false)
  let hasMore = $state(false)
  let sentinel = $state(null)
  let storage = $state(null)
  let health = $state(null)
  let error = $state(null)
  let activeTab = $state('recent')
  let dateFrom = $state('')
  let dateTo = $state('')

  // Index into the active tab's DATE_LADDER, and whether the user has set the
  // range themselves — an explicit range is never auto-widened.
  let rung = $state(0)
  let datesTouched = $state(false)

  // Bumped on every tab/date change so a late in-flight fetch can be discarded
  // instead of appending stale rows onto the freshly reset list.
  let requestToken = 0

  // Server rows consumed so far. Tracked separately from routes.length because
  // a de-duplicated row still advances the server-side offset.
  let nextOffset = 0

  const CHUNK_SIZE = 50
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

  // Effectively unbounded — predates any comma 3 footage.
  const EPOCH = '2020-01-01'

  // Widening ladder of `from` bounds per tab. Rung 0 is the tab's opening
  // window; when infinite scroll runs that window dry we step to the next rung
  // and keep loading, so the default filter never caps how far back you scroll.
  const DATE_LADDER = {
    recent:   [() => daysAgo(30),  () => monthsAgo(6), () => EPOCH],
    saved:    [() => EPOCH],
    all:      [() => monthsAgo(6), () => EPOCH],
    recycled: [() => EPOCH],
  }

  const TAB_DEFAULTS = {
    recent:   { from: () => DATE_LADDER.recent[0](),   to: () => todayStr() },
    saved:    { from: () => DATE_LADDER.saved[0](),    to: () => todayStr() },
    all:      { from: () => DATE_LADDER.all[0](),      to: () => todayStr() },
    recycled: { from: () => DATE_LADDER.recycled[0](), to: () => todayStr() },
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

  // Over-fetch by one row so we can tell whether more rows exist without a
  // second request or a response-shape change.
  function listOpts(offset) {
    const opts = { limit: CHUNK_SIZE + 1, offset, filter: activeTab }
    const afterGps = dateToEpoch(dateFrom)
    const beforeGps = dateToEpoch(dateTo, true)
    if (afterGps) opts.afterGps = afterGps
    if (beforeGps) opts.beforeGps = beforeGps
    return opts
  }

  async function loadRoutes(id) {
    loading = true
    error = null
    const token = requestToken
    try {
      const [data, st, hl] = await Promise.all([
        fetchRoutes(id, listOpts(0)),
        fetchStorage(),
        fetchHealth(),
      ])
      if (token !== requestToken) return
      hasMore = data.length > CHUNK_SIZE
      routes = data.slice(0, CHUNK_SIZE)
      nextOffset = routes.length
      storage = st
      health = hl
      storageInfo.set(st)
      loading = false
      if (activeTab === 'recent' || activeTab === 'all') {
        scanPendingRoutes(routes)
      }
      // The opening window may hold less than a full chunk, in which case no
      // sentinel renders and nothing would ever trigger a widen — so kick it
      // off here rather than leaving the first screen capped.
      if (!hasMore) loadMore(true)
    } catch (e) {
      if (token !== requestToken) return
      error = e.message
      loading = false
    }
  }

  // Step to the next-wider date window. Returns false when the ladder is spent
  // or the user set the range themselves.
  function widenWindow() {
    const ladder = DATE_LADDER[activeTab]
    if (datesTouched || rung >= ladder.length - 1) return false
    rung++
    dateFrom = ladder[rung]()
    return true
  }

  // Pull the next chunk. A chunk that comes back short means the current date
  // window ran dry, not that the list is over — widen and keep pulling until a
  // chunk fills or the ladder is spent. Widening only ever adds *older* routes,
  // which sort to the tail, so the loaded prefix is stable and `routes.length`
  // stays a valid offset across a widen.
  async function loadMore(windowExhausted = false) {
    if (loading || loadingMore || !$dongleId) return
    loadingMore = true
    const token = requestToken
    let exhausted = windowExhausted
    try {
      while (true) {
        if (exhausted && !widenWindow()) {
          hasMore = false
          return
        }
        const data = await fetchRoutes($dongleId, listOpts(nextOffset))
        if (token !== requestToken) return
        const chunk = data.slice(0, CHUNK_SIZE)
        nextOffset += chunk.length
        // Pending placeholders ignore the date filter, so an old-counter pending
        // row can sit at the tail of the narrow window and mid-list in the wider
        // one — breaking the prefix assumption above. Drop rows we already hold
        // rather than throwing on a duplicate key in the keyed {#each}.
        const seen = new Set(routes.map(r => r.local_id))
        const fresh = chunk.filter(r => !seen.has(r.local_id))
        if (fresh.length) {
          routes = [...routes, ...fresh]
          if (activeTab === 'recent' || activeTab === 'all') {
            scanPendingRoutes(fresh)
          }
        }
        if (data.length > CHUNK_SIZE) {
          hasMore = true
          return
        }
        exhausted = true
      }
    } catch (e) {
      // Leave hasMore set. IntersectionObserver only fires on transitions, so
      // this retries when the user scrolls away and back rather than looping.
      console.error('loadMore error:', e)
    } finally {
      loadingMore = false
    }
  }

  // Start the next chunk slightly before the bottom of the list is reached.
  $effect(() => {
    if (!sentinel) return
    const io = new IntersectionObserver(
      (entries) => { if (entries[0].isIntersecting) loadMore() },
      { rootMargin: '400px' },
    )
    io.observe(sentinel)
    return () => io.disconnect()
  })

  function resetList() {
    requestToken++
    hasMore = false
    loadingMore = false
    rung = 0
    datesTouched = false
  }

  function switchTab(tabId) {
    if (tabId === activeTab) return
    activeTab = tabId
    loading = true
    routes = []
    resetList()
    applyTabDefaults(tabId)
    if ($dongleId) loadRoutes($dongleId)
  }

  function resetDates() {
    resetList()
    applyTabDefaults(activeTab)
    if ($dongleId) loadRoutes($dongleId)
  }

  function onDateChange() {
    resetList()
    datesTouched = true
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

{#snippet skeletonCard()}
  <div class="card w-full animate-pulse">
    <div class="px-3 pt-2.5">
      <div class="h-3 w-40 bg-surface-700 rounded"></div>
    </div>
    <div class="px-3 py-2.5 flex gap-3">
      <div class="h-4 w-32 bg-surface-700 rounded"></div>
      <div class="h-4 w-20 bg-surface-700 rounded ml-auto"></div>
    </div>
  </div>
{/snippet}

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
        {@render skeletonCard()}
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

    <!-- Infinite scroll: the sentinel pulls in the next chunk before the bottom -->
    {#if hasMore}
      <div bind:this={sentinel} aria-hidden="true"></div>
    {/if}
    {#if loadingMore}
      {#each Array(2) as _}
        {@render skeletonCard()}
      {/each}
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
