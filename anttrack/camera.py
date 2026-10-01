"""Optional RTSP camera relay.

Browsers can't play RTSP directly, so this pulls the stream with ffmpeg
(installed via apt in the Docker image) and remuxes it into fragmented
MP4, which a plain HTML5 <video> tag can play directly as a live,
low-latency stream (no MediaSource/JS plumbing needed; the browser's own
MP4 demuxer reads it progressively).

With no crop/scale/fps configured this is a straight remux (-c:v copy,
no re-encode): lighter than an MJPEG relay, since there's no per-frame
JPEG re-encode and H.264's inter-frame compression beats a full image
every frame. Configuring a crop (e.g. to frame a fixed antenna and
exclude everything else in the shot) requires an actual decode +
filter + re-encode, since a compressed frame can't be cropped without
decoding it first; every output frame is then forced to be a keyframe
(needed since each MP4 fragment must be independently decodable for a
client that just connected) which keeps that re-encode cheap at a low
fps -- appropriate for a subject that isn't moving in frame anyway.

Each connected client gets its own fragment queue rather than sharing a
single "latest fragment" slot. A <video> fed a live fMP4 stream needs
*every* fragment in order: in stream-copy mode the fragments are
inter-coded frames, so a dropped one breaks the reference chain and
leaves a hole in the media timeline, and the element then wedges on that
hole showing a blank (grey) frame without ever firing an `error` event.
For the same reason a client is handed the fragments back to the last
keyframe when it joins (so it starts on something decodable), and its
response is closed when ffmpeg restarts -- a new ffmpeg means a new moov
and timestamps starting over at zero, which the already-running element
can't follow, so it has to reconnect and re-read the header.

Runs in a background thread with the same reconnect-on-any-error shape
as the rotator tracking loop.
"""

import collections
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone

RECONNECT_DELAY_S = 5.0
STALE_TIMEOUT_S = 10.0
SNAPSHOT_TIMEOUT_S = 8.0

# Per-client queue depth. A client this far behind can't catch up on a live
# stream, and dropping fragments to let it would corrupt its timeline, so it
# gets disconnected and rejoins at the live edge instead.
MAX_QUEUED_FRAGMENTS = 150

# Keyframe-anchored replay buffer handed to joining clients, capped so a
# camera with a very long (or absent) keyframe interval can't grow it without
# bound. ~10s at 15fps / 4MB.
MAX_REPLAY_FRAGMENTS = 150
MAX_REPLAY_BYTES = 4 * 1024 * 1024

# ISO-BMFF sample flags (trun/tfhd): ffmpeg marks a keyframe
# sample_depends_on=2 ("depends on nothing"), others non-sync.
_SAMPLE_FLAG_DEPENDS_NO = 0x02000000
_SAMPLE_FLAG_NON_SYNC = 0x00010000


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


def _iter_child_boxes(body):
    """Yield (type, payload) for each box in an ISO-BMFF container payload."""
    offset = 0
    while offset + 8 <= len(body):
        size = int.from_bytes(body[offset:offset + 4], "big")
        box_type = body[offset + 4:offset + 8]
        header = 8
        if size == 1:
            if offset + 16 > len(body):
                return
            size = int.from_bytes(body[offset + 8:offset + 16], "big")
            header = 16
        elif size == 0:
            size = len(body) - offset
        if size < header or offset + size > len(body):
            return  # truncated or malformed; stop rather than guess
        yield box_type, body[offset + header:offset + size]
        offset += size


def _sample_flags_are_sync(flags):
    return bool(flags & _SAMPLE_FLAG_DEPENDS_NO) and not flags & _SAMPLE_FLAG_NON_SYNC


