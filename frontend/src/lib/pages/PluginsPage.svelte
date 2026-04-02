<script>
  import { onMount } from 'svelte'
  import Toggle from '../components/Toggle.svelte'
  import Spinner from '../components/Spinner.svelte'
  import ModelPanel from '../components/ModelPanel.svelte'
  import MapdPanel from '../components/MapdPanel.svelte'
  import ChevronIcon from '../components/ChevronIcon.svelte'
  import { fetchPlugins, togglePlugin, setPluginParam, deviceReboot, fetchPluginRepo, setPluginRepo, installPluginRepo } from '../api.js'

  let plugins = $state(null)
  let loading = $state(true)
  let error = $state(null)
  let togglingPlugin = $state(null)
  let needsReboot = $state(false)
  let expandedPlugin = $state(null)
  let paramValues = $state({})  // { pluginId: { key: value } }
  let savingParam = $state(null)

  // Plugin source state
  let repo = $state(null)
  let editingUrl = $state(false)
  let urlDraft = $state('')
  let savingUrl = $state(false)
  let installing = $state(false)
  let installOutput = $state(null)

  onMount(async () => {
    try {
      const [pluginData, repoData] = await Promise.all([
        fetchPlugins(),
        fetchPluginRepo(),
      ])
      plugins = pluginData
      repo = repoData
      // Initialize local param state from fetched settings
      const vals = {}
      for (const p of plugins) {
        if (p.settings?.length) {
          vals[p.id] = {}
          for (const s of p.settings) {
            vals[p.id][s.key] = s.value
          }
        }
      }
      paramValues = vals
    } catch (e) {
      error = e.message
    } finally {
      loading = false
    }
  })

  function formatTime(ts) {
    if (!ts) return 'never'
    return new Date(ts * 1000).toLocaleString()
  }

  function startEditUrl() {
    urlDraft = repo?.url || ''
    editingUrl = true
  }

  async function saveUrl() {
    savingUrl = true
    try {
      await setPluginRepo(urlDraft)
      repo = { ...repo, url: urlDraft }
      editingUrl = false
    } catch (e) {
      error = e.message
    } finally {
      savingUrl = false
    }
  }

  async function handleInstall() {
    installing = true
    installOutput = null
    error = null
    try {
      const result = await installPluginRepo()
      installOutput = result.output
      if (result.reboot_required) needsReboot = true
      // Refresh plugin list and repo info
      const [pluginData, repoData] = await Promise.all([
        fetchPlugins(),
        fetchPluginRepo(),
      ])
      plugins = pluginData
      repo = repoData
    } catch (e) {
      error = e.message
    } finally {
      installing = false
    }
  }

  async function handleToggle(pluginId) {
    if (togglingPlugin) return
    togglingPlugin = pluginId
    try {
      const result = await togglePlugin(pluginId)
      plugins = plugins.map(p =>
        p.id === pluginId ? { ...p, enabled: result.enabled } : p
      )
      if (result.reboot_required) needsReboot = true
    } catch (e) {
      error = e.message
    } finally {
      togglingPlugin = null
    }
  }

  async function handleParamToggle(pluginId, key) {
    const prev = paramValues[pluginId]?.[key]
    const newVal = !prev
    paramValues[pluginId] = { ...paramValues[pluginId], [key]: newVal }
    savingParam = key
    try {
      await setPluginParam(pluginId, key, newVal)
    } catch (e) {
      paramValues[pluginId] = { ...paramValues[pluginId], [key]: prev }
      error = e.message
    } finally {
      savingParam = null
    }
  }

  async function handleParamPills(pluginId, key, value) {
    const prev = paramValues[pluginId]?.[key]
    paramValues[pluginId] = { ...paramValues[pluginId], [key]: value }
    savingParam = key
    try {
      await setPluginParam(pluginId, key, value)
    } catch (e) {
      paramValues[pluginId] = { ...paramValues[pluginId], [key]: prev }
      error = e.message
    } finally {
      savingParam = null
    }
  }

  async function handleReboot() {
    if (!confirm('Reboot device to apply plugin changes?')) return
    try { await deviceReboot() } catch (e) { error = e.message }
  }

  let enabledCount = $derived(plugins?.filter(p => p.enabled).length ?? 0)
</script>

