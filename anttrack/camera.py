"""Optional RTSP camera relay.

Browsers can't play RTSP directly, so this pulls the stream with ffmpeg
(installed via apt in the Docker image) and re-encodes it as MJPEG, which
an <img> tag can display straight from a multipart HTTP response. Runs in
a background thread with the same reconnect-on-any-error shape as the
rotator tracking loop.
"""

import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone

JPEG_SOI = b"\xff\xd8"
JPEG_EOI = b"\xff\xd9"

RECONNECT_DELAY_S = 5.0
READ_CHUNK = 4096
MAX_BUFFER_BYTES = 2_000_000  # guard against a malformed stream growing unbounded


class CameraStream:
    def __init__(self, rtsp_url, fps=5.0):
        self.rtsp_url = rtsp_url
        self.fps = fps

        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._latest_frame = None
        self._frame_seq = 0
        self._last_frame_utc = None
        self._connected = False
        self._error = None

        self._stop_event = threading.Event()
        self._thread = None
        self._proc = None

    @property
    def enabled(self):
        return bool(self.rtsp_url)

    def start(self):
        if not self.enabled or self._thread is not None:
            return
        if shutil.which("ffmpeg") is None:
            with self._lock:
                self._error = "ffmpeg not found on PATH"
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        proc = self._proc
        if proc is not None:
            proc.kill()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5)
        self._thread = None

    def status(self):
        with self._lock:
            return {
                "enabled": self.enabled,
                "connected": self._connected,
                "last_frame_utc": self._last_frame_utc,
                "error": self._error,
            }

    def latest(self):
        """Return (frame_bytes_or_None, seq) immediately, without waiting."""
        with self._lock:
            return self._latest_frame, self._frame_seq

    def get_frame(self, after_seq=0, timeout=10.0):
        """Block until a frame newer than after_seq is ready, or time out."""
        with self._condition:
            got = self._condition.wait_for(lambda: self._frame_seq > after_seq, timeout=timeout)
            if not got:
                return None, after_seq
            return self._latest_frame, self._frame_seq

    # -- background relay ------------------------------------------------

    def _run(self):
        while not self._stop_event.is_set():
            try:
                self._stream_once()
            except Exception as exc:  # pylint: disable=broad-except
                with self._lock:
                    self._connected = False
                    self._error = str(exc)
            self._stop_event.wait(RECONNECT_DELAY_S)

    def _stream_once(self):
        cmd = [
            "ffmpeg", "-loglevel", "error",
            "-rtsp_transport", "tcp",
            "-i", self.rtsp_url,
            "-an", "-r", str(self.fps),
            "-f", "mjpeg", "-q:v", "5",
            "pipe:1",
        ]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self._proc = proc

        # ffmpeg can wedge (e.g. the camera stops sending) without exiting;
        # kill it if too long passes without a decoded frame so _run reconnects.
        stale_timeout = max(10.0, 4.0 / self.fps)
        last_frame_at = time.monotonic()

        def watchdog():
            while proc.poll() is None and not self._stop_event.is_set():
                if time.monotonic() - last_frame_at > stale_timeout:
                    proc.kill()
                    return
                time.sleep(1.0)

        watchdog_thread = threading.Thread(target=watchdog, daemon=True)
        watchdog_thread.start()

        buf = b""
        try:
            while not self._stop_event.is_set():
                chunk = proc.stdout.read(READ_CHUNK)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > MAX_BUFFER_BYTES:
                    buf = b""

                while True:
                    start = buf.find(JPEG_SOI)
                    if start == -1:
                        buf = b""
                        break
                    end = buf.find(JPEG_EOI, start + 2)
                    if end == -1:
                        buf = buf[start:]
                        break
                    frame = buf[start:end + 2]
                    buf = buf[end + 2:]
                    last_frame_at = time.monotonic()
                    with self._condition:
                        self._latest_frame = frame
                        self._frame_seq += 1
                        self._last_frame_utc = datetime.now(timezone.utc).isoformat(
                            timespec="milliseconds")
                        self._connected = True
                        self._error = None
                        self._condition.notify_all()
        finally:
            self._proc = None
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            watchdog_thread.join(timeout=2)
            with self._lock:
                self._connected = False
