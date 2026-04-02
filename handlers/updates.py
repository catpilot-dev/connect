"""Update checking for COD and plugins.

COD:     release-based — compares local VERSION against latest GitHub release.
Plugins: git-based     — compares HEAD against origin/main.
"""
import asyncio
import logging
import os
import shutil
import sys
import tarfile
import tempfile
import time

import aiohttp
from aiohttp import web

logger = logging.getLogger("connect")

from config import (COD_DIR, PLUGINS_REPO_DIR as PLUGIN_REPO_DIR, OPENPILOT_DIR,
                     BUILD_HASH_FILE)

VERSION_FILE = os.path.join(COD_DIR, 'VERSION')
GITHUB_REPO = 'catpilot-dev/connect-on-device'
GITHUB_API = f'https://api.github.com/repos/{GITHUB_REPO}/releases/latest'

CACHE_TTL = 600  # 10 minutes
FETCH_TIMEOUT = 15  # seconds

_cache = {"result": None, "timestamp": 0}


# ─── Version helpers ─────────────────────────────────────────────────

def _read_local_version():
    """Read local COD version from VERSION file."""
    try:
        return open(VERSION_FILE).read().strip()
    except FileNotFoundError:
        return '0.0.0'


def _parse_version(tag):
    """Strip leading 'v' and return version string."""
    return tag.strip().lstrip('v')


# ─── COD: GitHub release check ──────────────────────────────────────