<div class="w-full max-w-lg mx-auto px-4 py-6 space-y-4 overflow-hidden">
  <div class="flex items-center justify-between">
    {#if plugins}
      <p class="text-sm text-surface-200">{enabledCount} of {plugins.length} enabled</p>
    {/if}
    {#if needsReboot}
      <button
        class="px-3 py-1.5 text-xs rounded-lg bg-engage-green/15 text-engage-green hover:bg-engage-green/25 transition-colors"
        onclick={handleReboot}
      >Reboot to Apply</button>
    {/if}
  </div>

  {#if error}
    <div class="card p-4 border-engage-red/50">
      <p class="text-engage-red text-sm">{error}</p>
      <button class="btn-ghost text-xs mt-2" onclick={() => { error = null }}>Dismiss</button>
    </div>
  {/if}

  <!-- Plugin Source -->
  {#if repo}
    <div class="card p-4 space-y-3">
      <div class="text-xs font-medium text-surface-300 uppercase tracking-wide">Plugin Source</div>
      <div class="flex items-center gap-2">
        {#if editingUrl}
          <input
            type="text"
            bind:value={urlDraft}
            class="flex-1 bg-surface-700 text-surface-100 text-xs rounded px-2 py-1.5 border border-surface-600 focus:border-engage-blue/50 outline-none"
            onkeydown={(e) => e.key === 'Enter' && saveUrl()}
          />
          <button
            class="px-2 py-1 text-xs rounded bg-engage-blue/15 text-engage-blue hover:bg-engage-blue/25 transition-colors"
            disabled={savingUrl}
            onclick={saveUrl}
          >{savingUrl ? '...' : 'Save'}</button>
          <button
            class="px-2 py-1 text-xs rounded bg-surface-700 text-surface-300 hover:bg-surface-600 transition-colors"
            onclick={() => { editingUrl = false }}
          >Cancel</button>
        {:else}
          <span class="flex-1 text-xs text-surface-200 truncate" title={repo.url}>{repo.url}</span>
          <button
            class="text-surface-400 hover:text-surface-200 transition-colors"
            onclick={startEditUrl}
            title="Edit repo URL"
          >
            <svg class="w-3.5 h-3.5" viewBox="0 0 20 20" fill="currentColor">
              <path d="m5.433 13.917 1.262-3.155A4 4 0 0 1 7.58 9.42l6.92-6.918a2.121 2.121 0 0 1 3 3l-6.92 6.918c-.383.383-.84.685-1.343.886l-3.154 1.262a.5.5 0 0 1-.65-.65Z" />
              <path d="M3.5 5.75c0-.69.56-1.25 1.25-1.25H10A.75.75 0 0 0 10 3H4.75A2.75 2.75 0 0 0 2 5.75v9.5A2.75 2.75 0 0 0 4.75 18h9.5A2.75 2.75 0 0 0 17 15.25V10a.75.75 0 0 0-1.5 0v5.25c0 .69-.56 1.25-1.25 1.25h-9.5c-.69 0-1.25-.56-1.25-1.25v-9.5Z" />
            </svg>
          </button>
        {/if}
      </div>
      {#if repo.last_updated}
        <p class="text-[10px] text-surface-400">Last updated: {formatTime(repo.last_updated)}</p>
      {:else if repo.installed}
        <p class="text-[10px] text-surface-400">Installed (update time unknown)</p>
      {:else}
        <p class="text-[10px] text-surface-400">Not installed</p>
      {/if}
      <button
        class="w-full px-3 py-2 text-xs rounded-lg transition-colors {installing ? 'bg-surface-700 text-surface-400' : 'bg-engage-blue/15 text-engage-blue hover:bg-engage-blue/25'}"
        disabled={installing}
        onclick={handleInstall}
      >
        {#if installing}
          <span class="inline-flex items-center gap-2">
            <Spinner size="sm" />
            {repo.installed ? 'Updating...' : 'Installing...'}
          </span>
        {:else}
          {repo.installed ? 'Update Plugins' : 'Install Plugins'}
        {/if}
      </button>
      {#if installOutput}
        <pre class="text-[10px] text-surface-400 bg-surface-800 rounded p-2 max-h-24 overflow-auto whitespace-pre-wrap">{installOutput}</pre>
      {/if}
    </div>
  {/if}

  {#if loading}
    <div class="space-y-4">
      {#each [1, 2, 3] as _}
        <div class="card p-4 animate-pulse">
          <div class="h-4 bg-surface-700 rounded w-32 mb-2"></div>
          <div class="h-3 bg-surface-700 rounded w-48"></div>
        </div>
      {/each}
    </div>
  {:else if plugins && plugins.length === 0}
    <div class="card p-6 text-center">
      <p class="text-surface-300 text-sm">No plugins installed</p>
      <p class="text-surface-400 text-xs mt-1">Place plugins in /data/plugins-runtime/ on your device</p>
    </div>
  {:else if plugins}
    {#each plugins as plugin}
      <div class="card p-4">
        <div class="flex items-center justify-between gap-3">
          <button
            class="min-w-0 flex-1 text-left"
            onclick={() => expandedPlugin = expandedPlugin === plugin.id ? null : plugin.id}
          >
            <div class="flex items-center gap-2">
              <span class="text-sm font-medium text-surface-100">{plugin.name}</span>
              <span class="text-[10px] px-1.5 py-0.5 rounded bg-surface-600 text-surface-300">{plugin.type}</span>
              {#if plugin.version}
                <span class="text-[10px] text-surface-300">{plugin.version}</span>
              {/if}
              {#if plugin.enabled && plugin.processes?.length}
                {#each plugin.processes as proc}
                  <span class="text-[10px] px-1.5 py-0.5 rounded {proc.running ? 'bg-engage-green/15 text-engage-green' : 'bg-surface-700 text-surface-400'}">{proc.running ? 'running' : 'stopped'}</span>
                {/each}
              {/if}
              {#if (plugin.panel || plugin.settings?.length) && plugin.enabled}
                <ChevronIcon rotated={expandedPlugin === plugin.id} />
              {/if}
            </div>
            {#if plugin.description}
              <p class="text-xs text-surface-300 mt-1">{plugin.description}</p>
            {/if}
            {#if plugin.author}
              <p class="text-[10px] text-surface-400 mt-0.5">by {plugin.author}</p>
            {/if}
            {#if plugin.dependencies?.length > 0}
              <div class="flex items-center gap-1 mt-1.5">
                <span class="text-[10px] text-surface-400">deps:</span>
                {#each plugin.dependencies as dep}
                  {@const depPlugin = plugins?.find(p => p.id === dep)}
                  <span class="text-[10px] px-1 py-0.5 rounded {depPlugin?.enabled ? 'bg-surface-700 text-surface-300' : 'bg-engage-red/15 text-engage-red'}">{dep}</span>
                {/each}
              </div>
            {/if}
          </button>
          <Toggle
            checked={plugin.enabled}
            disabled={plugin.locked || togglingPlugin === plugin.id}
            label={plugin.name}
            onCheckedChange={() => handleToggle(plugin.id)}
          />
        </div>

        {#if expandedPlugin === plugin.id && plugin.enabled}
          {#if plugin.id === 'model_selector' && plugin.panel}
            <div class="pt-4 border-t border-surface-700 mt-4">
              <ModelPanel />
            </div>
          {/if}
          {#if plugin.id === 'mapd'}
            <div class="pt-4 border-t border-surface-700 mt-4">
              <MapdPanel />
            </div>
          {/if}
          {#if plugin.settings?.length}
            <div class="pt-4 border-t border-surface-700 mt-4 space-y-4">
              {#each plugin.settings as setting}
                {@const pluginDepDisabled = setting.requiresPlugin ? !plugins?.find(p => p.id === setting.requiresPlugin)?.enabled : false}
                {#if setting.type === 'bool'}
                  <div class="flex items-center justify-between gap-4 {pluginDepDisabled ? 'opacity-40' : ''}">
                    <div>
                      <div class="text-sm text-surface-100">{setting.label}</div>
                      <div class="text-xs text-surface-300 mt-0.5">{setting.desc}{#if pluginDepDisabled} (requires {plugins?.find(p => p.id === setting.requiresPlugin)?.name || setting.requiresPlugin}){/if}</div>
                    </div>
                    <Toggle
                      checked={paramValues[plugin.id]?.[setting.key] ?? false}
                      disabled={pluginDepDisabled || savingParam === setting.key}
                      label={setting.label}
                      onCheckedChange={() => handleParamToggle(plugin.id, setting.key)}
                    />
                  </div>
                {:else if setting.type === 'pills'}
                  {@const depDisabled = pluginDepDisabled || (setting.dependsOn ? !paramValues[plugin.id]?.[setting.dependsOn] : false)}
                  <div class="{depDisabled ? 'opacity-40 pointer-events-none' : ''}">
                    <div class="flex items-center gap-2">
                      <div class="text-sm text-surface-100">{setting.label}</div>
                      <div class="flex-1"></div>
                      {#each setting.options as opt, i}
                        {@const val = typeof opt === 'string' ? i : opt}
                        {@const active = (paramValues[plugin.id]?.[setting.key] ?? -1) === val}
                        <button
                          class="px-2.5 py-1 text-xs rounded-full transition-colors {active ? 'bg-engage-blue/20 text-engage-blue border border-engage-blue/40' : 'bg-surface-700 text-surface-300 border border-surface-600 hover:border-surface-500'}"
                          disabled={depDisabled}
                          onclick={() => handleParamPills(plugin.id, setting.key, val)}
                        >
                          {opt}{setting.suffix || ''}
                        </button>
                      {/each}
                    </div>
                    {#if setting.desc}<div class="text-xs text-surface-300 mt-0.5">{setting.desc}</div>{/if}
                  </div>
                {/if}
              {/each}
            </div>
          {/if}
        {/if}
      </div>
    {/each}
  {/if}
</div>
