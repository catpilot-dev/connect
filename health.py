"""
COD Health Check System.

Runs startup and periodic checks to detect misconfigurations, missing
binaries, stale assets, disk pressure, and other silent failures.
All checks are isolated — a failing check logs a warning but never
crashes the server.

Usage:
    from health import run_startup_checks, run_periodic_checks, get_health_status
"""

import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import config

logger = logging.getLogger("connect.health")

# ─── Check result types ─────────────────────────────────────────────

LEVEL_OK = "ok"
LEVEL_WARN = "warn"
LEVEL_ERROR = "error"


def _check(name: str, level: str, message: str, detail: str = "") -> dict:
    return {"name": name, "level": level, "message": message, "detail": detail}


# ─── Shared state ────────────────────────────────────────────────────

_last_results: list[dict] = []
_last_run: float = 0


# ─── Individual checks ───────────────────────────────────────────────

def check_binary(name: str, path: str) -> dict:
    """Verify a binary exists, is executable, and is the real thing (not a shim)."""
    if not path:
        return _check(name, LEVEL_ERROR, f"{name} path is empty in config")
    if not os.path.isfile(path):
        return _check(name, LEVEL_ERROR, f"{name} not found at {path}")
    if not os.access(path, os.X_OK):
        return _check(name, LEVEL_ERROR, f"{name} not executable at {path}")

    # Detect Python shim wrappers (small text files that import a package)
    try:
        size = os.path.getsize(path)
        if size < 1024:
            with open(path, "r") as f:
                head = f.read(512)
            if "import" in head and ("from " in head or "import " in head):
                return _check(name, LEVEL_ERROR,
                              f"{name} at {path} is a Python shim ({size}B), not a real binary",
                              detail=head[:200])
    except (OSError, UnicodeDecodeError):
        pass  # Binary file — that's what we want

    # Try --version to confirm it works (ffmpeg uses -version, python uses --version)
    try:
        for flag in ("--version", "-version"):
            r = subprocess.run([path, flag], capture_output=True, text=True, timeout=5)
            out = r.stdout.strip() or r.stderr.strip()
            if r.returncode == 0 and out:
                return _check(name, LEVEL_OK, f"{name}: {out.split(chr(10))[0][:120]}")
        return _check(name, LEVEL_OK, f"{name}: {path} (version unknown)")
    except Exception as e:
        return _check(name, LEVEL_WARN, f"{name} exists but version check failed: {e}")


def check_path(name: str, path: str, writable: bool = False) -> dict:
    """Verify a directory exists and is optionally writable."""
    if not path:
        return _check(name, LEVEL_ERROR, f"{name} path is empty in config")
    if not os.path.isdir(path):
        return _check(name, LEVEL_ERROR, f"{name} directory missing: {path}")
    if writable and not os.access(path, os.W_OK):
        return _check(name, LEVEL_ERROR, f"{name} not writable: {path}")
    return _check(name, LEVEL_OK, f"{name}: {path}")


def check_disk_space(path: str, label: str, warn_bytes: int, error_bytes: int) -> dict:
    """Check free disk space at a mount point."""
    try:
        stat = shutil.disk_usage(path)
        free_gb = stat.free / (1024 ** 3)
        total_gb = stat.total / (1024 ** 3)
        pct_free = stat.free / stat.total * 100
        info = f"{free_gb:.1f}GB free of {total_gb:.1f}GB ({pct_free:.0f}%)"
        if stat.free < error_bytes:
            return _check(f"disk_{label}", LEVEL_ERROR, f"{label} critically low: {info}")
        if stat.free < warn_bytes:
            return _check(f"disk_{label}", LEVEL_WARN, f"{label} low: {info}")
        return _check(f"disk_{label}", LEVEL_OK, f"{label}: {info}")
    except OSError as e:
        return _check(f"disk_{label}", LEVEL_ERROR, f"{label} check failed: {e}")


