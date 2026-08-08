#!/usr/bin/env python3
"""Render openpilot UI HUD to MP4 — headless EGL backend.

Thin wrapper around catpilot's tools/clip/run.py clip() function.
Uses the stock clip pipeline (log loading, SubMaster mocking, VIPC feeding)
with headless rendering via OPENPILOT_UI_NULL_EGL=1.

No DRM master needed — the live openpilot UI continues running.

Requires:
  - Patched raylib .so at /data/connect-on-device/lib/ (GLFW null platform + Adreno GBM/EGL)
  - tools/clip/run.py in openpilot (provides the clip() function)
"""

import argparse
import json
import os
import sys
import signal
import time
from pathlib import Path

from config import FFMPEG_BIN, OPENPILOT_DIR, PYTHON_BIN

signal.signal(signal.SIGTERM, lambda *_: sys.exit(1))


def write_status(status_file: str, data: dict):
    """Atomically write status JSON."""
    tmp = status_file + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.rename(tmp, status_file)
    except Exception:
        pass


def create_symlink_dir(data_dir: str, local_id: str, dongle_id: str, num_segments: int) -> str:
    """Create temp directory with canonical-name symlinks for replay/Route."""
    import tempfile
    tmpdir = tempfile.mkdtemp(prefix="hud_render_headless_")
    for seg in range(num_segments):
        src = os.path.join(data_dir, f"{local_id}--{seg}")
        if not os.path.isdir(src):
            continue
        dst = os.path.join(tmpdir, f"{dongle_id}|{local_id}--{seg}")
        os.symlink(src, dst)
    return tmpdir


def find_max_segment(data_dir: str, local_id: str) -> int:
    """Find the highest segment number for a route."""
    max_seg = -1
    for entry in Path(data_dir).iterdir():
        if entry.name.startswith(f"{local_id}--") and entry.is_dir():
            try:
                max_seg = max(max_seg, int(entry.name.split("--")[-1]))
            except ValueError:
                pass
    return max_seg


def _patch_plugin_bus_for_replay():
    """Monkey-patch PluginSub to read from SubMaster's pluginBusLog instead of
    live IPC sockets. This makes ALL plugins automatically work during rlog replay
    without per-plugin patches.

    The patched PluginSub stores the latest message per topic from pluginBusLog,
    and drain()/recv() return from this cache instead of ZMQ sockets.
    """
    import json as _json
    from openpilot.selfdrive.plugins import plugin_bus

    class ReplayPluginSub:
        """Drop-in replacement for PluginSub that reads from SubMaster pluginBusLog."""

        _cache = {}  # topic → (topic, data_dict), shared across instances
        _last_frame = -1

        def __init__(self, topics):
            self._topics = set(topics)

        @classmethod
        def _refresh_cache(cls):
            """Update cache from SubMaster pluginBusLog if new data available."""
            try:
                from openpilot.selfdrive.ui.ui_state import ui_state
                sm = ui_state.sm
                frame = sm.frame
                if frame == cls._last_frame:
                    return
                cls._last_frame = frame
                if 'pluginBusLog' not in sm.services:
                    return
                bl = sm['pluginBusLog']
                for i in range(len(bl.entries)):
                    e = bl.entries[i]
                    try:
                        data = _json.loads(e.json)
                        cls._cache[e.topic] = (e.topic, data)
                    except Exception:
                        pass
            except Exception:
                pass

        def recv(self):
            self._refresh_cache()
            for t in self._topics:
                if t in self._cache:
                    return self._cache[t]
            return None

        def drain(self, topic=None):
            self._refresh_cache()
            if topic and topic in self._cache:
                return self._cache[topic]
            for t in self._topics:
                if t in self._cache:
                    return self._cache[t]
            return None

        def poll(self, timeout_ms=100):
            return self.recv()

        def close(self):
            pass

    # Replace PluginSub class so all plugins use the replay version
    plugin_bus.PluginSub = ReplayPluginSub
    print("Patched PluginSub for replay (reads from SubMaster pluginBusLog)", file=sys.stderr)

    # Blocking recorder patch is applied in main() where args are in scope


