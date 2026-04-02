<script>
  import { spriteUrl } from '../api.js'
  import EventTimeline from './EventTimeline.svelte'

  /**
   * Filmstrip timeline scrubber with selection handles and zoom.
   * Extracted from VideoControls for flexible layout positioning.
   */

  let {
    route,
    currentTime = 0,
    duration = 0,
    events = [],
    durationMs = 0,
    onSeek,
    selectionStart = $bindable(0),
    selectionEnd = $bindable(0),
  } = $props()

  let timelineEl = $state(null)
  let isDragging = $state(false)
  let draggingHandle = $state(null) // 'start' | 'end' | null
  let dragOrigin = $state(null) // time where bar drag started
  let prevSel = { start: 0, end: 0 } // selection before bar drag
  let viewStart = $state(0)
  let viewEnd = $state(0)

  // Initialize view: zoom to selection if set from URL, otherwise full duration
  let viewInitialized = false
  $effect(() => {
    if (duration > 0 && selectionEnd === 0) {
      selectionEnd = duration
    }
    if (duration > 0 && !viewInitialized) {
      viewInitialized = true
      if (selectionStart > 0 || (selectionEnd > 0 && selectionEnd < duration)) {
        // Zoom to selection with padding
        const selDur = (selectionEnd || duration) - selectionStart
        const padding = selDur * 0.15
        viewStart = Math.max(0, selectionStart - padding)
        viewEnd = Math.min(duration, (selectionEnd || duration) + padding)
      } else {
        viewEnd = duration
      }
    }
  })

  const viewDur = $derived(viewEnd - viewStart)
  const progress = $derived(viewDur > 0 ? Math.max(0, Math.min(100, ((currentTime - viewStart) / viewDur) * 100)) : 0)
  const selStartPct = $derived(viewDur > 0 ? Math.max(0, Math.min(100, ((selectionStart - viewStart) / viewDur) * 100)) : 0)
  const selEndPct = $derived(viewDur > 0 ? Math.max(0, Math.min(100, ((selectionEnd - viewStart) / viewDur) * 100)) : 0)
  const filmScaleX = $derived(viewDur > 0 && duration > 0 ? duration / viewDur : 1)
  const filmTranslateX = $derived(duration > 0 ? -(viewStart / duration) * 100 : 0)
  const currentSeg = $derived(Math.floor(currentTime / 60))
  const totalSegs = $derived(route?.maxqlog != null ? route.maxqlog + 1 : Math.ceil(duration / 60))

  const SPRITE_COUNT = 20
  // Distribute sprites evenly by time across the route, regardless of segment count
  const filmstripSlots = $derived(() => {
    if (duration <= 0) return []
    const step = duration / SPRITE_COUNT
    return Array.from({ length: SPRITE_COUNT }, (_, i) => {
      const t = i * step + step / 2  // center of each slot
      const seg = Math.min(Math.floor(t / 60), totalSegs - 1)
      const secInSeg = Math.floor(t % 60)
      return { seg, t: secInSeg }
    })
  })

  function getTimeFromEvent(e) {
    if (!timelineEl || duration <= 0) return 0
    const rect = timelineEl.getBoundingClientRect()
    const clientX = e.touches ? e.touches[0].clientX : e.clientX
    const pct = (clientX - rect.left) / rect.width
    if (draggingHandle) {
      return Math.max(0, Math.min(duration, viewStart + pct * viewDur))
    }
    return viewStart + Math.max(0, Math.min(1, pct)) * viewDur
  }

  // Add/remove window listeners synchronously — $effect is async-batched in
  // Svelte 5 and would miss the first mousemove after mousedown.
  function addDragListeners() {
    window.addEventListener('mousemove', onDrag)
    window.addEventListener('mouseup', stopDrag)
    window.addEventListener('touchmove', onDrag, { passive: true })
    window.addEventListener('touchend', stopDrag)
    window.addEventListener('touchcancel', stopDrag)
  }

  function removeDragListeners() {
    window.removeEventListener('mousemove', onDrag)
    window.removeEventListener('mouseup', stopDrag)
    window.removeEventListener('touchmove', onDrag)
    window.removeEventListener('touchend', stopDrag)
    window.removeEventListener('touchcancel', stopDrag)
  }

  function startHandleDrag(handle, e) {
    draggingHandle = handle
    isDragging = true
    e.preventDefault()
    e.stopPropagation()
    addDragListeners()
  }

  function startDrag(e) {
    isDragging = true
    const t = getTimeFromEvent(e)
    dragOrigin = t
    prevSel = { start: selectionStart, end: selectionEnd }
    onSeek(t)
    e.preventDefault()
    addDragListeners()
  }

  function onDrag(e) {
    if (!isDragging) return
    const t = getTimeFromEvent(e)
    if (draggingHandle === 'start') {
      selectionStart = Math.max(0, Math.min(t, selectionEnd - 1))
      if (selectionStart < viewStart) viewStart = selectionStart
      onSeek(selectionStart)
    } else if (draggingHandle === 'end') {
      selectionEnd = Math.min(duration, Math.max(t, selectionStart + 1))
      if (selectionEnd > viewEnd) viewEnd = selectionEnd
      onSeek(selectionStart)
    } else if (dragOrigin != null) {
      const lo = Math.max(0, Math.min(dragOrigin, t))
      const hi = Math.min(duration, Math.max(dragOrigin, t))
      if (hi - lo > 5) {
        selectionStart = lo
        selectionEnd = hi
      }
      onSeek(t)
    }
  }

  function stopDrag() {
    const selDur = selectionEnd - selectionStart
    if (draggingHandle && selDur > 0) {
      const padding = selDur / 2
      viewStart = Math.max(0, selectionStart - padding)
      viewEnd = Math.min(duration, selectionEnd + padding)
    } else if (dragOrigin != null && selDur > 5 && selDur < duration - 5) {
      const padding = selDur / 2
      viewStart = Math.max(0, selectionStart - padding)
      viewEnd = Math.min(duration, selectionEnd + padding)
    } else if (dragOrigin != null && selDur <= 5) {
      selectionStart = prevSel.start
      selectionEnd = prevSel.end
    }
    isDragging = false
    draggingHandle = null
    dragOrigin = null
    removeDragListeners()
  }

  function resetView() {
    selectionStart = 0
    selectionEnd = duration
    viewStart = 0
    viewEnd = duration
  }