def check_stale_assets(static_dir: str) -> dict:
    """Check for JS/CSS bundles in static/assets/ not referenced by index.html."""
    index_path = os.path.join(static_dir, "index.html")
    assets_dir = os.path.join(static_dir, "assets")

    if not os.path.isfile(index_path):
        return _check("stale_assets", LEVEL_WARN, "index.html not found")
    if not os.path.isdir(assets_dir):
        return _check("stale_assets", LEVEL_OK, "No assets directory")

    try:
        html = Path(index_path).read_text()
        # Extract all asset filenames from src=, href=, and modulepreload links
        referenced = set(re.findall(r'/assets/([\w._-]+(?:\.js|\.css|\.m4s|\.mp4))', html))
        # Also read the main JS bundle to find dynamically imported chunks (e.g. leaflet, hls)
        for js_file in referenced.copy():
            js_path = os.path.join(assets_dir, js_file)
            if js_file.endswith(".js") and os.path.isfile(js_path):
                try:
                    js_content = Path(js_path).read_text()
                    # Match "assets/filename.js" or "assets/filename.css" patterns
                    dynamic = re.findall(r'assets/([\w._-]+\.(?:js|css))', js_content)
                    referenced.update(dynamic)
                except Exception:
                    pass
        on_disk = set(f for f in os.listdir(assets_dir) if f.endswith((".js", ".css")))
        stale = on_disk - referenced

        if stale:
            return _check("stale_assets", LEVEL_WARN,
                          f"{len(stale)} stale asset(s) in static/assets/",
                          detail=", ".join(sorted(stale)[:10]))
        return _check("stale_assets", LEVEL_OK,
                       f"{len(on_disk)} assets, all referenced")
    except Exception as e:
        return _check("stale_assets", LEVEL_WARN, f"Asset check failed: {e}")


def check_stale_shm() -> dict:
    """Check for orphaned /dev/shm/msgq_* directories."""
    try:
        shm = Path("/dev/shm")
        orphans = [p.name for p in shm.glob("msgq_*") if p.is_dir()]
        if orphans:
            return _check("stale_shm", LEVEL_WARN,
                          f"{len(orphans)} orphaned msgq dir(s) in /dev/shm",
                          detail=", ".join(orphans[:5]))
        return _check("stale_shm", LEVEL_OK, "No orphaned shm directories")
    except Exception as e:
        return _check("stale_shm", LEVEL_WARN, f"shm check failed: {e}")


def check_tmp_logs() -> dict:
    """Check for accumulated temp log files."""
    try:
        tmp = Path("/tmp")
        hud_logs = list(tmp.glob("hud_*.log"))
        total_size = sum(f.stat().st_size for f in hud_logs if f.is_file())
        size_mb = total_size / (1024 * 1024)

        connect_log = tmp / "connect.log"
        connect_mb = 0
        if connect_log.exists():
            connect_mb = connect_log.stat().st_size / (1024 * 1024)

        issues = []
        if size_mb > 100:
            issues.append(f"hud logs: {size_mb:.0f}MB across {len(hud_logs)} files")
        if connect_mb > 100:
            issues.append(f"connect.log: {connect_mb:.0f}MB")

        if issues:
            return _check("tmp_logs", LEVEL_WARN,
                          "Large log files in /tmp", detail="; ".join(issues))
        return _check("tmp_logs", LEVEL_OK,
                       f"Logs OK (connect: {connect_mb:.0f}MB, hud: {size_mb:.0f}MB)")
    except Exception as e:
        return _check("tmp_logs", LEVEL_WARN, f"Log check failed: {e}")


def check_manager_process() -> dict:
    """Check if openpilot manager is running."""
    try:
        r = subprocess.run(["pgrep", "-f", "manager.py"], capture_output=True, timeout=3)
        if r.returncode == 0:
            pids = r.stdout.decode().strip().split("\n")
            return _check("manager", LEVEL_OK, f"Manager running (PID {pids[0]})")
        return _check("manager", LEVEL_WARN, "Manager process not found")
    except Exception as e:
        return _check("manager", LEVEL_WARN, f"Manager check failed: {e}")


