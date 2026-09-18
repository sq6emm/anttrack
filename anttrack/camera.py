"""Optional RTSP camera relay.

Browsers can't play RTSP directly, so this pulls the stream with ffmpeg
(installed via apt in the Docker image) and remuxes it -- no re-encoding,
just repackaging -- into fragmented MP4, which a plain HTML5 <video> tag
can play directly as a live, low-latency stream (no MediaSource/JS
plumbing needed; the browser's own MP4 demuxer reads it progressively).
This is lighter than an MJPEG relay: no per-frame JPEG re-encode, and
H.264's inter-frame compression instead of a full image every frame.

Runs in a background thread with the same reconnect-on-any-error shape
as the rotator tracking loop.
"""

import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone

RECONNECT_DELAY_S = 5.0
STALE_TIMEOUT_S = 10.0
SNAPSHOT_TIMEOUT_S = 8.0


def _read_exact(fp, n):
    """Read exactly n bytes from a blocking file object, or None on EOF."""
    buf = b""
    while len(buf) < n:
        chunk = fp.read(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def _read_box(fp):
    """Read one ISO-BMFF box (4-byte size + 4-byte type + body) from fp."""
    header = _read_exact(fp, 8)
    if header is None:
        return None, None
    size = int.from_bytes(header[0:4], "big")
    box_type = header[4:8]
    if size == 1:
        ext = _read_exact(fp, 8)
        if ext is None:
            return None, None
        size = int.from_bytes(ext, "big")
        header += ext
    if size < len(header):
        return None, None  # malformed; bail and let the caller reconnect
    body = _read_exact(fp, size - len(header))
    if body is None:
        return None, None
    return box_type, header + body


class CameraStream:
    def __init__(self, rtsp_url):
        self.rtsp_url = rtsp_url

        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._init_segment = None   # cached ftyp+moov, replayed to each new client
        self._latest_fragment = None
        self._fragment_seq = 0
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

    def get_init_segment(self, timeout=10.0):
        """Block until the ftyp+moov header is available, or time out."""
        with self._condition:
            self._condition.wait_for(lambda: self._init_segment is not None, timeout=timeout)
            return self._init_segment

    def get_fragment(self, after_seq=0, timeout=10.0):
        """Block until a fragment newer than after_seq is ready, or time out."""
        with self._condition:
            got = self._condition.wait_for(
                lambda: self._fragment_seq > after_seq, timeout=timeout)
            if not got:
                return None, after_seq
            return self._latest_fragment, self._fragment_seq

    def capture_snapshot(self, timeout=SNAPSHOT_TIMEOUT_S):
        """One-off still JPEG grab, independent of the live relay."""
        cmd = [
            "ffmpeg", "-loglevel", "error",
            "-rtsp_transport", "tcp",
            "-i", self.rtsp_url,
            "-frames:v", "1", "-f", "image2", "-q:v", "3",
            "pipe:1",
        ]
        try:
            result = subprocess.run(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=timeout)
        except subprocess.TimeoutExpired:
            return None
        return result.stdout or None

    # -- background relay ------------------------------------------------

    def _run(self):
        while not self._stop_event.is_set():
            try:
                self._stream_once()
            except Exception as exc:  # pylint: disable=broad-except
                with self._lock:
                    self._connected = False
                    self._error = str(exc)
            with self._lock:
                self._init_segment = None  # force a fresh header on reconnect
            self._stop_event.wait(RECONNECT_DELAY_S)

    def _stream_once(self):
        cmd = [
            "ffmpeg", "-loglevel", "error",
            "-rtsp_transport", "tcp",
            "-i", self.rtsp_url,
            "-an", "-c:v", "copy",
            "-f", "mp4",
            "-movflags", "empty_moov+frag_every_frame+default_base_moof",
            "pipe:1",
        ]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self._proc = proc

        # ffmpeg can wedge (e.g. the camera stops sending) without exiting;
        # kill it if too long passes without a fragment so _run reconnects.
        last_frame_at = time.monotonic()

        def watchdog():
            while proc.poll() is None and not self._stop_event.is_set():
                if time.monotonic() - last_frame_at > STALE_TIMEOUT_S:
                    proc.kill()
                    return
                time.sleep(1.0)

        watchdog_thread = threading.Thread(target=watchdog, daemon=True)
        watchdog_thread.start()

        pending_moof = None
        try:
            while not self._stop_event.is_set():
                box_type, box_bytes = _read_box(proc.stdout)
                if box_type is None:
                    break

                if box_type in (b"ftyp", b"moov"):
                    with self._condition:
                        self._init_segment = (self._init_segment or b"") + box_bytes
                        self._condition.notify_all()
                elif box_type == b"moof":
                    pending_moof = box_bytes
                elif box_type == b"mdat":
                    if pending_moof is None:
                        continue  # mdat without a preceding moof; drop and resync
                    fragment = pending_moof + box_bytes
                    pending_moof = None
                    last_frame_at = time.monotonic()
                    with self._condition:
                        self._latest_fragment = fragment
                        self._fragment_seq += 1
                        self._last_frame_utc = datetime.now(timezone.utc).isoformat(
                            timespec="milliseconds")
                        self._connected = True
                        self._error = None
                        self._condition.notify_all()
                # other box types (e.g. free/styp padding) are ignored
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
