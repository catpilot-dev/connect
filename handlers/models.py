import asyncio
import json
import logging
import subprocess
from pathlib import Path

from aiohttp import web

from handler_helpers import error_response, parse_json, read_param

logger = logging.getLogger("connect")

# ─── Model management ─────────────────────────────────────────────────

from config import PYTHON_BIN, MODELS_DIR, PLUGINS_RUNTIME_DIR

MODELS_BASE = Path(MODELS_DIR)
PLUGINS_DIR = Path(PLUGINS_RUNTIME_DIR)
PLUGIN_ID = "model_selector"


def _plugin_dir() -> Path | None:
    """Return plugin directory if installed and enabled, else None."""
    d = PLUGINS_DIR / PLUGIN_ID
    if d.is_dir() and not (d / ".disabled").exists():
        return d
    return None


def _find_script(name: str) -> Path | None:
    """Find a model management script — plugin dir first, then local fallback."""
    d = _plugin_dir()
    if d:
        s = d / name
        if s.exists():
            return s
    # Fallback for local development (script next to handlers/)
    s = Path(__file__).parent.parent / name
    return s if s.exists() else None

_model_download_task = None  # track background download {proc, model_id, type, status}


def _read_model_info(model_dir: Path) -> dict | None:
    """Read model_info.json from a model directory."""
    info_file = model_dir / "model_info.json"
    if info_file.exists():
        try:
            return json.loads(info_file.read_text())
        except Exception:
            return None
    return None


def _read_active_model(model_type: str) -> dict:
    """Read active model file. Returns {'id': ..., 'name': ...}. Handles JSON and plain text."""
    try:
        raw = (MODELS_BASE / f"active_{model_type}_model").read_text().strip()
    except Exception:
        return {"id": "", "name": ""}
    try:
        data = json.loads(raw)
        return {"id": data.get("id", raw), "name": data.get("name", data.get("id", raw))}
    except (json.JSONDecodeError, AttributeError):
        # Legacy plain text format — id only, need to look up name
        info = _read_model_info(MODELS_BASE / model_type / raw)
        name = info.get("name", raw) if info else raw
        return {"id": raw, "name": name}


def _list_installed_models(model_type: str) -> list[dict]:
    """List installed models of given type by reading filesystem directly."""
    type_dir = MODELS_BASE / model_type
    if not type_dir.is_dir():
        return []

    models = []
    for d in sorted(type_dir.iterdir()):
        if not d.is_dir():
            continue
        info = _read_model_info(d)
        name = info.get("name", d.name) if info else d.name
        date = info.get("date", "") if info else ""

        # Check for ONNX and PKL files
        onnx_files = list(d.glob("*.onnx"))
        pkl_files = list(d.glob("*.pkl"))

        # Truncate long names to prevent UI overflow
        if len(name) > 30:
            name = name[:28] + "..."

        models.append({
            "id": d.name,
            "name": name,
            "date": date,
            "has_onnx": len(onnx_files) > 0,
            "has_pkl": len(pkl_files) > 0,
            "onnx_count": len(onnx_files),
            "pkl_count": len(pkl_files),
        })

    # Newest first — sort by date descending (empty dates last)
    models.sort(key=lambda m: m["date"] or "", reverse=True)
    return models


def _require_plugin():
    """Raise 503 if model_selector plugin is not installed/enabled."""
    if not _plugin_dir():
        raise web.HTTPServiceUnavailable(
            text=json.dumps({"error": "model_selector plugin not installed or disabled"}))


async def handle_models_active(request: web.Request) -> web.Response:
    """GET /v1/models/active — lightweight: just active model IDs and names."""
    _require_plugin()
    result = {}
    for model_type in ("driving", "dm"):
        active = _read_active_model(model_type)
        result[f"active_{model_type}"] = active["id"]
        result[f"active_{model_type}_name"] = active["name"]
    return web.json_response(result)


async def handle_models_list(request: web.Request) -> web.Response:
    """GET /v1/models — list installed models and active model IDs."""
    _require_plugin()
    loop = asyncio.get_event_loop()
    driving = await loop.run_in_executor(None, _list_installed_models, "driving")
    dm = await loop.run_in_executor(None, _list_installed_models, "dm")

    # Read active model IDs
    active_driving = _read_active_model("driving")["id"]
    active_dm = _read_active_model("dm")["id"]

    # Include download task status if active
    download_status = None
    global _model_download_task
    if _model_download_task:
        proc = _model_download_task.get("proc")
        if proc and proc.poll() is None:
            download_status = {
                "model_id": _model_download_task["model_id"],
                "type": _model_download_task["type"],
                "status": "downloading",
            }
        elif proc:
            download_status = {
                "model_id": _model_download_task["model_id"],
                "type": _model_download_task["type"],
                "status": "complete" if proc.returncode == 0 else "error",
            }

    return web.json_response({
        "driving": driving,
        "dm": dm,
        "active_driving": active_driving,
        "active_dm": active_dm,
        "download": download_status,
    })


