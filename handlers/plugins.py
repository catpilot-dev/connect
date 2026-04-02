"""Plugin management handlers.

Proxies plugin listing and toggle operations through plugind's REST API
(localhost:8083) when available. Falls back to direct filesystem scanning
when plugind isn't running (e.g. during initial setup or offroad reboot).

Repo management (clone/pull/install) is COD-specific and always direct.
"""
import asyncio
import json
import logging
import os
import urllib.request
import urllib.error

from aiohttp import web

from config import (PLUGINS_RUNTIME_DIR, PLUGINS_REPO_DIR, OPENPILOT_DIR,
                     BUILD_HASH_FILE, PLUGIND_API_URL)
from handler_helpers import error_response, read_param, write_param, read_plugin_param, write_plugin_param
from .params import MAPD_PARAM_KEYS, update_mapd_settings

logger = logging.getLogger("connect")

PIDS_DIR = os.path.join(PLUGINS_RUNTIME_DIR, '.pids')
IS_C3 = os.path.exists('/TICI')
DEFAULT_PLUGIN_REPO_URL = 'https://github.com/catpilot-dev/plugins'

# Display sort order for plugin list
_SORT_ORDER = {
  'model_selector': -3, 'lane_centering': -2, 'speedlimitd': -1,
  'mapd': 0, 'bmw_e9x_e8x': 1, 'c3_compat': 2,
}


# ── Plugind API proxy ─────────────────────────────────────────────


def _plugind_get(path: str) -> list | dict | None:
  """GET from plugind's internal API. Returns parsed JSON or None on failure."""
  try:
    req = urllib.request.Request(f"{PLUGIND_API_URL}{path}")
    with urllib.request.urlopen(req, timeout=2) as resp:
      return json.loads(resp.read())
  except Exception:
    return None


def _plugind_put(path: str) -> dict | None:
  """PUT to plugind's internal API. Returns parsed JSON or None on failure."""
  try:
    req = urllib.request.Request(f"{PLUGIND_API_URL}{path}", method='PUT')
    with urllib.request.urlopen(req, timeout=5) as resp:
      return json.loads(resp.read())
  except Exception:
    return None


def _enrich_from_plugind(plugind_plugins: list) -> list:
  """Transform plugind's get_status() response into COD's richer format.

  plugind returns: id, name, version, type, enabled, loaded, error,
                   hooks, params, dependencies, conflicts, device_filter
  COD frontend expects: + description, author, locked, panel, settings, processes
  """
  plugins = []
  for p in plugind_plugins:
    plugin_id = p['id']
    plugin_dir = os.path.join(PLUGINS_RUNTIME_DIR, plugin_id)

    # Read manifest for fields plugind doesn't expose
    manifest = _read_manifest(plugin_dir)

    # Device filter check
    device_filter = p.get('device_filter')
    if device_filter:
      device_type = 'tici' if IS_C3 else 'unknown'
      if device_type not in device_filter:
        continue

    locked = os.path.exists(os.path.join(plugin_dir, '.enforced'))

    plugins.append({
      'id': plugin_id,
      'name': p.get('name', plugin_id),
      'version': p.get('version', ''),
      'type': p.get('type', 'plugin'),
      'description': manifest.get('description', ''),
      'author': manifest.get('author', ''),
      'enabled': True if locked else p.get('enabled', False),
      'locked': locked,
      'dependencies': p.get('dependencies', []),
      'panel': manifest.get('panel', False),
      'settings': _read_plugin_params(plugin_id, manifest),
      'processes': _get_process_status(manifest) if p.get('enabled') else [],
    })

  plugins.sort(key=lambda p: (_SORT_ORDER.get(p['id'], 0), p['id']))
  return plugins


# ── Filesystem fallback (when plugind is not running) ──────────────


def _read_manifest(plugin_dir: str) -> dict:
  """Read plugin.json manifest from a plugin directory."""
  manifest_path = os.path.join(plugin_dir, 'plugin.json')
  try:
    with open(manifest_path) as f:
      return json.load(f)
  except (json.JSONDecodeError, OSError, FileNotFoundError):
    return {}