</script>

<!-- Timeline scrubber: full-width, handles inside -->
<div
  bind:this={timelineEl}
  class="relative h-[44px] select-none group rounded-xl overflow-hidden cursor-pointer"
  role="slider"
  tabindex="0"
  aria-label="Video timeline"
  aria-valuenow={Math.round(currentTime)}
  aria-valuemin={0}
  aria-valuemax={Math.round(duration)}
  onmousedown={startDrag}
  ontouchstart={startDrag}
  ondblclick={resetView}
  onkeydown={(e) => {
    if (e.key === 'ArrowRight') onSeek(Math.min(duration, currentTime + 5))
    else if (e.key === 'ArrowLeft') onSeek(Math.max(0, currentTime - 5))
  }}
>
  <!-- Filmstrip + events (zoomed with view window) -->
  <div
    class="absolute inset-0"
    style="transform-origin: 0 0; transform: scaleX({filmScaleX}) translateX({filmTranslateX}%){isDragging ? '' : '; transition: transform 0.3s ease-out'}"
  >
    <!-- Filmstrip background -->
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

    <!-- Event timeline overlay -->
    <div class="absolute bottom-0 left-0 right-0">
      <EventTimeline {events} {durationMs} height="3px" />
    </div>
  </div>

  <!-- Dark overlay for played region contrast -->
  <div
    class="absolute inset-0 bg-black/30 pointer-events-none"
    style="clip-path: inset(0 {100 - progress}% 0 0)"
  ></div>

  <!-- Selection: darken outside range -->
  {#if duration > 0}
    <div
      class="absolute top-0 bottom-0 left-0 bg-black/50 pointer-events-none"
      style="width: {selStartPct}%"
    ></div>
    <div
      class="absolute top-0 bottom-0 right-0 bg-black/50 pointer-events-none"
      style="width: {100 - selEndPct}%"
    ></div>
  {/if}

  <!-- Playhead marker -->
  <div
    class="absolute top-0 bottom-0 w-0.5 bg-white shadow-[0_0_4px_rgba(255,255,255,0.5)] z-20 pointer-events-none"
    style="left: {progress}%"
  ></div>

  <!-- Selection handles inside timeline -->
  {#if duration > 0}
    <!-- Start handle -->
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div
      class="absolute top-1/4 bottom-1/4 w-4 cursor-ew-resize z-10 flex items-center justify-center"
      style="left: {selStartPct}%; transform: translateX(-50%)"
      onmousedown={(e) => startHandleDrag('start', e)}
      ontouchstart={(e) => startHandleDrag('start', e)}
    >
      <div class="w-1 h-full rounded-full bg-white shadow-[0_0_4px_rgba(255,255,255,0.5)]"></div>
    </div>

    <!-- End handle -->
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div
      class="absolute top-1/4 bottom-1/4 w-4 cursor-ew-resize z-10 flex items-center justify-center"
      style="left: {selEndPct}%; transform: translateX(-50%)"
      onmousedown={(e) => startHandleDrag('end', e)}
      ontouchstart={(e) => startHandleDrag('end', e)}
    >
      <div class="w-1 h-full rounded-full bg-white shadow-[0_0_4px_rgba(255,255,255,0.5)]"></div>
    </div>
  {/if}

  <!-- Segment number above playhead -->
  <div
    class="absolute -top-3 text-[10px] text-surface-300 font-mono pointer-events-none z-20"
    style="left: {progress}%; transform: translateX(-50%)"
  >{currentSeg}</div>
</div>