async def _check_cod_release():
    """Check GitHub releases API for a newer COD version.

    Returns status dict or None on failure.
    """
    local = _read_local_version()
    try:
        timeout = aiohttp.ClientTimeout(total=FETCH_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(GITHUB_API) as resp:
                if resp.status != 200:
                    # No releases, rate-limited, or network error
                    return {'available': False, 'current': local, 'latest': local, 'summary': ''}
                data = await resp.json()
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return {'available': False, 'current': local, 'latest': local, 'summary': ''}

    latest = _parse_version(data.get('tag_name', ''))
    if not latest:
        return {'available': False, 'current': local, 'latest': local, 'summary': ''}

    available = latest != local

    # Find the tarball asset (cod-v<version>.tar.gz)
    download_url = None
    for asset in data.get('assets', []):
        if asset['name'].endswith('.tar.gz'):
            download_url = asset['browser_download_url']
            break
    # Fallback to GitHub auto-generated source tarball
    if not download_url:
        download_url = data.get('tarball_url')

    return {
        'available': available,
        'current': local,
        'latest': latest,
        'summary': data.get('body', '').strip()[:500],
        'download_url': download_url,
        'tag': data.get('tag_name', ''),
    }


# ─── Plugins: git-based check ───────────────────────────────────────

async def _git_rev_parse(repo_dir, ref='HEAD'):
    """Get short commit hash for a ref."""
    proc = await asyncio.create_subprocess_exec(
        'git', '-C', repo_dir, 'rev-parse', '--short', ref,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return None
    return stdout.decode().strip()


async def _git_current_branch(repo_dir):
    """Get the current branch name."""
    proc = await asyncio.create_subprocess_exec(
        'git', '-C', repo_dir, 'rev-parse', '--abbrev-ref', 'HEAD',
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return 'main'
    return stdout.decode().strip() or 'main'


async def _git_fetch(repo_dir, timeout=FETCH_TIMEOUT):
    """Fetch from origin with timeout. Returns True on success."""
    try:
        proc = await asyncio.create_subprocess_exec(
            'git', '-C', repo_dir, 'fetch', 'origin',
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode == 0
    except asyncio.TimeoutError:
        proc.kill()
        return False


async def _git_log_summary(repo_dir, branch, max_lines=5):
    """Get oneline log of commits between HEAD and origin/<branch>."""
    proc = await asyncio.create_subprocess_exec(
        'git', '-C', repo_dir, 'log', '--oneline', f'-{max_lines}', f'HEAD..origin/{branch}',
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return ''
    return stdout.decode().strip()


async def _get_target_branch():
    """Get the target branch for plugins — aligned to catpilot's branch."""
    if os.path.isdir(os.path.join(OPENPILOT_DIR, '.git')):
        catpilot_branch = await _git_current_branch(OPENPILOT_DIR)
        return catpilot_branch
    return await _git_current_branch(PLUGIN_REPO_DIR)


async def _check_plugins():
    """Check plugin git repo for updates. Returns status dict or None."""
    if not os.path.isdir(os.path.join(PLUGIN_REPO_DIR, '.git')):
        return None

    branch = await _get_target_branch()

    if not await _git_fetch(PLUGIN_REPO_DIR):
        current = await _git_rev_parse(PLUGIN_REPO_DIR, 'HEAD')
        return {'available': False, 'current': current, 'latest': current, 'summary': '', 'branch': branch}

    current = await _git_rev_parse(PLUGIN_REPO_DIR, 'HEAD')
    latest = await _git_rev_parse(PLUGIN_REPO_DIR, f'origin/{branch}')

    if not current or not latest:
        return None

    available = current != latest
    summary = await _git_log_summary(PLUGIN_REPO_DIR, branch) if available else ''

    return {
        'available': available,
        'current': current,
        'latest': latest,
        'summary': summary,
        'branch': branch,
    }


# ─── HTTP handlers ───────────────────────────────────────────────────

async def handle_updates_check(request: web.Request) -> web.Response:
    """GET /v1/updates/check — check COD releases and plugin repo for updates."""
    now = time.time()
    if _cache["result"] and (now - _cache["timestamp"]) < CACHE_TTL:
        return web.json_response(_cache["result"])

    cod_task = _check_cod_release()
    plugins_task = _check_plugins()
    cod_result, plugins_result = await asyncio.gather(cod_task, plugins_task)

    # Strip download_url from response (internal use only)
    cod_public = None
    if cod_result:
        cod_public = {k: v for k, v in cod_result.items() if k not in ('download_url', 'tag')}

    result = {
        'cod': cod_public,
        'plugins': plugins_result,
    }
    # Store full result (with download_url) for apply
    _cache["result"] = {
        'cod': cod_result,
        'plugins': plugins_result,
    }
    _cache["timestamp"] = now

    return web.json_response(result)


# ─── COD apply: download + extract release ───────────────────────────

async def _apply_cod_update(release_info):
    """Download release tarball and extract over COD_DIR.

    Returns True on success.
    """
    download_url = release_info.get('download_url')
    if not download_url:
        logger.error("COD update: no download URL in release")
        return False

    try:
        timeout = aiohttp.ClientTimeout(total=120)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(download_url) as resp:
                if resp.status != 200:
                    logger.error("COD download failed: HTTP %d", resp.status)
                    return False
                tarball_bytes = await resp.read()
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logger.error("COD download error: %s", e)
        return False

    # Extract to a temp dir, then copy over
    staging = tempfile.mkdtemp(prefix='cod_update_')
    try:
        tarball_path = os.path.join(staging, 'release.tar.gz')
        with open(tarball_path, 'wb') as f:
            f.write(tarball_bytes)

        with tarfile.open(tarball_path, 'r:gz') as tar:
            tar.extractall(staging)

        # GitHub tarballs extract to a subdirectory (e.g., catpilot-dev-connect-on-device-abc1234/)
        # Custom release assets may extract directly. Find the right root.
        extracted_dirs = [
            d for d in os.listdir(staging)
            if os.path.isdir(os.path.join(staging, d))
        ]
        if len(extracted_dirs) == 1:
            src = os.path.join(staging, extracted_dirs[0])
        else:
            src = staging

        # Copy key files and directories into COD_DIR
        for item in ('handlers', 'static', 'VERSION'):
            src_item = os.path.join(src, item)
            dst_item = os.path.join(COD_DIR, item)
            if not os.path.exists(src_item):
                continue
            if os.path.isdir(src_item):
                if os.path.exists(dst_item):
                    shutil.rmtree(dst_item)
                shutil.copytree(src_item, dst_item)
            else:
                shutil.copy2(src_item, dst_item)

        # Copy Python files at root level
        for f in os.listdir(src):
            if f.endswith('.py'):
                shutil.copy2(os.path.join(src, f), os.path.join(COD_DIR, f))

        logger.info("COD update extracted to %s", COD_DIR)
        return True

    except Exception:
        logger.exception("COD update extraction failed")
        return False
    finally:
        shutil.rmtree(staging, ignore_errors=True)


# ─── Plugin apply: git reset + install.sh ────────────────────────────

async def _apply_plugin_update():
    """Pull plugin updates: align to catpilot branch, reset + run install.sh.

    Returns dict with ok, changed keys.
    """
    head_before = await _git_rev_parse(PLUGIN_REPO_DIR, 'HEAD')
    branch = await _get_target_branch()

    proc = await asyncio.create_subprocess_exec(
        'git', '-C', PLUGIN_REPO_DIR, 'reset', '--hard', f'origin/{branch}',
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return {'ok': False, 'error': stdout.decode(errors='replace')}

    install_script = os.path.join(PLUGIN_REPO_DIR, 'install.sh')
    if os.path.isfile(install_script):
        proc = await asyncio.create_subprocess_exec(
            'bash', install_script, '--target', OPENPILOT_DIR,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            return {'ok': False, 'error': stdout.decode(errors='replace')}

    head_after = await _git_rev_parse(PLUGIN_REPO_DIR, 'HEAD')
    changed = head_before != head_after

    if changed:
        try:
            os.remove(BUILD_HASH_FILE)
        except FileNotFoundError:
            pass

    return {'ok': True, 'changed': changed}


async def handle_updates_apply(request: web.Request) -> web.Response:
    """POST /v1/updates/apply — download COD release and/or pull plugin updates."""
    cod_updated = False
    plugins_updated = False
    reboot_required = False

    # Use internal cache (includes download_url) if fresh, otherwise re-check
    now = time.time()
    if _cache["result"] and (now - _cache["timestamp"]) < CACHE_TTL:
        status = _cache["result"]
    else:
        cod_status = await _check_cod_release()
        plugins_status = await _check_plugins()
        status = {'cod': cod_status, 'plugins': plugins_status}

    # Update COD via release download
    if status.get('cod') and status['cod'].get('available'):
        cod_updated = await _apply_cod_update(status['cod'])

    # Update plugins via git
    if status.get('plugins') and status['plugins'].get('available'):
        result = await _apply_plugin_update()
        if result['ok']:
            plugins_updated = True
            reboot_required = result.get('changed', False)
            logger.info("Plugins updated (reboot_required=%s)", reboot_required)
        else:
            logger.error("Plugin update failed: %s", result.get('error', ''))

    # Clear cache
    _cache["result"] = None
    _cache["timestamp"] = 0

    # Schedule server restart if COD was updated (new code needs re-exec)
    if cod_updated:
        asyncio.get_event_loop().call_later(3, _restart_server)

    return web.json_response({
        'status': 'ok',
        'cod_updated': cod_updated,
        'plugins_updated': plugins_updated,
        'reboot_required': reboot_required,
    })


def _restart_server():
    """Re-exec the server process to pick up COD code changes."""
    logger.info("Restarting COD server after update...")
    os.execv(sys.executable, [sys.executable] + sys.argv)