def _check_process_running(pid_name):
  """Check if a plugin process is running by reading its PID file."""
  pid_file = os.path.join(PIDS_DIR, f'{pid_name}.pid')
  try:
    with open(pid_file) as f:
      pid = int(f.read().strip())
    os.kill(pid, 0)
    with open(f'/proc/{pid}/status') as f:
      for line in f:
        if line.startswith('State:'):
          return 'Z' not in line
    return True
  except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError, OSError):
    return False


def _get_process_status(manifest):
  """Return list of {name, running} for each declared process."""
  processes = manifest.get('processes', [])
  if not processes:
    return []
  return [
    {'name': p['name'], 'running': _check_process_running(p['name'])}
    for p in processes
  ]


def _read_plugin_params(plugin_id, manifest):
  """Read UI-visible params (those with a 'desc' field) and their current values."""
  params_def = manifest.get('params', {})

  settings = []
  for key, meta in params_def.items():
    if 'desc' not in meta:
      continue

    entry = {
      'key': key,
      'type': meta.get('type', 'string'),
      'label': meta.get('label', key),
      'desc': meta['desc'],
    }
    if 'options' in meta:
      entry['options'] = meta['options']
    if 'suffix' in meta:
      entry['suffix'] = meta['suffix']
    if 'dependsOn' in meta:
      entry['dependsOn'] = meta['dependsOn']
    if 'requiresPlugin' in meta:
      entry['requiresPlugin'] = meta['requiresPlugin']

    raw = read_plugin_param(plugin_id, key)

    ptype = meta.get('type', 'string')
    if ptype == 'bool':
      entry['value'] = raw == "1"
    elif ptype == 'pills':
      try:
        entry['value'] = int(raw) if raw else meta.get('default', 0)
      except ValueError:
        entry['value'] = meta.get('default', 0)
    else:
      entry['value'] = raw

    settings.append(entry)

  return settings


def _scan_plugins_filesystem():
  """Scan plugins runtime dir directly. Used when plugind API is unavailable."""
  if not os.path.isdir(PLUGINS_RUNTIME_DIR):
    return []

  plugins = []
  for name in sorted(os.listdir(PLUGINS_RUNTIME_DIR)):
    plugin_dir = os.path.join(PLUGINS_RUNTIME_DIR, name)
    if not os.path.isdir(plugin_dir):
      continue
    manifest = _read_manifest(plugin_dir)
    if not manifest:
      continue

    device_filter = manifest.get('device_filter')
    if device_filter:
      device_type = 'tici' if IS_C3 else 'unknown'
      if device_type not in device_filter:
        continue

    locked = os.path.exists(os.path.join(plugin_dir, '.enforced'))
    enabled = True if locked else not os.path.exists(os.path.join(plugin_dir, '.disabled'))

    plugins.append({
      'id': name,
      'name': manifest.get('name', name),
      'version': manifest.get('version', ''),
      'type': manifest.get('type', 'plugin'),
      'description': manifest.get('description', ''),
      'author': manifest.get('author', ''),
      'enabled': enabled,
      'locked': locked,
      'dependencies': manifest.get('dependencies', []),
      'panel': manifest.get('panel', False),
      'settings': _read_plugin_params(name, manifest),
      'processes': _get_process_status(manifest) if enabled else [],
    })

  plugins.sort(key=lambda p: (_SORT_ORDER.get(p['id'], 0), p['id']))
  return plugins


# ── Handlers ───────────────────────────────────────────────────────


async def handle_plugins_get(request: web.Request) -> web.Response:
  """GET /v1/plugins — list plugins, proxying through plugind when available."""
  # Try plugind API first (authoritative source of enabled/loaded state)
  plugind_data = _plugind_get('/v1/plugins')
  if plugind_data is not None and isinstance(plugind_data, list):
    plugins = _enrich_from_plugind(plugind_data)
  else:
    plugins = _scan_plugins_filesystem()
  return web.json_response(plugins)