def _trun_has_sync_sample(payload, default_sample_flags):
    """True if a trun box describes at least one keyframe sample."""
    if len(payload) < 8:
        return False
    flags = int.from_bytes(payload[1:4], "big")
    sample_count = int.from_bytes(payload[4:8], "big")
    offset = 8
    if flags & 0x000001:  # data-offset-present
        offset += 4
    if flags & 0x000004:  # first-sample-flags-present
        if len(payload) < offset + 4:
            return False
        if _sample_flags_are_sync(int.from_bytes(payload[offset:offset + 4], "big")):
            return True
        offset += 4
        sample_count -= 1  # the first sample's flags replace the per-sample ones

    entry_size = (4 * bool(flags & 0x000100) + 4 * bool(flags & 0x000200)
                  + 4 * bool(flags & 0x000400) + 4 * bool(flags & 0x000800))
    if not flags & 0x000400:
        # No per-sample flags: every remaining sample uses the track default.
        return (sample_count > 0 and default_sample_flags is not None
                and _sample_flags_are_sync(default_sample_flags))

    flags_offset = offset + 4 * bool(flags & 0x000100) + 4 * bool(flags & 0x000200)
    for index in range(max(sample_count, 0)):
        start = flags_offset + index * entry_size
        if start + 4 > len(payload):
            break
        if _sample_flags_are_sync(int.from_bytes(payload[start:start + 4], "big")):
            return True
    return False


def _tfhd_default_sample_flags(payload):
    """default_sample_flags from a tfhd box, or None if it carries none."""
    if len(payload) < 8:
        return None
    flags = int.from_bytes(payload[1:4], "big")
    if not flags & 0x000020:  # default-sample-flags-present
        return None
    offset = 8  # version/flags + track_ID
    if flags & 0x000001:  # base-data-offset-present
        offset += 8
    if flags & 0x000002:  # sample-description-index-present
        offset += 4
    if flags & 0x000008:  # default-sample-duration-present
        offset += 4
    if flags & 0x000010:  # default-sample-size-present
        offset += 4
    if offset + 4 > len(payload):
        return None
    return int.from_bytes(payload[offset:offset + 4], "big")


def fragment_has_keyframe(moof):
    """True if a complete moof box describes at least one keyframe sample.

    Used to anchor the replay buffer: a client that starts mid-GOP has no
    reference frames and renders nothing until the camera's next keyframe.
    """
    for box_type, payload in _iter_child_boxes(moof[8:]):
        if box_type != b"traf":
            continue
        default_sample_flags = None
        truns = []
        for child_type, child_payload in _iter_child_boxes(payload):
            if child_type == b"tfhd":
                default_sample_flags = _tfhd_default_sample_flags(child_payload)
            elif child_type == b"trun":
                truns.append(child_payload)
        for trun in truns:
            if _trun_has_sync_sample(trun, default_sample_flags):
                return True
    return False


class Subscriber:
    """One connected client's fragment queue, fed by the relay thread."""

    def __init__(self, generation, primed):
        self.generation = generation
        self._condition = threading.Condition()
        self._queue = collections.deque(primed)
        self._closed = False

    @property
    def closed(self):
        with self._condition:
            return self._closed and not self._queue

    def push(self, fragment):
        with self._condition:
            if self._closed:
                return
            if len(self._queue) >= MAX_QUEUED_FRAGMENTS:
                # Too far behind to ever catch up; close instead of dropping
                # fragments, which would corrupt what it has already buffered.
                self._closed = True
                self._queue.clear()
            else:
                self._queue.append(fragment)
            self._condition.notify_all()

    def close(self):
        """Stop accepting fragments; whatever is queued can still be read."""
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def next_fragment(self, timeout):
        """Next queued fragment, or None on timeout or once closed and drained."""
        with self._condition:
            self._condition.wait_for(lambda: self._queue or self._closed, timeout=timeout)
            if self._queue:
                return self._queue.popleft()
            return None


