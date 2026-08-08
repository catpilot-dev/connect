<script>
  import { onMount } from 'svelte'
  import { dongleId, selectedRoute, isMetric } from './lib/stores.js'
  import { fetchDevices, fetchIsOnroad, fetchParams, fetchUpdates, applyUpdates } from './lib/api.js'
  import DeviceHeader from './lib/components/DeviceHeader.svelte'
  import UpdateBanner from './lib/components/UpdateBanner.svelte'
  import RouteListPage from './lib/pages/RouteListPage.svelte'
  import RouteDetailPage from './lib/pages/RouteDetailPage.svelte'
  import TileManager from './lib/pages/TileManager.svelte'
  import SettingsPage from './lib/pages/SettingsPage.svelte'
  import DashboardPage from './lib/pages/DashboardPage.svelte'
  import SignalBrowserPage from './lib/pages/SignalBrowserPage.svelte'
  import PluginsPage from './lib/pages/PluginsPage.svelte'
  import ScreenshotsPage from './lib/pages/ScreenshotsPage.svelte'

  let error = $state(null)
  let isOnroad = $state(false)
  let updates = $state(null)
  let updatesDismissed = $state(false)
  function parsePage() {
    const parts = location.pathname.split('/').filter(Boolean)
    if (parts[0] === 'tiles') return 'tiles'
    if (parts[0] === 'settings') return 'settings'
    if (parts[0] === 'plugins') return 'plugins'
    if (parts[0] === 'screenshots') return 'screenshots'
    if (parts[0] === 'routes') return 'routes'
    // if (parts[0] === 'dashboard') return 'dashboard'  // disabled for now
    if (parts[0] === 'signals') return 'signals'
    // Route detail (/{dongleId}/{localId}) and anything unrecognised — including
    // the old /home — all land on the route list.
    return 'routes'
  }

  let page = $state(parsePage())

  function parseRoutePath() {
    // URL: /{dongleId}/{localId}/{start?}/{end?}
    const parts = location.pathname.split('/').filter(Boolean)
    if (parts[0] === 'tiles') return null
    return parts.length >= 2 ? parts[1] : null  // local_id
  }

  // ── Screen wake lock ─────────────────────────────────────────────────────
  // Keeps the phone screen on while COD is open.
  // Re-acquires automatically after tab visibility is restored (e.g. unlock).
  function startWakeLock() {
    if (!navigator.wakeLock) return
    let lock = null
    async function acquire() {
      try { lock = await navigator.wakeLock.request('screen') } catch {}
    }
    acquire()
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible') acquire()
    })
    return () => lock?.release()
  }

  // ── Phone GPS sender ─────────────────────────────────────────────────────
  // Streams browser Geolocation fixes to /ws/gps on the device so the
  // phone_gps plugin can publish gpsLocationExternal cereal messages.
  function startGpsSender() {
    if (!navigator.geolocation) return

    const proto = location.protocol === 'https:' ? 'wss' : 'ws'
    let ws = null
    let watchId = null
    let reconnectTimer = null

    function connect() {
      ws = new WebSocket(`${proto}://${location.host}/ws/gps`)
      ws.onopen = () => {
        watchId = navigator.geolocation.watchPosition(
          (pos) => {
            if (ws.readyState !== WebSocket.OPEN) return
            const c = pos.coords
            ws.send(JSON.stringify({
              latitude:         c.latitude,
              longitude:        c.longitude,
              altitude:         c.altitude,
              speed:            c.speed,
              heading:          c.heading,
              accuracy:         c.accuracy,
              altitudeAccuracy: c.altitudeAccuracy,
              timestamp:        pos.timestamp,
            }))
          },
          (err) => console.warn('phone_gps: geolocation error', err.message),
          { enableHighAccuracy: true, maximumAge: 1000, timeout: 10000 },
        )
      }
      ws.onclose = () => {
        if (watchId !== null) { navigator.geolocation.clearWatch(watchId); watchId = null }
        // Reconnect after 5s if page is still open
        reconnectTimer = setTimeout(connect, 5000)
      }
    }

    connect()

    return () => {
      clearTimeout(reconnectTimer)
      if (watchId !== null) navigator.geolocation.clearWatch(watchId)
      if (ws) ws.close()
    }
  }

  // isOnroad gates the Update, Reboot and Software controls, so it must not go
  // stale while the page sits open — a session that starts parked and then
  // drives off would otherwise keep offering them. Polled rather than streamed:
  // /ws/home is gone, and this only needs to be right to within a few seconds.
  function startOnroadWatcher() {
    const timer = setInterval(async () => {
      try { isOnroad = await fetchIsOnroad() } catch {}
    }, 30000)
    return () => clearInterval(timer)
  }

  onMount(async () => {
    // Fetch all startup data in parallel
    const [onroadResult, devicesResult, paramsResult, updatesResult] = await Promise.allSettled([
      fetchIsOnroad(),
      fetchDevices(),
      fetchParams(),
      fetchUpdates(),
    ])
    isOnroad = onroadResult.status === 'fulfilled' ? onroadResult.value : false
    if (devicesResult.status === 'fulfilled' && devicesResult.value?.length > 0) {
      dongleId.set(devicesResult.value[0].dongle_id)
    } else if (devicesResult.status === 'rejected') {
      error = devicesResult.reason?.message ?? 'Connection error'
    }
    if (paramsResult.status === 'fulfilled') {
      isMetric.set(paramsResult.value.IsMetric !== '0')
    }
    if (updatesResult.status === 'fulfilled') {
      updates = updatesResult.value
    }

    page = parsePage()
    const initialRoute = parseRoutePath()
    if (initialRoute) selectedRoute.set(initialRoute)

    // Sync selectedRoute → URL (pushState only on route switch)
    let lastRoute = initialRoute
    const unsub = selectedRoute.subscribe(route => {
      if (route === lastRoute) return
      lastRoute = route
      if (!route && page === 'routes') {
        history.pushState(null, '', '/routes')
      }
    })

    window.addEventListener('popstate', () => {
      const p = parsePage()
      // if (isOnroad && (p === 'routes')) {
      //   page = 'dashboard'
      //   history.replaceState(null, '', '/dashboard')
      //   return
      // }
      page = p
      const route = parseRoutePath()
      lastRoute = route
      selectedRoute.set(route)
    })

    const stopWakeLock = startWakeLock()
    const stopGps = startGpsSender()
    const stopOnroadWatcher = startOnroadWatcher()
    return () => { unsub(); if (stopWakeLock) stopWakeLock(); if (stopGps) stopGps(); stopOnroadWatcher() }
  })

  function showRoutes() {
    page = 'routes'
    selectedRoute.set(null)
    history.pushState(null, '', '/routes')
  }

  function showSettings() {
    page = 'settings'
    selectedRoute.set(null)
    history.pushState(null, '', '/settings')
  }

  function showDashboard() {
    page = 'dashboard'
    selectedRoute.set(null)
    history.pushState(null, '', '/dashboard')
  }

  function showPlugins() {
    page = 'plugins'
    selectedRoute.set(null)
    history.pushState(null, '', '/plugins')
  }

  function showScreenshots() {
    page = 'screenshots'
    selectedRoute.set(null)
    history.pushState(null, '', '/screenshots')
  }

  async function handleUpdate() {
    const data = await applyUpdates()
    if (data.cod_updated) {
      // Server will restart — reload page after delay
      setTimeout(() => location.reload(), 4000)
    }
    return data
  }
