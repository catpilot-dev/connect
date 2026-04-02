<script>
  import { onMount } from 'svelte'
  import { fetchScreenshots, screenshotUrl, deleteScreenshot } from '../api.js'

  let screenshots = $state([])
  let loading = $state(true)
  let error = $state(null)
  let selected = $state(new Set())
  let lightbox = $state(null)

  async function load() {
    loading = true
    error = null
    try {
      screenshots = await fetchScreenshots()
    } catch (e) {
      error = e.message
    } finally {
      loading = false
    }
  }

  onMount(load)

  function toggleSelect(e, filename) {
    e.stopPropagation()
    const next = new Set(selected)
    if (next.has(filename)) next.delete(filename)
    else next.add(filename)
    selected = next
  }

  function selectAll() {
    if (selected.size === screenshots.length) {
      selected = new Set()
    } else {
      selected = new Set(screenshots.map(s => s.filename))
    }
  }

  async function deleteSelected() {
    if (selected.size === 0) return
    const count = selected.size
    if (!confirm(`Delete ${count} screenshot${count > 1 ? 's' : ''}?`)) return

    const toDelete = [...selected]
    for (const filename of toDelete) {
      try {
        await deleteScreenshot(filename)
      } catch { /* ignore individual failures */ }
    }
    selected = new Set()
    await load()
  }

  function downloadOne(filename) {
    const a = document.createElement('a')
    a.href = screenshotUrl(filename)
    a.download = filename
    a.click()
  }

  async function downloadSelected() {
    const files = selected.size > 0 ? [...selected] : screenshots.map(s => s.filename)
    for (const f of files) {
      downloadOne(f)
      await new Promise(r => setTimeout(r, 200))
    }
  }

  function formatDate(mtime) {
    return new Date(mtime * 1000).toLocaleString()
  }

  function formatSize(bytes) {
    if (bytes < 1024) return `${bytes} B`
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
  }

  function handleKeydown(e) {
    if (!lightbox) return
    if (e.key === 'Escape') lightbox = null
    else if (e.key === 'ArrowRight' || e.key === 'ArrowDown') navigateLightbox(1)
    else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') navigateLightbox(-1)
  }

  function navigateLightbox(dir) {
    if (!lightbox) return
    const idx = screenshots.findIndex(s => s.filename === lightbox)
    const next = idx + dir
    if (next >= 0 && next < screenshots.length) {
      lightbox = screenshots[next].filename
    }
  }
</script>

<svelte:window onkeydown={handleKeydown} />

