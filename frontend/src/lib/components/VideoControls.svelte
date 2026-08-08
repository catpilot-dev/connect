<script>
  import { formatVideoTimeHMS, formatAbsoluteTime, formatEpochTime } from '../format.js'

  /**
   * Video playback controls bar.
   *
   * Center: << < {time} - {seg} - {frame} > >>
   * Right:  play/pause, mute, screenshot, speed, fullscreen
   */

  let {
    route,
    currentTime = 0,
    duration = 0,
    startTime = 0,
    absoluteTime = null,
    onSeek,
    onToggle,
    onRate,
    onScreenshot,
    onStepFrame,
    onMuteToggle,
    onSourceChange,
    isPlaying = false,
    isMuted = true,
    screenshotBusy = false,
    hdSource = null,
    sources = [],
  } = $props()

  let showSpeedMenu = $state(false)
  let showSourceMenu = $state(false)
  let playbackRate = $state(1)

  const sourceLabel = $derived(sources.find(s => s.id === hdSource)?.label ?? 'HD')

  const speeds = [0.5, 1, 1.5, 2]
  const SKIP_STEP = 5
  const currentSeg = $derived(Math.floor(currentTime / 60))
  const currentFrame = $derived(String(Math.floor((currentTime % 1) * 20)).padStart(2, '0'))
  // Prefer the log-derived per-frame time; fall back to route start + offset
  // for modes that have no frame times (MJPEG / HLS qcamera).
  const timeDisplay = $derived(
    formatEpochTime(absoluteTime)
    || formatAbsoluteTime(startTime, currentTime)
    || formatVideoTimeHMS(currentTime)
  )

  function setSpeed(rate) {
    playbackRate = rate
    onRate(rate)
    showSpeedMenu = false
  }

  function seekBack() {
    onSeek(Math.max(0, currentTime - SKIP_STEP))
  }

  function seekFwd() {
    onSeek(Math.min(duration, currentTime + SKIP_STEP))
  }

  function toggleFullscreen() {
    const container = document.querySelector('[data-video-container]')
    if (!container) return

    // Native Fullscreen API (desktop + Android)
    if (document.fullscreenElement) {
      document.exitFullscreen?.()
      return
    }
    const rfs = container.requestFullscreen
      || container.webkitRequestFullscreen
      || container.mozRequestFullScreen
      || container.msRequestFullscreen
    if (rfs) {
      rfs.call(container)
      return
    }

    // iOS fallback: CSS-based fullscreen (iOS doesn't support Fullscreen API on divs)
    container.classList.toggle('ios-fullscreen')
    const entering = container.classList.contains('ios-fullscreen')
    // Dispatch synthetic fullscreenchange so RouteDetailPage picks it up
    document.dispatchEvent(new Event('fullscreenchange'))
    if (entering) {
      container._iosEscHandler = (e) => {
        if (e.key === 'Escape') {
          container.classList.remove('ios-fullscreen')
          document.dispatchEvent(new Event('fullscreenchange'))
          document.removeEventListener('keydown', container._iosEscHandler)
        }
      }
      document.addEventListener('keydown', container._iosEscHandler)
    }
  }
</script>