async def handle_plugin_toggle(request: web.Request) -> web.Response:
  """POST /v1/plugins/{plugin_id}/toggle — toggle enable/disable.

  Tries plugind API first for immediate hot-reload effect.
  Falls back to filesystem marker if plugind is unavailable.
  """
  plugin_id = request.match_info['plugin_id']
  plugin_dir = os.path.join(PLUGINS_RUNTIME_DIR, plugin_id)

  if not os.path.isdir(plugin_dir):
    return error_response(f"Plugin '{plugin_id}' not found", 404)
  if not os.path.exists(os.path.join(plugin_dir, 'plugin.json')):
    return error_response(f"Plugin '{plugin_id}' has no plugin.json", 400)

  # Determine current state and desired state
  marker = os.path.join(plugin_dir, '.disabled')
  currently_disabled = os.path.exists(marker)
  want_enable = currently_disabled

  # Try plugind API for immediate effect
  if want_enable:
    result = _plugind_put(f'/v1/plugins/{plugin_id}/enable')
  else:
    result = _plugind_put(f'/v1/plugins/{plugin_id}/disable')

  if result is not None:
    enabled = result.get('enabled', want_enable)
  else:
    # Fallback: toggle filesystem marker directly
    if currently_disabled:
      os.remove(marker)
      enabled = True
    else:
      with open(marker, 'w') as f:
        f.write('')
      enabled = False

  # Force builder rebuild on next boot
  try:
    os.remove(BUILD_HASH_FILE)
  except FileNotFoundError:
    pass

  return web.json_response({
    'id': plugin_id,
    'enabled': enabled,
    'reboot_required': True,
  })


async def handle_plugin_param(request: web.Request) -> web.Response:
  """POST /v1/plugins/{plugin_id}/param — set a plugin param {key, value}."""
  plugin_id = request.match_info['plugin_id']
  plugin_dir = os.path.join(PLUGINS_RUNTIME_DIR, plugin_id)

  if not os.path.isdir(plugin_dir):
    return error_response(f"Plugin '{plugin_id}' not found", 404)

  manifest = _read_manifest(plugin_dir)
  if not manifest:
    return error_response(f"Plugin '{plugin_id}' has no valid plugin.json", 400)

  try:
    body = await request.json()
  except Exception:
    return error_response("Invalid JSON body", 400)

  key = body.get("key")
  value = body.get("value")
  params_def = manifest.get("params", {})

  if key not in params_def:
    return error_response(f"Unknown param: {key}", 400)

  meta = params_def[key]
  ptype = meta.get("type", "string")
  if ptype == "bool":
    raw = "1" if value else "0"
  elif ptype in ("int", "pills"):
    raw = str(int(value))
  else:
    raw = str(value)

  write_plugin_param(plugin_id, key, raw)

  # Sync MapdSettings JSON if this is a mapd param
  if key in MAPD_PARAM_KEYS:
    update_mapd_settings()

  return web.json_response({"status": "ok", "key": key, "value": value})


# ── Plugin repo management ──────────────────────────────────────────


async def handle_plugin_repo_get(request: web.Request) -> web.Response:
  """GET /v1/plugins/repo — return repo URL, installed status, last updated time."""
  url = read_param('PluginRepoUrl') or DEFAULT_PLUGIN_REPO_URL
  installed = os.path.isdir(os.path.join(PLUGINS_REPO_DIR, '.git'))
  last_updated = None
  if installed:
    try:
      git_fetch_head = os.path.join(PLUGINS_REPO_DIR, '.git', 'FETCH_HEAD')
      git_head = os.path.join(PLUGINS_REPO_DIR, '.git', 'HEAD')
      ref = git_fetch_head if os.path.exists(git_fetch_head) else git_head
      last_updated = os.path.getmtime(ref)
    except OSError:
      pass
  return web.json_response({
    'url': url,
    'installed': installed,
    'last_updated': last_updated,
  })