def main():
    parser = argparse.ArgumentParser(description="Render openpilot HUD video (headless EGL)")
    parser.add_argument("--route-name", required=True)
    parser.add_argument("--local-id", required=True)
    parser.add_argument("--dongle-id", required=True)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--start", type=float, default=0)
    parser.add_argument("--end", type=float, default=None)
    parser.add_argument("--screenshot-at", type=float, default=None,
                        help="Route-offset seconds — render a single frame and write a PNG to --output")
    parser.add_argument("--output", required=True)
    parser.add_argument("--status-file", required=True)
    parser.add_argument("--fps", type=int, default=20)
    parser.add_argument("--scale", default=None)
    parser.add_argument("--route-date", default="")
    parser.add_argument("--op-version", default="")
    parser.add_argument("--op-branch", default="")
    parser.add_argument("--op-commit", default="")
    parser.add_argument("--car-fingerprint", default="")
    args = parser.parse_args()

    shot_mode = args.screenshot_at is not None
    if shot_mode:
        # Single-frame PNG extraction: replay a short window ending just past the
        # target so SubMaster state (model, alerts, HUD elements) has settled.
        args.start = max(0, int(args.screenshot_at) - 2)
        args.end = int(args.screenshot_at) + 1
    elif args.end is None:
        write_status(args.status_file, {"status": "error", "error": "--end is required"})
        sys.exit(1)

    duration = args.end - args.start
    if duration <= 0:
        write_status(args.status_file, {"status": "error", "error": "Invalid time range"})
        sys.exit(1)

    # Enable headless EGL rendering
    os.environ["OPENPILOT_UI_NULL_EGL"] = "1"

    # Inject patched raylib .so (with GLFW null platform support)
    # Must be done BEFORE importing pyray/raylib
    patched_lib = "/data/connect-on-device/lib"
    if os.path.isdir(patched_lib):
        # Prepend to sys.path so patched raylib module loads first
        sys.path.insert(0, patched_lib)

    # Setup openpilot path
    if OPENPILOT_DIR not in sys.path:
        sys.path.insert(0, OPENPILOT_DIR)
    os.chdir(OPENPILOT_DIR)

    # Force texture-copy render path instead of EGL images (DMA-BUF).
    # EGL images fail on pbuffer surfaces; texture-copy works everywhere.
    import openpilot.system.hardware as _hw
    _hw.TICI = False

    max_seg = find_max_segment(args.data_dir, args.local_id)
    if max_seg < 0:
        write_status(args.status_file, {"status": "error", "error": "No segments found"})
        sys.exit(1)

    symlink_dir = create_symlink_dir(args.data_dir, args.local_id, args.dongle_id, max_seg + 1)

    os.makedirs(os.path.dirname(args.output), exist_ok=True)

    try:
        write_status(args.status_file, {
            "status": "rendering", "elapsed_sec": 0,
            "total_sec": duration, "phase": "loading",
        })

        # Import clip function (after path setup)
        from tools.lib.route import Route
        from tools.clip.run import clip, setup_env

        canonical_name = f"{args.dongle_id}/{args.local_id}"
        route = Route(canonical_name, data_dir=symlink_dir)

        if shot_mode:
            # No RECORD/ffmpeg — a post_end_drawing hook exports one frame as PNG.
            os.environ["OFFSCREEN"] = "1"
            os.environ["BIG"] = "1"
        else:
            # Set up recording environment (RECORD=1, RECORD_OUTPUT, etc.)
            setup_env(args.output, big=True, headless=True, duration=int(duration))

        # Register plugin hooks — plugins aren't auto-loaded by clip(),
        # only by the full openpilot manager startup.
        # Must register BEFORE ui_state import so SubMaster includes pluginBusLog.
        try:
            sys.path.insert(0, "/data/plugins-runtime")
            from openpilot.selfdrive.plugins.hooks import hooks

            # Load all UI plugin hooks from plugin.json files.
            # Only register hooks needed for onroad rendering.
            # Skip hooks that override stock UI values (rlog data is correct)
            # and settings/home hooks (not visible during recording).
            _ALLOW_HOOKS = {
                "ui.state_subscriptions",   # adds pluginBusLog to SubMaster
                "ui.state_tick",            # updates plugin state each frame
                "ui.render_overlay",        # draws plugin visuals (speed sign, etc.)
                "ui.onroad_exp_button",     # experiment button
            }
            # screen_capture grabs the same render texture as our own recorder
            _SKIP_PLUGINS = {"screen_capture"}

            import json as _json
            _plugins_dir = "/data/plugins-runtime"
            for _pname in sorted(os.listdir(_plugins_dir)):
                _pj = os.path.join(_plugins_dir, _pname, "plugin.json")
                if not os.path.isfile(_pj):
                    continue
                try:
                    _pdata = _json.load(open(_pj))
                    for _hname, _hinfo in _pdata.get("hooks", {}).items():
                        if _hname not in _ALLOW_HOOKS:
                            continue
                        if _pname in _SKIP_PLUGINS:
                            continue
                        _mod = __import__(f"{_pname}.{_hinfo['module']}", fromlist=[_hinfo["function"]])
                        _fn = getattr(_mod, _hinfo["function"])
                        _pri = _hinfo.get("priority", 50)
                        hooks.register(_hname, _pname, _fn, _pri)
                except Exception as _e:
                    print(f"Warning: plugin {_pname} hook load failed: {_e}", file=sys.stderr)

            print(f"Registered {sum(len(v) for v in hooks._hooks.values())} plugin hooks", file=sys.stderr)

            # Monkey-patch plugin bus readers to use SubMaster pluginBusLog
            # instead of live PluginSub sockets (which don't exist during replay)
            _patch_plugin_bus_for_replay()

            if shot_mode:
                # Capture the first frame at/after the target index. Target maps
                # route-offset to render frames the same way clip() pairs camera
                # frames with log chunks: frame i ↔ start + i/20 s.
                _target_idx = min(round((args.screenshot_at - args.start) * 20),
                                  int(duration * 20) - 1)
                _shot = {"done": False}

                def _screenshot_hook(default):
                    if _shot["done"]:
                        return
                    from openpilot.system.ui.lib.application import gui_app
                    import pyray as rl
                    rt = gui_app._render_texture
                    if rt is None or gui_app.frame < _target_idx:
                        return
                    _shot["done"] = True
                    image = rl.load_image_from_texture(rt.texture)
                    rl.image_flip_vertical(image)
                    w, h = image.width, image.height
                    rgba = bytes(rl.ffi.buffer(image.data, w * h * 4))
                    rl.unload_image(image)
                    from PIL import Image as PILImage
                    img = PILImage.frombytes("RGBA", (w, h), rgba).convert("RGB")
                    _tmp = args.output + ".tmp"
                    img.save(_tmp, format="PNG")
                    os.replace(_tmp, args.output)
                    write_status(args.status_file, {"status": "complete", "output": args.output})

                hooks.register("ui.post_end_drawing", "cod_screenshot", _screenshot_hook, 50)
                print(f"Screenshot capture enabled (frame {_target_idx})", file=sys.stderr)
            else:
                # COD owns frame capture — see cod_recorder.
                import cod_recorder as _rec
                _status_file = args.status_file
                _total_duration = duration
                _fps = 20
                _preview_path = os.path.join(os.path.dirname(args.status_file), "preview.jpg")
                # Clean up stale preview from previous render
                for _old in (_preview_path, _preview_path + ".tmp"):
                    try:
                        os.unlink(_old)
                    except FileNotFoundError:
                        pass

                def _capture_hook(default):
                    if not _rec.RECORD:
                        return
                    from openpilot.system.ui.lib.application import gui_app
                    import pyray as rl
                    rt = gui_app._render_texture
                    if rt is None:
                        return
                    if not _rec.is_started():
                        _rec.start(gui_app.width, gui_app.height, gui_app.target_fps)
                    image = rl.load_image_from_texture(rt.texture)
                    data_size = image.width * image.height * 4
                    data = bytes(rl.ffi.buffer(image.data, data_size))
                    rl.unload_image(image)
                    _rec.write_frame(data)

                    frame_num = gui_app.frame
                    elapsed = frame_num / _fps

                    # Write JPEG preview every 10th frame (~2fps)
                    # Use PIL instead of raylib export (more reliable with absolute paths)
                    if frame_num % 10 == 0:
                        try:
                            from PIL import Image as PILImage
                            import io
                            preview = rl.load_image_from_texture(rt.texture)
                            rl.image_flip_vertical(preview)
                            w, h = preview.width, preview.height
                            rgba = bytes(rl.ffi.buffer(preview.data, w * h * 4))
                            rl.unload_image(preview)
                            img = PILImage.frombytes("RGBA", (w, h), rgba).convert("RGB")
                            img = img.resize((960, 480), PILImage.LANCZOS)
                            _tmp_path = _preview_path + ".tmp"
                            img.save(_tmp_path, "JPEG", quality=70)
                            os.rename(_tmp_path, _preview_path)
                        except Exception as _prev_err:
                            import traceback
                            traceback.print_exc(file=sys.stderr)

                    # Update status every second
                    if frame_num % _fps == 0:
                        write_status(_status_file, {
                            "status": "rendering",
                            "elapsed_sec": round(min(elapsed, _total_duration), 1),
                            "total_sec": _total_duration,
                            "phase": "recording",
                        })

                hooks.register("ui.post_end_drawing", "cod_recorder", _capture_hook, 50)
                print("Blocking capture + preview enabled", file=sys.stderr)
        except Exception as e:
            print(f"Warning: plugin hooks not available: {e}", file=sys.stderr)

        # Run the clip renderer
        write_status(args.status_file, {
            "status": "rendering", "elapsed_sec": 0,
            "total_sec": duration, "phase": "recording",
        })

        clip(
            route=route,
            output=args.output,
            start=int(args.start),
            end=int(args.end),
            headless=True,
            big=True,
            # A screenshot must show exactly what the driver saw — no burned-in
            # metadata banner or clip-time overlay.
            show_metadata=not shot_mode,
            show_time=not shot_mode,
        )

        # Verify output
        if os.path.isfile(args.output) and os.path.getsize(args.output) > 1000:
            output_kb = os.path.getsize(args.output) / 1024
            write_status(args.status_file, {
                "status": "complete",
                "output": args.output,
                "elapsed_sec": round(duration, 1),
                "total_sec": duration,
            })
            print(f"Render complete: {args.output} ({output_kb:.0f}KB)", file=sys.stderr)
        else:
            write_status(args.status_file, {
                "status": "error",
                "error": "Output file missing or too small",
            })
            sys.exit(1)

    except Exception as e:
        write_status(args.status_file, {"status": "error", "error": str(e)})
        print(f"Render error: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)

    finally:
        # Clean up symlink dir
        try:
            import shutil
            shutil.rmtree(symlink_dir, ignore_errors=True)
        except Exception:
            pass


if __name__ == "__main__":
    main()
