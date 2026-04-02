"""Screencast: play fcamera.hevc on C3 screen, controlled from mobile browser.

Lightweight video player using PyAV (decode) + raylib (DRM render).
No HUD overlay, no replay pipeline — just raw camera video at native quality.

Control protocol (UDP on port 8090):
  PLAY <route_local_id> <segment> <offset>   — start/seek
  PAUSE                                       — pause playback
  RESUME                                      — resume playback
  STOP                                        — stop and exit
"""

import os
import signal
import socket
import sys
import threading
import time

from config import OPENPILOT_DIR, REALDATA_DIR
CONTROL_PORT = 8090
FPS = 20
SCREEN_W = 2160
SCREEN_H = 1080


def _stop_manager():
    """Stop openpilot manager (and all children) to free DRM master."""
    import subprocess
    subprocess.run(["pkill", "-f", "manager.py"], capture_output=True)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        r = subprocess.run(["pgrep", "-f", "manager.py"], capture_output=True)
        if r.returncode != 0:
            break
        time.sleep(0.3)
    else:
        subprocess.run(["pkill", "-9", "-f", "manager.py"], capture_output=True)
        time.sleep(0.5)
    subprocess.run(["pkill", "-9", "-f", "selfdrive.ui.ui"], capture_output=True)
    time.sleep(1)