def check_route_store(store) -> dict:
    """Check route store health — route count, metadata readability."""
    try:
        route_count = len(store._routes) if hasattr(store, "_routes") else 0
        hidden_count = len(store._hidden) if hasattr(store, "_hidden") else 0
        pending = sum(1 for lid in store._raw
                      if lid not in store._metadata) if hasattr(store, "_metadata") else 0

        info = f"{route_count} routes, {hidden_count} hidden, {pending} unenriched"
        if pending > 20:
            return _check("route_store", LEVEL_WARN,
                          f"Many unenriched routes: {info}")
        return _check("route_store", LEVEL_OK, info)
    except Exception as e:
        return _check("route_store", LEVEL_WARN, f"Route store check failed: {e}")


# ─── Check runners ───────────────────────────────────────────────────

def run_startup_checks(static_dir: str = None) -> list[dict]:
    """Run all checks at server startup. Logs results."""
    global _last_results, _last_run

    results = []

    # Binaries
    results.append(check_binary("ffmpeg", config.FFMPEG_BIN))
    results.append(check_binary("python", config.PYTHON_BIN))
    replay = config.REPLAY_BIN
    if os.path.isfile(replay):
        results.append(check_binary("replay", replay))
    else:
        results.append(_check("replay", LEVEL_WARN,
                              f"Replay binary not found at {replay} (HUD rendering unavailable)"))

    # Paths
    results.append(check_path("openpilot", config.OPENPILOT_DIR))
    results.append(check_path("realdata", config.REALDATA_DIR))
    results.append(check_path("params", config.PARAMS_DIR))
    results.append(check_path("cache", config.COD_CACHE_DIR, writable=True))

    # Disk (skip /tmp — it's tmpfs with misleading stats)
    results.append(check_disk_space(config.REALDATA_DIR, "data",
                                    warn_bytes=20 * 1024**3, error_bytes=10 * 1024**3))

    # Stale assets
    if static_dir:
        results.append(check_stale_assets(static_dir))

    # Temp files
    results.append(check_stale_shm())
    results.append(check_tmp_logs())

    _last_results = results
    _last_run = time.time()

    # Log summary
    errors = [r for r in results if r["level"] == LEVEL_ERROR]
    warns = [r for r in results if r["level"] == LEVEL_WARN]
    if errors:
        for r in errors:
            logger.error("Health ERROR: %s — %s", r["name"], r["message"])
    if warns:
        for r in warns:
            logger.warning("Health WARN: %s — %s", r["name"], r["message"])
    if not errors and not warns:
        logger.info("Health: all %d checks passed", len(results))
    else:
        logger.info("Health: %d checks — %d errors, %d warnings, %d ok",
                     len(results), len(errors), len(warns),
                     len(results) - len(errors) - len(warns))

    return results


def run_periodic_checks(store=None, static_dir: str = None) -> list[dict]:
    """Run lightweight periodic checks. Called from background task."""
    global _last_results, _last_run

    results = []

    # Disk space
    results.append(check_disk_space(config.REALDATA_DIR, "data",
                                    warn_bytes=20 * 1024**3, error_bytes=10 * 1024**3))

    # Process health
    results.append(check_manager_process())

    # Temp file accumulation
    results.append(check_stale_shm())
    results.append(check_tmp_logs())

    # Route store
    if store:
        results.append(check_route_store(store))

    # Stale assets
    if static_dir:
        results.append(check_stale_assets(static_dir))

    _last_results = results
    _last_run = time.time()

    # Only log issues
    for r in results:
        if r["level"] == LEVEL_ERROR:
            logger.error("Health ERROR: %s — %s", r["name"], r["message"])
        elif r["level"] == LEVEL_WARN:
            logger.warning("Health WARN: %s — %s", r["name"], r["message"])

    return results


def get_health_status() -> dict:
    """Return current health status for the /health endpoint."""
    errors = [r for r in _last_results if r["level"] == LEVEL_ERROR]
    warns = [r for r in _last_results if r["level"] == LEVEL_WARN]

    if errors:
        status = "unhealthy"
    elif warns:
        status = "degraded"
    else:
        status = "healthy"

    return {
        "status": status,
        "checks": _last_results,
        "last_check": _last_run,
        "errors": len(errors),
        "warnings": len(warns),
    }
