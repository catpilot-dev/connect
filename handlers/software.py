import asyncio
import json
import logging
import os
import re
import shutil
import subprocess

from aiohttp import web

from handler_helpers import error_response, parse_json, read_param, write_param

logger = logging.getLogger("connect")


# ─── Software update management ──────────────────────────────────────

SOFTWARE_PARAMS = [
    "GitBranch", "GitCommit", "GitCommitDate", "GitRemote",
    "UpdaterState", "UpdaterTargetBranch",
    "UpdaterCurrentDescription", "UpdaterNewDescription",
    "UpdaterCurrentReleaseNotes", "UpdaterNewReleaseNotes",
    "UpdaterAvailableBranches",
    "UpdateAvailable", "UpdaterFetchAvailable",
    "LastUpdateTime", "UpdateFailedCount",
    "IsTestedBranch",
]

_SOFTWARE_BOOL_PARAMS = {"UpdateAvailable", "UpdaterFetchAvailable", "IsTestedBranch"}
_SOFTWARE_INT_PARAMS = {"UpdateFailedCount"}


async def handle_software_get(request: web.Request) -> web.Response:
    """GET /v1/software — read all software-related params."""
    result = {}
    for key in SOFTWARE_PARAMS:
        raw = read_param(key)

        if key in _SOFTWARE_BOOL_PARAMS:
            result[key] = raw == "1"
        elif key in _SOFTWARE_INT_PARAMS:
            try:
                result[key] = int(raw) if raw else 0
            except ValueError:
                result[key] = 0
        elif key == "UpdaterAvailableBranches":
            result[key] = [b for b in raw.split(",") if b] if raw else []
        elif key == "GitCommitDate":
            # Raw: "'1770870385 2026-02-12 12:26:25 +0800'" → "2026-02-12 12:26:25"
            m = re.search(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', raw)
            result[key] = m.group(1) if m else raw.strip("'\" ")
        else:
            result[key] = raw

    return web.json_response(result)


async def handle_software_check(request: web.Request) -> web.Response:
    """POST /v1/software/check — send SIGUSR1 to updater to trigger check."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "pkill", "-SIGUSR1", "-f", "system.updated.updated",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(proc.wait(), timeout=5)
    except Exception as e:
        logger.warning("Failed to signal updater for check: %s", e)
    return web.json_response({"status": "checking"})


async def handle_software_download(request: web.Request) -> web.Response:
    """POST /v1/software/download — send SIGHUP to updater to trigger download."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "pkill", "-SIGHUP", "-f", "system.updated.updated",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(proc.wait(), timeout=5)
    except Exception as e:
        logger.warning("Failed to signal updater for download: %s", e)
    return web.json_response({"status": "downloading"})


async def handle_software_install(request: web.Request) -> web.Response:
    """POST /v1/software/install — write DoReboot param to trigger reboot."""
    try:
        write_param("DoReboot", "1")
    except Exception as e:
        logger.error("Failed to set DoReboot: %s", e)
        return error_response(str(e), 500)
    return web.json_response({"status": "rebooting"})


async def handle_software_branch(request: web.Request) -> web.Response:
    """POST /v1/software/branch — set UpdaterTargetBranch and trigger check."""
    body = await parse_json(request)

    branch = body.get("branch", "").strip()
    if not branch:
        raise web.HTTPBadRequest(text=json.dumps({"error": "Missing branch name"}))

    try:
        write_param("UpdaterTargetBranch", branch)
    except Exception as e:
        return error_response(str(e), 500)

    # Trigger a check for the new branch
    try:
        proc = await asyncio.create_subprocess_exec(
            "pkill", "-SIGUSR1", "-f", "system.updated.updated",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(proc.wait(), timeout=5)
    except Exception:
        pass

    return web.json_response({"status": "ok", "branch": branch})


async def handle_software_uninstall(request: web.Request) -> web.Response:
    """POST /v1/software/uninstall — write DoUninstall param."""
    try:
        write_param("DoUninstall", "1")
    except Exception as e:
        logger.error("Failed to set DoUninstall: %s", e)
        return error_response(str(e), 500)
    return web.json_response({"status": "uninstalling"})


# ─── Plugin bootstrap for branch upgrades ────────────────────────────

from config import STAGING_PATHS, PLUGINS_RUNTIME_DIR

PLUGINS_DIR = PLUGINS_RUNTIME_DIR


def _get_device_type() -> str:
    """Detect comma device type from devicetree."""
    try:
        with open("/sys/firmware/devicetree/base/model") as f:
            return f.read().strip('\x00').split('comma ')[-1]
    except FileNotFoundError:
        return "unknown"


async def handle_software_prepare_plugins(request: web.Request) -> web.Response:
    """POST /v1/software/prepare-plugins — copy plugins from staged update, auto-enable c3_compat."""
    staged_plugins = next((p for p in STAGING_PATHS if os.path.isdir(p)), None)
    if not staged_plugins:
        return web.json_response({"status": "no_plugins", "message": "No plugins/ in staged update"})

    # Copy staged plugins to /data/plugins-runtime/
    if os.path.exists(PLUGINS_DIR):
        shutil.rmtree(PLUGINS_DIR)
    shutil.copytree(staged_plugins, PLUGINS_DIR)

    result = {"status": "ok", "plugins_copied": True, "c3_compat_enabled": False}

    # Auto-enable c3_compat for tici (C3 on AGNOS 12.8)
    device_type = _get_device_type()
    c3_compat_dir = os.path.join(PLUGINS_DIR, "c3_compat")
    if device_type == "tici" and os.path.isdir(c3_compat_dir):
        disabled_marker = os.path.join(c3_compat_dir, ".disabled")
        if os.path.exists(disabled_marker):
            os.remove(disabled_marker)
        result["c3_compat_enabled"] = True
        logger.info("Auto-enabled c3_compat for tici device (AGNOS 12.8)")

    # Force plugin builder rebuild on next boot
    try:
        from config import BUILD_HASH_FILE
        os.remove(BUILD_HASH_FILE)
    except FileNotFoundError:
        pass

    # Sync venv packages against local uv.lock (may install missing deps)
    venv_sync_result = await _run_venv_sync()
    if venv_sync_result:
        result["venv_sync"] = venv_sync_result

    logger.info("Plugins prepared: copied from staging, device=%s, c3_compat=%s, venv_sync=%s",
                device_type, result["c3_compat_enabled"],
                venv_sync_result.get("synced") if venv_sync_result else "skipped")
    return web.json_response(result)


# ─── Venv sync ────────────────────────────────────────────────────────

from config import PYTHON_BIN as VENV_SYNC_PYTHON

VENV_SYNC_SCRIPT = os.path.join(PLUGINS_DIR, "c3_compat", "venv_sync.py")


async def _run_venv_sync(check_only: bool = False) -> dict | None:
    """Run venv_sync.py as a subprocess. Returns parsed JSON result or None on error."""
    if not os.path.isfile(VENV_SYNC_SCRIPT):
        logger.debug("venv_sync.py not found at %s, skipping", VENV_SYNC_SCRIPT)
        return None

    cmd = [VENV_SYNC_PYTHON, VENV_SYNC_SCRIPT, "--json"]
    if check_only:
        cmd.append("--check-only")

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=180)
        if stderr:
            logger.info("venv_sync stderr: %s", stderr.decode().strip()[:500])
        if stdout:
            return json.loads(stdout.decode())
    except asyncio.TimeoutError:
        logger.error("venv_sync timed out after 180s")
        return {"error": "timeout"}
    except json.JSONDecodeError:
        logger.error("venv_sync returned invalid JSON: %s", stdout.decode()[:200] if stdout else "")
        return {"error": "invalid json"}
    except Exception as e:
        logger.error("venv_sync failed: %s", e)
        return {"error": str(e)}
    return None


async def handle_venv_sync(request: web.Request) -> web.Response:
    """POST /v1/software/venv-sync — manually trigger venv sync.

    Optional JSON body: {"check_only": true}
    """
    check_only = False

    if request.content_type == "application/json":
        try:
            body = await request.json()
            check_only = body.get("check_only", False)
        except Exception:
            pass

    result = await _run_venv_sync(check_only=check_only)
    if result is None:
        return web.json_response(
            {"error": "venv_sync.py not available (c3_compat plugin not installed)"},
            status=404)

    status = 200 if result.get("synced") or result.get("synced") is None else 200
    return web.json_response(result, status=status)