def _start_manager():
    """Restart openpilot manager after screencast."""
    import subprocess
    MANAGER_CWD = os.path.join(OPENPILOT_DIR, "system/manager")
    subprocess.run(["pkill", "-f", "selfdrive.ui.ui"], capture_output=True)
    time.sleep(0.5)
    env = os.environ.copy()
    env["PYTHONPATH"] = OPENPILOT_DIR
    env["PATH"] = "/usr/local/venv/bin:/usr/local/bin:/usr/bin:/bin"
    env["HOME"] = os.environ.get("HOME", "/root")
    try:
        subprocess.Popen(
            ["/usr/local/venv/bin/python", "./manager.py"],
            cwd=MANAGER_CWD, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        pass


def _find_hevc(local_id, segment):
    """Find fcamera.hevc for given segment."""
    seg_dir = os.path.join(REALDATA_DIR, f"{local_id}--{segment}")
    for name in ("fcamera.hevc", "ecamera.hevc"):
        path = os.path.join(seg_dir, name)
        if os.path.exists(path):
            return path
    return None


def _frame_generator(local_id, start_segment, start_offset):
    """Yield (frame_rgb_bytes, width, height) from fcamera.hevc starting at given position."""
    import av

    seg = start_segment
    while True:
        hevc_path = _find_hevc(local_id, seg)
        if not hevc_path:
            break

        container = av.open(hevc_path)
        stream = container.streams.video[0]
        stream.thread_type = "AUTO"

        frame_idx = 0
        skip_frames = int(start_offset * FPS) if seg == start_segment else 0

        for frame in container.decode(stream):
            if frame_idx < skip_frames:
                frame_idx += 1
                continue

            rgb = frame.to_ndarray(format="rgb24")
            yield rgb, rgb.shape[1], rgb.shape[0]
            frame_idx += 1

        container.close()
        seg += 1
        # Reset offset for subsequent segments
        start_offset = 0


class Screencast:
    def __init__(self):
        self._playing = False
        self._paused = False
        self._route_id = None
        self._segment = 0
        self._offset = 0.0
        self._stop_event = threading.Event()
        self._command_lock = threading.Lock()
        self._pending_command = None

    def run(self):
        """Main loop: listen for commands, play video on DRM screen."""
        sys.path.insert(0, OPENPILOT_DIR)
        os.environ["PYTHONPATH"] = OPENPILOT_DIR

        # Start UDP control listener
        ctrl_thread = threading.Thread(target=self._control_listener, daemon=True)
        ctrl_thread.start()

        # Kill production UI to free DRM master
        print("Stopping production UI for DRM access...", flush=True)
        _stop_manager()

        # Wait for kernel to fully release DRM resources after UI process exits
        time.sleep(1)

        # Grab DRM master — manager won't respawn externally-killed UI (stale proc object)
        import pyray as rl
        print(f"Initializing raylib DRM window {SCREEN_W}x{SCREEN_H}...", flush=True)
        rl.set_trace_log_level(rl.TraceLogLevel.LOG_WARNING)
        rl.set_config_flags(rl.ConfigFlags.FLAG_MSAA_4X_HINT)
        rl.init_window(SCREEN_W, SCREEN_H, "screencast")
        rl.set_target_fps(FPS)
        print("DRM window acquired, waiting for PLAY command...", flush=True)

        # Show black screen while waiting for PLAY command
        while not self._stop_event.is_set():
            if rl.window_should_close():
                rl.close_window()
                _start_manager()
                return

            cmd = self._get_command()
            if cmd and cmd[0] == "PLAY":
                self._route_id = cmd[1]
                self._segment = int(cmd[2])
                self._offset = float(cmd[3])
                break
            elif cmd and cmd[0] == "STOP":
                rl.close_window()
                _start_manager()
                return

            # Render black frame to keep DRM alive
            rl.begin_drawing()
            rl.clear_background(rl.BLACK)
            rl.end_drawing()

        if self._stop_event.is_set():
            rl.close_window()
            _start_manager()
            return

        try:
            self._play_loop(rl)
        finally:
            rl.close_window()
            _start_manager()

    def _play_loop(self, rl):
        """Render loop: decode fcamera → draw to DRM screen via raylib."""
        print(f"Playing {self._route_id} seg={self._segment} offset={self._offset:.1f}", flush=True)

        texture = None
        frame_gen = _frame_generator(self._route_id, self._segment, self._offset)
        paused = False

        try:
            while not rl.window_should_close() and not self._stop_event.is_set():
                # Check for commands
                cmd = self._get_command()
                if cmd:
                    if cmd[0] == "STOP":
                        break
                    elif cmd[0] == "PAUSE":
                        paused = True
                    elif cmd[0] == "RESUME":
                        paused = False
                    elif cmd[0] == "PLAY":
                        self._route_id = cmd[1]
                        self._segment = int(cmd[2])
                        self._offset = float(cmd[3])
                        frame_gen = _frame_generator(cmd[1], int(cmd[2]), float(cmd[3]))
                        paused = False
                        print(f"Seek: {cmd[1]} seg={cmd[2]} offset={cmd[3]}", flush=True)

                if not paused:
                    try:
                        rgb, fw, fh = next(frame_gen)
                    except StopIteration:
                        paused = True
                        rl.begin_drawing()
                        rl.end_drawing()
                        continue

                    # Create/update texture from frame
                    if texture is None or texture.width != fw or texture.height != fh:
                        if texture is not None:
                            rl.unload_texture(texture)
                        img = rl.gen_image_color(fw, fh, rl.BLACK)
                        texture = rl.load_texture_from_image(img)
                        rl.set_texture_filter(texture, rl.TextureFilter.TEXTURE_FILTER_BILINEAR)
                        rl.unload_image(img)

                    rl.update_texture(texture, rl.ffi.from_buffer(rgb.tobytes()))

                # Draw
                rl.begin_drawing()
                rl.clear_background(rl.BLACK)
                if texture is not None:
                    scale = max(SCREEN_W / texture.width, SCREEN_H / texture.height)
                    dw = int(texture.width * scale)
                    dh = int(texture.height * scale)
                    dx = (SCREEN_W - dw) // 2
                    dy = (SCREEN_H - dh) // 2
                    src = rl.Rectangle(0, 0, float(texture.width), float(texture.height))
                    dst = rl.Rectangle(float(dx), float(dy), float(dw), float(dh))
                    rl.draw_texture_pro(texture, src, dst, rl.Vector2(0, 0), 0.0, rl.WHITE)
                rl.end_drawing()

        finally:
            if texture is not None:
                rl.unload_texture(texture)

    def _control_listener(self):
        """Listen for UDP control commands."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", CONTROL_PORT))
        sock.settimeout(1.0)

        while not self._stop_event.is_set():
            try:
                data, _ = sock.recvfrom(256)
                msg = data.decode().strip()
                parts = msg.split()
                if parts:
                    with self._command_lock:
                        self._pending_command = parts
            except socket.timeout:
                continue
            except Exception:
                continue
        sock.close()

    def _get_command(self):
        with self._command_lock:
            cmd = self._pending_command
            self._pending_command = None
            return cmd


def main():
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    sc = Screencast()
    try:
        sc.run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