</script>

{#if page === 'signals'}
  <SignalBrowserPage />
{:else if page === 'tiles'}
  <TileManager />
<!-- Dashboard disabled for now
{:else if isOnroad && page === 'dashboard'}
  <DashboardPage {isOnroad} />
-->
{:else}
  <div class="min-h-dvh flex flex-col">
    <DeviceHeader>
      {#snippet nav()}
        <div class="flex items-center gap-1">
          <button
            class="px-3 py-1.5 text-sm rounded transition-colors {page === 'routes' && !$selectedRoute ? 'bg-surface-700 text-surface-50' : 'text-surface-400 hover:text-surface-200'}"
            onclick={showRoutes}
          >
            Routes
          </button>
          <!-- Dashboard button disabled for now
          <button
            class="px-3 py-1.5 text-sm rounded transition-colors {page === 'dashboard' ? 'bg-surface-700 text-surface-50' : 'text-surface-400 hover:text-surface-200'}"
            onclick={showDashboard}
          >
            Dashboard
          </button>
          -->
          <button
            class="px-3 py-1.5 text-sm rounded transition-colors {page === 'settings' ? 'bg-surface-700 text-surface-50' : 'text-surface-400 hover:text-surface-200'}"
            onclick={showSettings}
          >
            Settings
          </button>
          <button
            class="px-3 py-1.5 text-sm rounded transition-colors {page === 'plugins' ? 'bg-surface-700 text-surface-50' : 'text-surface-400 hover:text-surface-200'}"
            onclick={showPlugins}
          >
            Plugins
          </button>
          <button
            class="px-3 py-1.5 text-sm rounded transition-colors {page === 'screenshots' ? 'bg-surface-700 text-surface-50' : 'text-surface-400 hover:text-surface-200'}"
            onclick={showScreenshots}
          >
            Captures
          </button>
        </div>
      {/snippet}
    </DeviceHeader>

    {#if updates && !updatesDismissed && (updates.cod?.available || updates.plugins?.available)}
      <UpdateBanner {updates} {isOnroad} onDismiss={() => updatesDismissed = true} onUpdate={handleUpdate} />
    {/if}

    <main class="flex-1 flex flex-col">
      {#if error}
        <div class="flex items-center justify-center h-64">
          <div class="text-center">
            <p class="text-engage-red text-lg mb-2">Connection Error</p>
            <p class="text-surface-400 text-sm">{error}</p>
            <button class="btn-ghost mt-4" onclick={() => location.reload()}>
              Retry
            </button>
          </div>
        </div>
      <!-- {:else if page === 'dashboard'}
        <DashboardPage {isOnroad} /> -->
      {:else if page === 'settings'}
        <SettingsPage {isOnroad} />
      {:else if page === 'plugins'}
        <PluginsPage />
      {:else if page === 'screenshots'}
        <ScreenshotsPage />
      {:else if $selectedRoute}
        <RouteDetailPage />
      {:else}
        <RouteListPage />
      {/if}
    </main>
  </div>
{/if}