async def handle_models_swap(request: web.Request) -> web.Response:
    """POST /v1/models/swap — swap active model via model_swapper.py."""
    _require_plugin()
    body = await parse_json(request)

    model_type = body.get("type")
    model_id = body.get("model_id")
    if model_type not in ("driving", "dm") or not model_id:
        raise web.HTTPBadRequest(text=json.dumps({"error": "Need type (driving|dm) and model_id"}))

    swapper_script = _find_script("model_swapper.py")
    if not swapper_script:
        raise web.HTTPServiceUnavailable(text=json.dumps({"error": "model_swapper.py not found"}))

    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(None, lambda: subprocess.run(
            [PYTHON_BIN, str(swapper_script), "--type", model_type, "swap", model_id],
            capture_output=True, text=True, timeout=60,
        ))
    except subprocess.TimeoutExpired:
        raise web.HTTPGatewayTimeout(text=json.dumps({"error": "Swap timed out"}))

    if result.returncode != 0:
        error_msg = result.stderr.strip() or result.stdout.strip() or "Swap failed"
        return error_response(error_msg, 500)

    # Parse JSON output from swap command
    try:
        swap_result = json.loads(result.stdout)
    except json.JSONDecodeError:
        swap_result = {"output": result.stdout.strip(), "status": "ok"}

    return web.json_response(swap_result)


async def handle_models_check_updates(request: web.Request) -> web.Response:
    """POST /v1/models/check-updates — update registry then check for new models."""
    _require_plugin()
    download_script = _find_script("model_download.py")
    if not download_script:
        raise web.HTTPServiceUnavailable(
            text=json.dumps({"error": "model_download.py not found"}))

    loop = asyncio.get_event_loop()

    # Step 1: update registry from GitHub
    try:
        await loop.run_in_executor(None, lambda: subprocess.run(
            [PYTHON_BIN, str(download_script), "update-registry"],
            capture_output=True, text=True, timeout=120,
        ))
    except subprocess.TimeoutExpired:
        logger.warning("Registry update timed out, continuing with existing registry")

    # Step 2: check for available (not installed) models
    try:
        result = await loop.run_in_executor(None, lambda: subprocess.run(
            [PYTHON_BIN, str(download_script), "check-updates"],
            capture_output=True, text=True, timeout=30,
        ))
    except subprocess.TimeoutExpired:
        raise web.HTTPGatewayTimeout(text=json.dumps({"error": "Check updates timed out"}))

    if result.returncode != 0:
        return error_response(result.stderr.strip() or "Check failed", 500)

    try:
        updates = json.loads(result.stdout)
    except json.JSONDecodeError:
        updates = {"driving": [], "dm": [], "total": 0}

    # Newest first, truncate long names
    for key in ("driving", "dm"):
        if key in updates and isinstance(updates[key], list):
            for m in updates[key]:
                name = m.get("name", "")
                if len(name) > 30:
                    m["name"] = name[:28] + "..."
            updates[key].sort(key=lambda m: m.get("date", ""), reverse=True)

    return web.json_response(updates)


async def handle_models_download(request: web.Request) -> web.Response:
    """POST /v1/models/download — start downloading a model in background."""
    _require_plugin()
    global _model_download_task

    body = await parse_json(request)

    model_type = body.get("type")
    model_id = body.get("model_id")
    if model_type not in ("driving", "dm") or not model_id:
        raise web.HTTPBadRequest(text=json.dumps({"error": "Need type (driving|dm) and model_id"}))

    download_script = _find_script("model_download.py")
    if not download_script:
        raise web.HTTPServiceUnavailable(
            text=json.dumps({"error": "model_download.py not found"}))

    # Check if already downloading
    if _model_download_task:
        proc = _model_download_task.get("proc")
        if proc and proc.poll() is None:
            return web.json_response({
                "error": f"Already downloading {_model_download_task['model_id']}",
            }, status=409)

    # Launch download as background subprocess
    proc = subprocess.Popen(
        [PYTHON_BIN, str(download_script), "download", "--type", model_type, model_id],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )

    _model_download_task = {
        "proc": proc,
        "model_id": model_id,
        "type": model_type,
    }

    return web.json_response({"status": "downloading", "model_id": model_id, "type": model_type})
