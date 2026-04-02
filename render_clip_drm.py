#!/usr/bin/env python3
"""Render openpilot UI HUD to MP4 — DRM backend with RECORD mode.

Leverages the production raylib UI's built-in RECORD mode on DRM/KMS backend.
No Weston compositor, no screenshooter — direct GPU framebuffer capture.

Pipeline: replay(0.2x) → UI(RECORD=1, DRM) → RenderTexture2D → ffmpeg pipe → raw MP4
          raw MP4 → ffmpeg post-process (trim warmup + 5x speedup) → final MP4

vs render_clip.py pipeline:
  patched_weston → UI(wayland-egl) → stream_capture(5fps) → ffmpeg → MP4

Advantages:
  - 10 unique frames/route-second (vs 5fps screenshooter frame-duplicated)
  - Direct GPU framebuffer readback (no compositor artifacts)
  - No external binaries needed (no weston_patched, stream_capture)
  - Simpler lifecycle (just stop UI, record, restart)
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from subprocess import Popen

from config import FFMPEG_BIN

# Handle SIGTERM gracefully — raises SystemExit so finally blocks run
signal.signal(signal.SIGTERM, lambda *_: sys.exit(1))

from config import OPENPILOT_DIR, PYTHON_BIN, COD_HLS_TMP_DIR, PARAMS_BASE
if OPENPILOT_DIR not in sys.path:
    sys.path.insert(0, OPENPILOT_DIR)

from common.prefix import OpenpilotPrefix
from tools.lib.logreader import LogReader
from tools.clip.run import populate_car_params, wait_for_frames, check_for_failure

REPLAY_BIN = str(Path(OPENPILOT_DIR, "tools/replay/replay").resolve())
UI_SCRIPT = "selfdrive/ui/ui.py"

# C3 GPU readback bottleneck: load_image_from_texture() + 9.3MB stdin write per frame
# limits actual capture to ~2-3fps at 2160x1080. At 0.2x replay speed, each route-second
# takes 5 wall-seconds → 2fps * 5 = 10 unique frames per route-second.
# The raw 2fps video is sped up 5x in post (-itsscale 0.2) → 10fps real-time output,
# frame-duplicated to args.fps (default 20) for smooth playback.
RECORD_FPS = 2
REPLAY_SPEED = 0.2
SPEEDUP_FACTOR = 1.0 / REPLAY_SPEED  # 5x
HLS_DIR = COD_HLS_TMP_DIR
SECONDS_TO_WARM = 2


def start_selfdrive_publisher(stop_event: threading.Event):
    """Publish fake selfdriveState to prevent 'System Unresponsive' alert.

    At 0.2x replay speed, gaps in rlog selfdriveState are amplified 5x — a 1s gap
    becomes 5s, triggering the UI's staleness timeout. This keeps the message fresh.
    Not needed for HUD Preview (1x speed) where replay's own messages suffice.
    """
    import cereal.messaging as messaging
    pm = messaging.PubMaster(['selfdriveState'])
    while not stop_event.is_set():
        msg = messaging.new_message('selfdriveState')
        msg.valid = True
        ss = msg.selfdriveState
        ss.enabled = False
        ss.active = False
        ss.alertSize = 0  # NONE — no alert overlay
        pm.send('selfdriveState', msg)
        stop_event.wait(0.05)  # 20Hz


def run_ffmpeg_with_progress(cmd, status_file, phase, total_sec, log_path):
    """Run ffmpeg with -progress tracking, updating status file with post_elapsed_sec."""
    # Insert -progress pipe:1 before output (last arg)
    full_cmd = cmd[:-1] + ["-progress", "pipe:1", cmd[-1]]
    proc = Popen(full_cmd, stdout=subprocess.PIPE, stderr=open(log_path, "w"),
                 universal_newlines=True)
    post_elapsed = 0.0
    for line in proc.stdout:
        line = line.strip()
        if line.startswith("out_time_us="):
            try:
                us = int(line.split("=", 1)[1])
                post_elapsed = min(us / 1_000_000, total_sec)
                write_status(status_file, {
                    "status": "rendering", "phase": phase,
                    "elapsed_sec": round(total_sec, 1), "total_sec": total_sec,
                    "post_elapsed_sec": round(post_elapsed, 1), "post_total_sec": round(total_sec, 1),
                })
            except (ValueError, IndexError):
                pass
    proc.wait()
    return proc.returncode


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
    """Create temp directory with canonical-name symlinks for replay."""
    tmpdir = tempfile.mkdtemp(prefix="hud_render_drm_")
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


def find_rlog(data_dir: str, local_id: str) -> str | None:
    """Find rlog in segment 0."""
    seg0 = os.path.join(data_dir, f"{local_id}--0")
    for fname in ["rlog.zst", "rlog"]:
        path = os.path.join(seg0, fname)
        if os.path.exists(path):
            return path
    return None


MANAGER_CWD = os.path.join(OPENPILOT_DIR, "system/manager")


def stop_manager():
    """Stop openpilot manager (and all children) to free DRM master."""
    print("Stopping openpilot manager...", file=sys.stderr)
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


def start_manager():
    """Restart openpilot manager after HUD rendering."""
    print("Restarting openpilot manager...", file=sys.stderr)
    subprocess.run(["pkill", "-f", "selfdrive.ui.ui"], capture_output=True)
    time.sleep(0.5)

    env = os.environ.copy()
    env["PYTHONPATH"] = OPENPILOT_DIR
    env["PATH"] = "/usr/local/venv/bin:/usr/local/bin:/usr/bin:/bin"
    env["HOME"] = os.environ.get("HOME", "/root")

    try:
        Popen(
            [PYTHON_BIN, "./manager.py"],
            cwd=MANAGER_CWD,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        print("Manager started", file=sys.stderr)
    except Exception as e:
        print(f"Failed to start manager: {e}", file=sys.stderr)


def cleanup_procs(*procs):
    """Terminate all processes cleanly."""
    for p in procs:
        try:
            if p and p.poll() is None:
                p.terminate()
        except Exception:
            pass
    time.sleep(1)
    for p in procs:
        try:
            if p and p.poll() is None:
                p.kill()
        except Exception:
            pass
    # Name-based fallback for orphans
    for pattern in ["tools/replay/replay"]:
        subprocess.run(["pkill", "-KILL", "-f", pattern], capture_output=True)


def main():
    parser = argparse.ArgumentParser(description="Render openpilot HUD video (DRM backend)")
    parser.add_argument("--route-name", required=True)
    parser.add_argument("--local-id", required=True)
    parser.add_argument("--dongle-id", required=True)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--start", type=float, default=0)
    parser.add_argument("--end", type=float, required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--status-file", required=True)
    parser.add_argument("--fps", type=int, default=20)
    parser.add_argument("--route-date", default="")  # human-readable date for metadata
    parser.add_argument("--op-version", default="")
    parser.add_argument("--op-branch", default="")
    parser.add_argument("--op-commit", default="")
    parser.add_argument("--car-fingerprint", default="")
    args = parser.parse_args()

    duration = args.end - args.start
    if duration <= 0:
        write_status(args.status_file, {"status": "error", "error": "Invalid time range"})
        sys.exit(1)

    # Check prerequisites
    if not os.path.isfile(REPLAY_BIN):
        write_status(args.status_file, {"status": "error", "error": f"replay not found at {REPLAY_BIN}"})
        sys.exit(1)
    ui_path = os.path.join(OPENPILOT_DIR, UI_SCRIPT)
    if not os.path.isfile(ui_path):
        write_status(args.status_file, {"status": "error", "error": f"UI script not found at {ui_path}"})
        sys.exit(1)

    os.makedirs(os.path.dirname(args.output), exist_ok=True)

    max_seg = find_max_segment(args.data_dir, args.local_id)
    if max_seg < 0:
        write_status(args.status_file, {"status": "error", "error": "No segments found"})
        sys.exit(1)

    rlog_path = find_rlog(args.data_dir, args.local_id)
    if not rlog_path:
        write_status(args.status_file, {"status": "error", "error": "No rlog found in segment 0"})
        sys.exit(1)

    symlink_dir = create_symlink_dir(args.data_dir, args.local_id, args.dongle_id, max_seg + 1)
    canonical_name = f"{args.dongle_id}|{args.local_id}"
    prefix = f"clip_drm_{os.getpid()}"

    # Output goes to a temp file first, then we trim warmup with ffmpeg
    raw_output = args.output + ".raw.mp4"
    replay_proc = None
    ui_proc = None
    sd_stop = threading.Event()

    try:
        write_status(args.status_file, {"status": "rendering", "elapsed_sec": 0,
                                        "total_sec": duration, "phase": "loading params"})

        with OpenpilotPrefix(prefix, shared_download_cache=True):
            # Ensure shm prefix directory exists for msgq socket binding
            # C++ msgq uses /dev/shm/msgq_{prefix}/ path format
            os.makedirs(f"/dev/shm/msgq_{prefix}", exist_ok=True)

            # Populate CarParams from rlog
            lr = LogReader(rlog_path)
            populate_car_params(lr)

            # Prevent "System Unresponsive" — at 0.2x replay, selfdriveState gaps
            # in the rlog get amplified 5x, exceeding the UI's 5s staleness timeout.
            sd_thread = threading.Thread(target=start_selfdrive_publisher,
                                         args=(sd_stop,), daemon=True)
            sd_thread.start()

            # Stop production UI to free DRM master
            write_status(args.status_file, {"status": "rendering", "elapsed_sec": 0,
                                            "total_sec": duration, "phase": "acquiring display"})
            stop_manager()

            # Start replay at 0.2x speed — slow enough for 2fps GPU capture to get 10 unique frames/route-sec
            # Warm up a few seconds early so UI has data when recording starts
            write_status(args.status_file, {"status": "rendering", "elapsed_sec": 0,
                                            "total_sec": duration, "phase": "starting replay"})

            begin_at = max(args.start - SECONDS_TO_WARM, 0)
            actual_warmup = args.start - begin_at

            replay_cmd = [
                REPLAY_BIN, "-c", "1",
                "--no-hw-decoder",
                "-s", str(int(begin_at)),
                "-x", str(REPLAY_SPEED),
                "--no-loop",
                "--data_dir", symlink_dir,
                "--prefix", prefix,
                canonical_name,
            ]

            # Replay needs TERM for ncurses consoleui, and OPENPILOT_PREFIX for IPC
            replay_env = os.environ.copy()
            replay_env["TERM"] = "xterm"
            replay_env["OPENPILOT_PREFIX"] = prefix

            replay_proc = Popen(replay_cmd,
                                env=replay_env,
                                stdout=subprocess.DEVNULL,
                                stderr=open("/tmp/hud_replay_drm.log", "w"))

            # Build UI environment for DRM RECORD+HLS mode
            # HLS output enables live preview in the browser during recording.
            # All segments kept (list_size=9999) for concatenation into MP4 afterward.
            import shutil
            if os.path.exists(HLS_DIR):
                shutil.rmtree(HLS_DIR)
            os.makedirs(HLS_DIR, exist_ok=True)

            hls_m3u8 = os.path.join(HLS_DIR, "stream.m3u8")
            ui_env = {
                "RECORD": "1",
                "RECORD_HLS": "1",
                "RECORD_OUTPUT": hls_m3u8,
                "RECORD_HLS_TIME": "2",
                "RECORD_HLS_LIST_SIZE": "9999",
                "RECORD_CRF": "10",  # Near-lossless first pass; post-processing is the single lossy encode
                "FPS": str(RECORD_FPS),
                "BIG": "1",  # Force 2160x1080 layout
                "PYTHONPATH": OPENPILOT_DIR,
                "OPENPILOT_PREFIX": prefix,
                "PATH": f"/usr/local/venv/bin:/usr/local/bin:/usr/bin:/bin",
                "HOME": os.environ.get("HOME", "/root"),
                "TERM": "xterm",
            }

            # Skip config_realtime_process (SCHED_FIFO requires CAP_SYS_NICE / root).
            # Patch os.sched_setscheduler (the underlying syscall) — catches all callers
            # regardless of import path (common.realtime vs openpilot.common.realtime).
            ui_cmd = [
                PYTHON_BIN, "-c",
                "import os; os.sched_setscheduler = lambda *a, **kw: None; "
                "exec(open('selfdrive/ui/ui.py').read())",
            ]

            ui_proc = Popen(ui_cmd,
                            cwd=OPENPILOT_DIR,
                            env=ui_env,
                            stdout=open("/tmp/hud_ui_drm.log", "w"),
                            stderr=subprocess.STDOUT)

            procs = [replay_proc, ui_proc]

            # Wait for UI to draw first frame
            print("Waiting for UI to start drawing...", file=sys.stderr)
            wait_for_frames(procs)
            warmup_wall = actual_warmup / REPLAY_SPEED
            print(f"UI drawing, warming up ({actual_warmup:.1f}s route = {warmup_wall:.1f}s wall)...",
                  file=sys.stderr)
            time.sleep(warmup_wall)
            check_for_failure(procs)

            # Now recording — the RECORD mode records from frame 0, including warmup.
            # We'll trim the warmup frames with ffmpeg afterward.
            write_status(args.status_file, {"status": "rendering", "elapsed_sec": 0,
                                            "total_sec": duration, "phase": "recording"})

            start_time = time.monotonic()
            wall_duration = duration / REPLAY_SPEED
            print(f"Recording {duration}s clip at {RECORD_FPS}fps, {REPLAY_SPEED}x speed "
                  f"({wall_duration:.0f}s wall)...", file=sys.stderr)

            while True:
                elapsed = time.monotonic() - start_time
                route_elapsed = min(elapsed * REPLAY_SPEED, duration)

                # Check if replay has finished (exhausted data)
                if replay_proc.poll() is not None:
                    print(f"Replay finished (elapsed {elapsed:.1f}s)", file=sys.stderr)
                    time.sleep(2)
                    break

                # Check if UI has exited unexpectedly
                if ui_proc.poll() is not None:
                    print(f"UI exited (code {ui_proc.returncode})", file=sys.stderr)
                    break

                # Check if we've recorded enough wall time
                if elapsed >= wall_duration + 3:
                    print(f"Recording time reached ({elapsed:.1f}s)", file=sys.stderr)
                    break

                write_status(args.status_file, {
                    "status": "rendering",
                    "elapsed_sec": round(route_elapsed, 1),
                    "total_sec": duration,
                    "remaining_sec": round(max(0, wall_duration - elapsed)),
                    "phase": "recording",
                })
                time.sleep(2)

            # Stop UI — triggers close_ffmpeg() which flushes and closes the MP4
            print("Stopping UI to finalize recording...", file=sys.stderr)
            if ui_proc.poll() is None:
                ui_proc.terminate()
                try:
                    ui_proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    ui_proc.kill()
                    ui_proc.wait()

            print(f"UI exited (code {ui_proc.returncode})", file=sys.stderr)

            # Kill replay + selfdriveState publisher — not needed for post-processing
            sd_stop.set()
            if replay_proc.poll() is None:
                replay_proc.terminate()
                try:
                    replay_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    replay_proc.kill()

            # Concatenate HLS segments into raw MP4 for post-processing
            hls_m3u8 = os.path.join(HLS_DIR, "stream.m3u8")
            if os.path.isfile(hls_m3u8):
                write_status(args.status_file, {"status": "rendering", "elapsed_sec": round(duration, 1),
                                                "total_sec": duration, "phase": "concatenating"})
                concat_cmd = [FFMPEG_BIN, "-y", "-allowed_extensions", "ALL",
                              "-i", hls_m3u8, "-c", "copy", raw_output]
                run_ffmpeg_with_progress(concat_cmd, args.status_file,
                                         "concatenating", duration, "/tmp/hud_concat_drm.log")

            # Post-process: trim warmup + speed up from 0.2x to real-time
            # Raw recording: 2fps at 0.2x speed → needs 5x speedup to play at real-time.
            # Use -itsscale for reliable speedup (setpts+fps unreliable on ffmpeg 4.2.2).
            # Then frame-duplicate to args.fps (20) for smooth playback.
            if os.path.isfile(raw_output) and os.path.getsize(raw_output) > 1000:
                write_status(args.status_file, {"status": "rendering", "elapsed_sec": round(duration, 1),
                                                "total_sec": duration, "phase": "post-processing"})

                # Build ffmpeg command with input options for trim + speedup
                post_cmd = [FFMPEG_BIN, "-y"]

                # Trim warmup: -ss and -t are in INPUT time (before itsscale)
                if actual_warmup > 0:
                    raw_warmup = actual_warmup / REPLAY_SPEED
                    raw_duration = duration / REPLAY_SPEED
                    post_cmd.extend(["-ss", f"{raw_warmup:.2f}", "-t", f"{raw_duration:.2f}"])

                # Speed up 5x via input timestamp scaling (demuxer-level, reliable)
                post_cmd.extend(["-itsscale", str(REPLAY_SPEED)])
                post_cmd.extend(["-i", raw_output])

                # Frame-duplicate to target fps + enforce exact output duration
                post_cmd.extend(["-vf", f"fps={args.fps}"])
                post_cmd.extend(["-t", f"{duration:.2f}"])

                # MP4 metadata — Discord #driving-feedback format
                seg_start = int(args.start) // 60
                seg_end = (int(args.end) - 1) // 60
                route_display = f"{args.dongle_id}/{args.local_id}/{seg_start}/{seg_end}"
                short_commit = args.op_commit[:7] if args.op_commit else ""

                meta_comment = "\n".join(filter(None, [
                    f"Route: {route_display}",
                    f"Fingerprint: {args.car_fingerprint}" if args.car_fingerprint else None,
                    f"Branch: {args.op_branch}" if args.op_branch else None,
                    f"Version: {args.op_version}" if args.op_version else None,
                    f"Commit: {short_commit}" if short_commit else None,
                ]))

                meta_title = f"openpilot {args.op_version} HUD" if args.op_version else "openpilot HUD"
                meta_artist = f"openpilot ({args.car_fingerprint})" if args.car_fingerprint else f"openpilot ({args.dongle_id})"

                post_cmd.extend([
                    "-metadata", f"title={meta_title}",
                    "-metadata", f"comment={meta_comment}",
                    "-metadata", f"artist={meta_artist}",
                    "-metadata", "encoder=connect-on-device render_clip_drm",
                    "-c:v", "libx264",
                    "-preset", "ultrafast",
                    "-crf", "18",
                    "-threads", "0",
                    "-pix_fmt", "yuv420p",
                    "-movflags", "+faststart",
                    args.output,
                ])

                print(f"Post-processing: trim={actual_warmup:.1f}s, {SPEEDUP_FACTOR:.0f}x speedup → {args.fps}fps...",
                      file=sys.stderr)
                rc = run_ffmpeg_with_progress(post_cmd, args.status_file,
                                              "post-processing", duration, "/tmp/hud_trim_drm.log")
                if rc == 0 and os.path.isfile(args.output):
                    os.unlink(raw_output)
                else:
                    # Post-process failed — use raw output as fallback
                    print("Post-processing failed, using raw output", file=sys.stderr)
                    os.rename(raw_output, args.output)

            # Verify output
            if not os.path.isfile(args.output) or os.path.getsize(args.output) < 1000:
                write_status(args.status_file, {"status": "error",
                                                "error": "Output file missing or too small"})
                sys.exit(1)

            output_kb = os.path.getsize(args.output) / 1024
            write_status(args.status_file, {
                "status": "complete",
                "output": args.output,
                "elapsed_sec": round(duration, 1),
                "total_sec": duration,
            })
            print(f"Render complete: {args.output} ({output_kb:.0f}KB)", file=sys.stderr)

    except Exception as e:
        write_status(args.status_file, {"status": "error", "error": str(e)})
        sys.exit(1)

    finally:
        sd_stop.set()
        cleanup_procs(replay_proc, ui_proc)
        start_manager()
        # Clean up temp files
        try:
            import shutil
            shutil.rmtree(symlink_dir, ignore_errors=True)
            shutil.rmtree(f"/dev/shm/{prefix}", ignore_errors=True)
            shutil.rmtree(f"/dev/shm/msgq_{prefix}", ignore_errors=True)
            shutil.rmtree(f"{PARAMS_BASE}/{prefix}", ignore_errors=True)
            if os.path.isfile(raw_output):
                os.unlink(raw_output)
            shutil.rmtree(HLS_DIR, ignore_errors=True)
        except Exception:
            pass


if __name__ == "__main__":
    main()