<div class="flex items-center h-8 gap-1">
  <!-- Left group: source selector + screenshot -->
  <div class="flex items-center gap-0.5 shrink-0">
    {#if sources.length > 0}
      <div class="relative">
        <button
          class="btn-ghost text-xs px-1.5 py-0.5 rounded text-engage-green"
          onclick={() => { showSourceMenu = !showSourceMenu; showSpeedMenu = false }}
          title="Video source"
        >
          {sourceLabel}
        </button>
        {#if showSourceMenu}
          <!-- svelte-ignore a11y_click_events_have_key_events -->
          <!-- svelte-ignore a11y_interactive_supports_focus -->
          <div
            class="absolute bottom-full left-0 mb-1 bg-surface-800 border border-surface-700 rounded-xl shadow-xl py-1 z-20 min-w-[5rem]"
            role="menu"
            onclick={(e) => e.stopPropagation()}
          >
            {#each sources as src}
              <button
                class="block w-full text-left px-4 py-1.5 text-xs hover:bg-surface-700 transition-colors"
                class:text-engage-blue={hdSource === src.id}
                role="menuitem"
                onclick={() => { onSourceChange?.(src.id); showSourceMenu = false }}
              >{src.label}</button>
            {/each}
          </div>
        {/if}
      </div>
    {/if}

    {#if onScreenshot}
      <button
        class="hidden sm:inline-flex btn-ghost p-1.5 disabled:opacity-30"
        onclick={onScreenshot}
        disabled={isPlaying || screenshotBusy}
        aria-label="Screenshot"
        title="Screenshot"
      >
        {#if screenshotBusy}
          <div class="w-4 h-4 border-2 border-surface-300 border-t-transparent rounded-full animate-spin"></div>
        {:else}
          <svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
            <path d="M23 19a2 2 0 01-2 2H3a2 2 0 01-2-2V8a2 2 0 012-2h4l2-3h6l2 3h4a2 2 0 012 2z"/>
            <circle cx="12" cy="13" r="4"/>
          </svg>
        {/if}
      </button>
    {/if}
  </div>

  <!-- Center group: -5s {Time}-{Seg} +5s | Play/Pause | -1f {Frame} +1f -->
  <div class="flex-1 min-w-0 flex items-center justify-center">
    <div class="flex items-center gap-0.5 sm:gap-1">
      <!-- Skip back -5s -->
      <button
        class="btn-ghost p-1"
        onclick={seekBack}
        aria-label="Back {SKIP_STEP} seconds"
        title="-{SKIP_STEP}s"
      >
        <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
          <path d="M1 4v6h6"/>
          <path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"/>
        </svg>
      </button>

      <!-- Time (+ Seg on sm+) -->
      <span class="text-xs font-mono tabular-nums text-surface-200 px-0.5 truncate">
        <span title="GPS local time">{timeDisplay}</span><span class="hidden sm:inline text-surface-500 mx-0.5">-</span><span class="hidden sm:inline" title="Segment">{currentSeg}</span>
      </span>

      <!-- Skip forward +5s -->
      <button
        class="btn-ghost p-1"
        onclick={seekFwd}
        aria-label="Forward {SKIP_STEP} seconds"
        title="+{SKIP_STEP}s"
      >
        <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
          <path d="M23 4v6h-6"/>
          <path d="M20.49 15a9 9 0 1 1-2.13-9.36L23 10"/>
        </svg>
      </button>

      <!-- Play/Pause -->
      <button
        class="btn-ghost p-1.5"
        onclick={onToggle}
        aria-label={isPlaying ? 'Pause' : 'Play'}
        title={isPlaying ? 'Pause' : 'Play'}
      >
        {#if isPlaying}
          <svg class="w-4 h-4" fill="currentColor" viewBox="0 0 20 20">
            <path fill-rule="evenodd" d="M5.75 3a.75.75 0 01.75.75v12.5a.75.75 0 01-1.5 0V3.75A.75.75 0 015.75 3zm8.5 0a.75.75 0 01.75.75v12.5a.75.75 0 01-1.5 0V3.75a.75.75 0 01.75-.75z" clip-rule="evenodd"/>
          </svg>
        {:else}
          <svg class="w-4 h-4" fill="currentColor" viewBox="0 0 20 20">
            <path d="M6.3 2.841A1.5 1.5 0 004 4.11V15.89a1.5 1.5 0 002.3 1.269l9.344-5.89a1.5 1.5 0 000-2.538L6.3 2.84z"/>
          </svg>
        {/if}
      </button>

      <!-- Frame controls: hidden on mobile -->
      <div class="hidden sm:flex items-center gap-0.5">
        <!-- Frame back -1f -->
        <button
          class="btn-ghost p-1 text-engage-blue disabled:opacity-30"
          onclick={() => onStepFrame?.(-1)}
          disabled={isPlaying}
          aria-label="Previous frame"
          title="-1 frame"
        >
          <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
            <path d="M1 4v6h6"/>
            <path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"/>
          </svg>
        </button>

        <!-- Frame -->
        <span class="text-xs font-mono tabular-nums px-0.5 text-engage-blue" class:opacity-30={isPlaying} title="Frame (0-19)">{currentFrame}</span>

        <!-- Frame forward +1f -->
        <button
          class="btn-ghost p-1 text-engage-blue disabled:opacity-30"
          onclick={() => onStepFrame?.(1)}
          disabled={isPlaying}
          aria-label="Next frame"
          title="+1 frame"
        >
          <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
            <path d="M23 4v6h-6"/>
            <path d="M20.49 15a9 9 0 1 1-2.13-9.36L23 10"/>
          </svg>
        </button>
      </div>

    </div>
  </div>

  <!-- Right group: playback actions -->
  <div class="flex items-center gap-0.5 shrink-0">
    <!-- Mute/Unmute -->
    <button
      class="btn-ghost p-1.5"
      onclick={onMuteToggle}
      aria-label={isMuted ? 'Unmute' : 'Mute'}
      title={isMuted ? 'Unmute' : 'Mute'}
    >
      {#if isMuted}
        <svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
          <path d="M11 5L6 9H2v6h4l5 4V5z"/>
          <line x1="23" y1="9" x2="17" y2="15"/>
          <line x1="17" y1="9" x2="23" y2="15"/>
        </svg>
      {:else}
        <svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
          <path d="M11 5L6 9H2v6h4l5 4V5z"/>
          <path d="M19.07 4.93a10 10 0 010 14.14M15.54 8.46a5 5 0 010 7.07"/>
        </svg>
      {/if}
    </button>

    <!-- Speed selector -->
    <div class="relative">
      <button
        class="btn-ghost text-xs px-2 py-1"
        onclick={() => { showSpeedMenu = !showSpeedMenu; showSourceMenu = false }}
        title="Playback speed"
      >
        {playbackRate}x
      </button>
      {#if showSpeedMenu}
        <!-- svelte-ignore a11y_click_events_have_key_events -->
        <!-- svelte-ignore a11y_interactive_supports_focus -->
        <div
          class="absolute bottom-full right-0 mb-1 bg-surface-800 border border-surface-700 rounded-xl shadow-xl py-1 z-20"
          role="menu"
          onclick={(e) => e.stopPropagation()}
        >
          {#each speeds as rate}
            <button
              class="block w-full text-left px-4 py-1.5 text-xs hover:bg-surface-700 transition-colors"
              class:text-engage-blue={rate === playbackRate}
              role="menuitem"
              onclick={() => setSpeed(rate)}
            >
              {rate}x
            </button>
          {/each}
        </div>
      {/if}
    </div>

    <!-- Fullscreen -->
    <button
      class="btn-ghost p-1.5"
      onclick={toggleFullscreen}
      aria-label="Toggle fullscreen"
      title="Fullscreen"
    >
      <svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
        <path d="M8 3H5a2 2 0 00-2 2v3m18 0V5a2 2 0 00-2-2h-3m0 18h3a2 2 0 002-2v-3M3 16v3a2 2 0 002 2h3"/>
      </svg>
    </button>
  </div>
</div>

<!-- Close menus on outside click -->
{#if showSpeedMenu || showSourceMenu}
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div class="fixed inset-0 z-10" onclick={() => { showSpeedMenu = false; showSourceMenu = false }}></div>
{/if}