async def handle_plugin_repo_set(request: web.Request) -> web.Response:
  """POST /v1/plugins/repo — set the plugin repo URL param."""
  try:
    body = await request.json()
  except Exception:
    return error_response("Invalid JSON body", 400)

  url = body.get('url', '').strip()
  if not url:
    return error_response("Missing 'url' field", 400)

  write_param('PluginRepoUrl', url)
  return web.json_response({'status': 'ok', 'url': url})


async def handle_plugin_repo_install(request: web.Request) -> web.Response:
  """POST /v1/plugins/repo/install — git clone/pull + run install.sh."""
  url = read_param('PluginRepoUrl') or DEFAULT_PLUGIN_REPO_URL

  try:
    is_clone = False
    if os.path.isdir(os.path.join(PLUGINS_REPO_DIR, '.git')):
      proc = await asyncio.create_subprocess_exec(
        'git', '-C', PLUGINS_REPO_DIR, 'rev-parse', 'HEAD',
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
      )
      stdout, _ = await proc.communicate()
      head_before = stdout.decode().strip()

      logger.info("Plugin repo: fetching %s", url)
      proc = await asyncio.create_subprocess_exec(
        'git', '-C', PLUGINS_REPO_DIR, 'fetch', 'origin',
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
      )
      stdout, _ = await proc.communicate()
      git_output = stdout.decode(errors='replace')
      if proc.returncode != 0:
        logger.error("Plugin repo git fetch failed: %s", git_output)
        return web.json_response({
          'status': 'error',
          'output': git_output,
          'reboot_required': False,
        }, status=500)

      proc = await asyncio.create_subprocess_exec(
        'git', '-C', PLUGINS_REPO_DIR, 'reset', '--hard', 'origin/main',
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
      )
    else:
      is_clone = True
      head_before = None
      logger.info("Plugin repo: cloning %s", url)
      proc = await asyncio.create_subprocess_exec(
        'git', 'clone', '--depth=1', url, PLUGINS_REPO_DIR,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
      )
    stdout, _ = await proc.communicate()
    git_output = stdout.decode(errors='replace')

    if proc.returncode != 0:
      logger.error("Plugin repo git failed: %s", git_output)
      return web.json_response({
        'status': 'error',
        'output': git_output,
        'reboot_required': False,
      }, status=500)

    install_script = os.path.join(PLUGINS_REPO_DIR, 'install.sh')
    if not os.path.isfile(install_script):
      return web.json_response({
        'status': 'error',
        'output': 'install.sh not found in plugin repo',
        'reboot_required': False,
      }, status=500)

    logger.info("Plugin repo: running install.sh --target %s", OPENPILOT_DIR)
    proc = await asyncio.create_subprocess_exec(
      'bash', install_script, '--target', OPENPILOT_DIR,
      stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()
    install_output = stdout.decode(errors='replace')

    if proc.returncode != 0:
      logger.error("Plugin install.sh failed: %s", install_output)
      return web.json_response({
        'status': 'error',
        'output': install_output,
        'reboot_required': False,
      }, status=500)

    proc = await asyncio.create_subprocess_exec(
      'git', '-C', PLUGINS_REPO_DIR, 'rev-parse', 'HEAD',
      stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    head_after = stdout.decode().strip()
    changed = is_clone or head_before != head_after

    if changed:
      try:
        os.remove(BUILD_HASH_FILE)
      except FileNotFoundError:
        pass

    combined = git_output + '\n' + install_output
    logger.info("Plugin repo install complete (changed=%s)", changed)
    return web.json_response({
      'status': 'ok',
      'output': combined.strip() if changed else 'Already up to date.',
      'reboot_required': changed,
    })

  except Exception as e:
    logger.exception("Plugin repo install error")
    return web.json_response({
      'status': 'error',
      'output': str(e),
      'reboot_required': False,
    }, status=500)