<div class="p-4 max-w-7xl mx-auto">
  <div class="flex items-center justify-between mb-4">
    <h2 class="text-lg font-medium text-surface-100">
      Screenshots
      {#if !loading && screenshots.length > 0}
        <span class="text-surface-500 text-sm ml-2">({screenshots.length})</span>
      {/if}
    </h2>
    <div class="flex items-center gap-2">
      {#if screenshots.length > 0}
        <button class="btn-ghost text-xs" onclick={selectAll}>
          {selected.size === screenshots.length ? 'Deselect All' : 'Select All'}
        </button>
        {#if selected.size > 0}
          <button class="btn-ghost text-xs text-engage-red" onclick={deleteSelected}>
            Delete ({selected.size})
          </button>
        {/if}
        <button class="btn-ghost text-xs" onclick={downloadSelected}>
          {selected.size > 0 ? `Download (${selected.size})` : 'Download All'}
        </button>
      {/if}
      <button class="btn-ghost text-xs" onclick={load}>Refresh</button>
    </div>
  </div>

  {#if loading}
    <div class="flex items-center justify-center h-48">
      <p class="text-surface-400 text-sm">Loading...</p>
    </div>
  {:else if error}
    <div class="flex items-center justify-center h-48">
      <p class="text-engage-red text-sm">{error}</p>
    </div>
  {:else if screenshots.length === 0}
    <div class="flex items-center justify-center h-48">
      <div class="text-center">
        <p class="text-surface-400">No screenshots yet</p>
        <p class="text-surface-600 text-sm mt-1">Tap the camera icon on the HUD to capture</p>
      </div>
    </div>
  {:else}
    <div class="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-3">
      {#each screenshots as s (s.filename)}
        {@const sel = selected.has(s.filename)}
        <div
          class="group relative rounded-lg overflow-hidden bg-surface-800 border transition-colors {sel ? 'border-engage-green' : 'border-surface-700 hover:border-surface-500'}"
        >
          <button
            class="w-full aspect-video bg-surface-900 cursor-pointer"
            onclick={() => { lightbox = s.filename }}
          >
            <img
              src={screenshotUrl(s.filename)}
              alt={s.filename}
              class="w-full h-full object-cover"
              loading="lazy"
            />
          </button>

          <button
            class="absolute top-2 left-2 w-6 h-6 rounded border flex items-center justify-center transition-colors cursor-pointer
              {sel ? 'bg-engage-green border-engage-green text-white' : 'bg-surface-900/70 border-surface-500 text-transparent group-hover:text-surface-400'}"
            onclick={(e) => toggleSelect(e, s.filename)}
          >
            <svg class="w-4 h-4" viewBox="0 0 20 20" fill="currentColor">
              <path fill-rule="evenodd" d="M16.707 5.293a1 1 0 010 1.414l-8 8a1 1 0 01-1.414 0l-4-4a1 1 0 011.414-1.414L8 12.586l7.293-7.293a1 1 0 011.414 0z" clip-rule="evenodd"/>
            </svg>
          </button>

          <div class="absolute top-2 right-2 flex gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
            <button
              class="w-7 h-7 rounded bg-surface-900/80 flex items-center justify-center text-surface-300 hover:text-surface-50 cursor-pointer"
              onclick={(e) => { e.stopPropagation(); downloadOne(s.filename) }}
              title="Download"
            >
              <svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4"/>
              </svg>
            </button>
          </div>

          <div class="px-2.5 py-1.5 text-xs text-surface-400">
            <div class="truncate">{formatDate(s.mtime)}</div>
            <div class="text-surface-600">{formatSize(s.size)}</div>
          </div>
        </div>
      {/each}
    </div>
  {/if}
</div>

{#if lightbox}
  <div
    class="fixed inset-0 z-50 bg-black/90 flex items-center justify-center"
    role="dialog"
    aria-modal="true"
    onclick={() => { lightbox = null }}
  >
    <button
      class="absolute top-4 right-4 w-10 h-10 rounded-full bg-surface-800/80 flex items-center justify-center text-surface-300 hover:text-white cursor-pointer z-10"
      onclick={() => { lightbox = null }}
    >
      <svg class="w-6 h-6" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
        <path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12"/>
      </svg>
    </button>

    {#if screenshots.findIndex(s => s.filename === lightbox) > 0}
      <button
        class="absolute left-4 top-1/2 -translate-y-1/2 w-10 h-10 rounded-full bg-surface-800/80 flex items-center justify-center text-surface-300 hover:text-white cursor-pointer"
        onclick={(e) => { e.stopPropagation(); navigateLightbox(-1) }}
      >
        <svg class="w-6 h-6" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" d="M15 19l-7-7 7-7"/>
        </svg>
      </button>
    {/if}
    {#if screenshots.findIndex(s => s.filename === lightbox) < screenshots.length - 1}
      <button
        class="absolute right-4 top-1/2 -translate-y-1/2 w-10 h-10 rounded-full bg-surface-800/80 flex items-center justify-center text-surface-300 hover:text-white cursor-pointer"
        onclick={(e) => { e.stopPropagation(); navigateLightbox(1) }}
      >
        <svg class="w-6 h-6" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" d="M9 5l7 7-7 7"/>
        </svg>
      </button>
    {/if}

    <!-- svelte-ignore a11y_click_events_have_key_events -->
    <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
    <img
      src={screenshotUrl(lightbox)}
      alt={lightbox}
      class="max-w-[95vw] max-h-[90vh] object-contain"
      onclick={(e) => e.stopPropagation()}
    />

    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div
      class="absolute bottom-4 left-1/2 -translate-x-1/2 flex items-center gap-3 bg-surface-800/80 rounded-lg px-4 py-2"
      onclick={(e) => e.stopPropagation()}
    >
      <span class="text-surface-300 text-sm">{lightbox}</span>
      <button
        class="text-surface-400 hover:text-surface-100 cursor-pointer"
        onclick={() => downloadOne(lightbox)}
        title="Download"
      >
        <svg class="w-5 h-5" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4"/>
        </svg>
      </button>
      <button
        class="text-surface-400 hover:text-engage-red cursor-pointer"
        onclick={async () => {
          if (!confirm('Delete this screenshot?')) return
          await deleteScreenshot(lightbox)
          lightbox = null
          await load()
        }}
        title="Delete"
      >
        <svg class="w-5 h-5" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"/>
        </svg>
      </button>
    </div>
  </div>
{/if}