class CameraStream:
    def __init__(self, rtsp_url, crop=None, scale=None, fps=None):
        self.rtsp_url = rtsp_url
        self.crop = crop      # ffmpeg crop filter args: "w:h:x:y"
        self.scale = scale    # ffmpeg scale filter args: "w:h"
        self.fps = fps        # output frames/sec; only meaningful with crop/scale

        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._init_segment = None   # cached ftyp+moov, replayed to each new client
        self._replay = []           # fragments back to the last keyframe
        self._replay_bytes = 0
        self._have_keyframe = False
        self._generation = 0        # bumped per ffmpeg run; clients can't span two
        self._subscribers = []
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
                "clients": len(self._subscribers),
                "error": self._error,
            }

    def subscribe(self, timeout=10.0):
        """Register a client, primed with the init segment and the fragments
        back to the last keyframe. None if no header arrived within timeout."""
        with self._condition:
            ready = self._condition.wait_for(
                lambda: self._init_segment is not None and self._replay, timeout=timeout)
            if not ready:
                return None
            subscriber = Subscriber(self._generation, [self._init_segment] + self._replay)
            self._subscribers.append(subscriber)
            return subscriber

    def unsubscribe(self, subscriber):
        with self._lock:
            if subscriber in self._subscribers:
                self._subscribers.remove(subscriber)
        subscriber.close()

    def capture_snapshot(self, timeout=SNAPSHOT_TIMEOUT_S):
        """One-off still JPEG grab, independent of the live relay. Uses the
        same crop/scale as the live stream so it frames the same subject."""
        cmd = [
            "ffmpeg", "-loglevel", "error",
            "-rtsp_transport", "tcp",
            "-i", self.rtsp_url,
        ]
        filters = []
        if self.crop:
            filters.append(f"crop={self.crop}")
        if self.scale:
            filters.append(f"scale={self.scale}")
        if filters:
            cmd += ["-vf", ",".join(filters)]
        cmd += ["-frames:v", "1", "-f", "image2", "-q:v", "3", "pipe:1"]
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
            self._reset_for_reconnect()
            self._stop_event.wait(RECONNECT_DELAY_S)

    def _reset_for_reconnect(self):
        """Drop the cached header and cut every client loose.

        The next ffmpeg run writes a fresh moov and restarts timestamps at
        zero. A <video> that is already mid-playback can't follow that, and
        would just freeze on the discontinuity, so end each response instead:
        the client then reconnects and reads the new header from the start.
        """
        with self._lock:
            self._init_segment = None
            self._replay = []
            self._replay_bytes = 0
            self._have_keyframe = False
            self._generation += 1
            subscribers, self._subscribers = self._subscribers, []
        for subscriber in subscribers:
            subscriber.close()

    def _build_command(self):
        cmd = [
            "ffmpeg", "-loglevel", "error",
            "-rtsp_transport", "tcp",
            "-i", self.rtsp_url,
            "-an",
        ]

        filters = []
        if self.crop:
            filters.append(f"crop={self.crop}")
        if self.scale:
            filters.append(f"scale={self.scale}")

        if filters or self.fps:
            if filters:
                cmd += ["-vf", ",".join(filters)]
            if self.fps:
                cmd += ["-r", str(self.fps)]
            # Every fragment must stand alone for a client that just
            # connected, so every frame has to be a keyframe (cheap here:
            # low fps, and intra-only encoding skips motion estimation).
            cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
                    "-g", "1", "-keyint_min", "1"]
        else:
            cmd += ["-c:v", "copy"]

        cmd += [
            "-f", "mp4",
            "-movflags", "empty_moov+frag_every_frame+default_base_moof",
            "pipe:1",
        ]
        return cmd

    def _publish_fragment(self, fragment, is_keyframe):
        """Queue a fragment for every client and update the replay buffer."""
        with self._condition:
            if is_keyframe:
                self._replay = [fragment]
                self._replay_bytes = len(fragment)
                self._have_keyframe = True
            elif not self._have_keyframe:
                # Nothing decodable to anchor on yet (no keyframe seen since
                # this ffmpeg started, or the cap below gave up on one): hand
                # joining clients just the newest fragment and let their
                # decoder resynchronise on the camera's next keyframe.
                self._replay = [fragment]
                self._replay_bytes = len(fragment)
            elif (len(self._replay) >= MAX_REPLAY_FRAGMENTS
                    or self._replay_bytes + len(fragment) > MAX_REPLAY_BYTES):
                # Keyframe interval longer than we're willing to buffer.
                self._have_keyframe = False
                self._replay = [fragment]
                self._replay_bytes = len(fragment)
            else:
                self._replay.append(fragment)
                self._replay_bytes += len(fragment)

            self._last_frame_utc = datetime.now(timezone.utc).isoformat(
                timespec="milliseconds")
            self._connected = True
            self._error = None
            for subscriber in self._subscribers:
                subscriber.push(fragment)
            self._condition.notify_all()

    def _stream_once(self):
        cmd = self._build_command()
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
                    is_keyframe = fragment_has_keyframe(pending_moof)
                    fragment = pending_moof + box_bytes
                    pending_moof = None
                    last_frame_at = time.monotonic()
                    self._publish_fragment(fragment, is_keyframe)
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
