<script>
  import { selectedRoute } from '../stores.js'
  import { spriteUrl } from '../api.js'
  import { formatDate, formatTimeRange, formatDuration, formatDistance, getRouteDurationMs } from '../format.js'
  import EventTimeline from './EventTimeline.svelte'

  /** @type {{ route: object }} */
  let { route } = $props()

  const durationMin = $derived(
    route.maxqlog != null ? route.maxqlog + 1 : null
  )
  const durationMs = $derived(getRouteDurationMs(route))

  const hasEnrichedData = $derived(route.engagement_pct != null)
  const timelineEvents = $derived(route.timeline ?? [])
  const isRecycled = $derived(!!route.recycled_reason)

  const RECYCLE_TTL_DAYS = 7
  const daysLeft = $derived(() => {
    if (!route.hidden_at) return null
    const elapsed = (Date.now() / 1000 - route.hidden_at) / 86400
    return Math.max(0, Math.ceil(RECYCLE_TTL_DAYS - elapsed))
  })

  const FILMSTRIP_COUNT = 8
  const totalSegs = $derived(route.maxqlog != null ? route.maxqlog + 1 : 0)
  const filmstripSlots = $derived(() => {
    if (!hasEnrichedData || !route.url || totalSegs <= 0) return []
    const dur = totalSegs * 60
    const step = dur / FILMSTRIP_COUNT
    return Array.from({ length: FILMSTRIP_COUNT }, (_, i) => {
      const t = i * step + step / 2
      const seg = Math.min(Math.floor(t / 60), totalSegs - 1)
      const secInSeg = Math.floor(t % 60)
      return { seg, t: secInSeg }
    })
  })

  function handleClick() {
    selectedRoute.set(route.local_id)
  }
</script>

{#if route.pending}
  <div class="card w-full overflow-hidden">
    <div class="px-3 py-3 flex items-center gap-3">
      <div class="animate-spin w-4 h-4 border-2 border-surface-400 border-t-transparent rounded-full flex-shrink-0"></div>
      <span class="text-sm text-surface-400">Scanning route...</span>
      <span class="text-xs text-surface-600 ml-auto">{route.seg_count} segments</span>
    </div>
  </div>
{:else if isRecycled}
  <div class="card w-full overflow-hidden opacity-50">
    {#if route.start_address}
      <div class="px-3 pt-2 text-xs text-surface-300 truncate">
        {route.start_address}{route.end_address && route.end_address !== route.start_address ? ` → ${route.end_address}` : ''}
      </div>
    {/if}
    <div class="px-3 py-2.5 flex items-center gap-3 flex-wrap text-sm">
      <div class="flex items-center gap-2 min-w-0">
        <span class="text-surface-100 font-medium truncate">
          {formatDate(route.create_time)} {formatTimeRange(route.start_time, route.end_time)}
        </span>
      </div>
      <div class="flex items-center gap-2 text-xs ml-auto">
        {#if durationMin != null}
          <span class="text-surface-400">{formatDuration(durationMin)}</span>
        {/if}
        {#if route.recycled_reason === 'deleted'}
          <span class="px-1.5 py-0.5 rounded text-[10px] font-medium bg-red-500/20 text-red-400">Deleted</span>
          {#if daysLeft() != null}
            <span class="px-1.5 py-0.5 rounded text-[10px] font-medium bg-surface-600/50 text-surface-400">{daysLeft()}d left</span>
          {/if}
        {:else}
          <span class="px-1.5 py-0.5 rounded text-[10px] font-medium bg-surface-600/50 text-surface-400">Invalid</span>
        {/if}
      </div>
    </div>
  </div>
{:else}
  <button
    class="card w-full text-left cursor-pointer hover:border-surface-500/50 transition-colors duration-150 overflow-hidden"
    onclick={handleClick}
  >
    <!-- Filmstrip timeline (enriched routes only) -->
    {#if filmstripSlots().length > 0}
      <div class="relative h-[44px] overflow-hidden rounded-t-lg">
        <div class="absolute inset-0 flex opacity-40">
          {#each filmstripSlots() as slot}
            <div class="flex-1 min-w-0 overflow-hidden bg-surface-800">
              <img
                src={spriteUrl(route, slot.seg, slot.t)}
                alt=""
                class="w-full h-full object-cover"
                loading="lazy"
                onerror={(e) => e.target.style.display = 'none'}
              />
            </div>
          {/each}
        </div>
        <div class="absolute bottom-0 left-0 right-0">
          <EventTimeline events={timelineEvents} {durationMs} height="3px" />
        </div>
      </div>
    {:else if hasEnrichedData}
      <div class="px-3 pt-2">
        <EventTimeline events={timelineEvents} {durationMs} height="4px" />
      </div>
    {/if}

    <!-- Address row -->
    {#if route.start_address}
      <div class="px-3 pt-2 text-xs text-surface-300 truncate">
        {route.start_address}{route.end_address && route.end_address !== route.start_address ? ` → ${route.end_address}` : ''}
      </div>
    {/if}

    <!-- Note preview -->
    {#if route.notes}
      <p class="px-3 pt-1 text-xs text-surface-500 truncate">{route.notes.split('\n')[0]}</p>
    {/if}

    <!-- Metadata row -->
    <div class="px-3 py-2.5 flex items-center gap-3 flex-wrap text-sm">
      <div class="flex items-center gap-2 min-w-0">
        <span class="text-surface-100 font-medium truncate">
          {formatDate(route.create_time)} {formatTimeRange(route.start_time, route.end_time)}
        </span>
      </div>

      <div class="flex items-center gap-2 text-xs text-surface-400 ml-auto">
        {#if durationMin != null}
          <span>{formatDuration(durationMin)}</span>
        {/if}
        {#if route.distance_m != null}
          <span class="text-surface-600">&middot;</span>
          <span>{formatDistance(route.distance_m)}</span>
        {/if}
        {#if route.engagement_pct != null}
          <span class="text-surface-600">&middot;</span>
          <span class="text-engage-green">{route.engagement_pct}%</span>
        {/if}
        {#if route.is_preserved}
          <svg class="w-4 h-4 text-engage-blue" fill="currentColor" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24" title="Preserved">
            <path d="M5 5a2 2 0 012-2h10a2 2 0 012 2v16l-7-3.5L5 21V5z"/>
          </svg>
        {/if}
      </div>
    </div>
  </button>
{/if}
