"""Frame encoder for HUD clip rendering — ffmpeg process + writer thread.

render_clip_headless.py captures the UI's render texture each frame and hands
raw RGBA here to be encoded. This was previously borrowed from the ui_recorder
plugin, but COD only ever used its RECORD path: the plugin's STREAM_UI half fed
the live UI stream, which no longer exists, and its health-check hook belongs to
plugind. Owning the encoder keeps offline rendering independent of what is
installed or enabled under /data/plugins-runtime.

Env var interface is unchanged — openpilot's tools.clip.run.setup_env sets these
before the UI process starts.

The writer thread drains a small queue into ffmpeg's stdin. The queue is
deliberately shallow (maxsize=2) and the caller uses a blocking put, so the
render thread waits for the encoder rather than dropping frames — a dropped
frame in an offline render is a corrupt clip, not a hiccup.
"""

import atexit
import os
import queue
import subprocess
import threading
from pathlib import Path

RECORD = os.getenv("RECORD") == "1"

if RECORD:
    RECORD_HLS = os.getenv("RECORD_HLS") == "1"
    RECORD_OUTPUT = os.getenv("RECORD_OUTPUT", "output.mp4")
    RECORD_SKIP = int(os.getenv("RECORD_SKIP", "0"))
    RECORD_CODEC = os.getenv("RECORD_CODEC", "libx264")
    RECORD_FRAG_MP4 = os.getenv("RECORD_FRAG_MP4") == "1"
    RECORD_RAW = os.getenv("RECORD_RAW") == "1"
    RECORD_VF = os.getenv("RECORD_VF", "")
    if not RECORD_HLS and not RECORD_FRAG_MP4 and not RECORD_RAW:
        RECORD_OUTPUT = str(Path(RECORD_OUTPUT).with_suffix(".mp4"))

_initialized = False
_ffmpeg_proc: subprocess.Popen | None = None
_writer_queue: queue.Queue | None = None
_writer_thread: threading.Thread | None = None


def _build_ffmpeg_args(width: int, height: int, fps: float) -> list[str]:
    """Build the ffmpeg command line. Pure function — easy to test."""
    capture_fps = fps / (RECORD_SKIP + 1) if RECORD_SKIP > 0 else fps
    args = [
        'ffmpeg',
        '-v', 'warning',
        '-stats',
        '-f', 'rawvideo',
        '-pix_fmt', 'rgba',
        '-s', f'{width}x{height}',
        '-r', str(capture_fps),
        '-i', 'pipe:0',
        '-vf', f'vflip,{RECORD_VF + "," if RECORD_VF else ""}format=yuv420p',
    ]
    if not RECORD_RAW:
        args += ['-c:v', RECORD_CODEC]
        if RECORD_CODEC == 'libx264':
            args += ['-preset', 'ultrafast']
    args += ['-y']

    if RECORD_RAW:
        args += ['-f', 'rawvideo', '-pix_fmt', 'yuv420p', RECORD_OUTPUT]
    elif RECORD_HLS:
        args += [
            '-g', str(max(1, int(capture_fps))),
            '-f', 'hls',
            '-hls_time', os.getenv("RECORD_HLS_TIME", "2"),
            '-hls_list_size', os.getenv("RECORD_HLS_LIST_SIZE", "10"),
            '-hls_flags', 'delete_segments+append_list',
            RECORD_OUTPUT,
        ]
    elif RECORD_FRAG_MP4:
        args += [
            '-g', str(max(1, int(capture_fps))),
            '-f', 'mp4',
            '-movflags', 'frag_keyframe+empty_moov+default_base_moof',
            RECORD_OUTPUT,
        ]
    else:
        args += ['-f', 'mp4', RECORD_OUTPUT]

    return args


def start(width: int, height: int, fps: float) -> None:
    """Spawn ffmpeg and the writer thread. Idempotent."""
    global _initialized, _ffmpeg_proc, _writer_queue, _writer_thread

    if _initialized:
        return

    _writer_queue = queue.Queue(maxsize=2)
    _ffmpeg_proc = subprocess.Popen(_build_ffmpeg_args(width, height, fps), stdin=subprocess.PIPE)

    def _writer():
        while True:
            data = _writer_queue.get()
            if data is None:
                break
            try:
                _ffmpeg_proc.stdin.write(data)
                _ffmpeg_proc.stdin.flush()
            except (BrokenPipeError, OSError):
                break

    _writer_thread = threading.Thread(target=_writer, daemon=True, name="cod_recorder_writer")
    _writer_thread.start()

    atexit.register(cleanup)
    _initialized = True


def write_frame(data: bytes) -> None:
    """Hand one raw RGBA frame to the encoder. Blocks while ffmpeg catches up."""
    if _writer_queue is not None:
        _writer_queue.put(data)


def is_started() -> bool:
    return _initialized


def cleanup() -> None:
    """Flush the queue, close ffmpeg's stdin and wait for it to finish."""
    global _ffmpeg_proc, _writer_queue, _writer_thread

    if _writer_queue is not None:
        try:
            _writer_queue.put_nowait(None)
        except queue.Full:
            pass
        if _writer_thread is not None:
            _writer_thread.join(timeout=2)

    if _ffmpeg_proc is not None:
        try:
            _ffmpeg_proc.stdin.flush()
            _ffmpeg_proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        try:
            _ffmpeg_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _ffmpeg_proc.terminate()
            _ffmpeg_proc.wait()
