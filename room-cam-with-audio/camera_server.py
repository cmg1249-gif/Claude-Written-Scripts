"""
Room Cam with Audio — CAMERA SERVER (always-on control server; runs on the host).

Same as Room Cam v2, plus the host MICROPHONE. Camera and mic both stay OFF
(no CPU, no webcam light) until a viewer asks. The laptop viewer can:
    - turn the camera ON        -> POST /start   (opening /video also turns it on)
    - turn the camera OFF        -> POST /stop
    - turn the mic ON            -> POST /mic/start
    - turn the mic OFF           -> POST /mic/stop
    - check what's live          -> GET  /status     {"active": .., "mic": ..}
    - read the host's log lines   -> GET  /logs
    - watch the live feed         -> GET  /video     (MJPEG, each frame timestamped)
    - hear the live mic           -> GET  /audio     (raw PCM chunks, timestamped)

Everything is behind the same password. Frames and audio chunks carry the
host's clock so the viewer can keep them in sync.

Run this ONCE on the HOST and leave it running:
    pip install opencv-python flask sounddevice numpy
    python camera_server.py

Press Ctrl+C to stop the whole server.
"""

import datetime
import hmac
import logging
import queue
import socket
import struct
import threading
import time
from collections import deque

import cv2
import sounddevice as sd
from flask import Flask, Response, jsonify, request

# ---- Settings you can tweak ------------------------------------------------
CAMERA_INDEX = 0        # 0 = default webcam; try 1, 2 if you have more
PORT = 5000             # the web server port
JPEG_QUALITY = 80       # 0-100; lower = smaller/faster, higher = crisper
REOPEN_AFTER_FAILURES = 30   # consecutive bad reads before we try to reopen
QUIET_HOST = True       # True = host terminal stays silent; logs still go to
                        #        the viewer via /logs. False = also print here.

# ---- Microphone --------------------------------------------------------------
MIC_DEVICE = None       # None = default mic; or an index from `python -m sounddevice`
SAMPLE_RATE = 44100
CHANNELS = 1
DTYPE = "int16"
BLOCK_SIZE = 1024       # frames per audio chunk (~23 ms)
BLOCK_SECONDS = BLOCK_SIZE / SAMPLE_RATE
# Each audio chunk on the wire: host timestamp (double, start of the chunk),
# sequence number (uint32), payload length (uint16), then the PCM bytes.
# A chunk with length 0 is a heartbeat: "still connected, mic is off/quiet".
AUDIO_HEADER = struct.Struct("!dIH")

# ---- LAN auto-discovery ----------------------------------------------------
ENABLE_DISCOVERY = True
DISCOVERY_PORT = 50505
DISCOVERY_REQUEST = b"ROOMCAM_DISCOVERY_V1"
DISCOVERY_REPLY_PREFIX = b"ROOMCAM_HERE"

# ---- Login (change these!) -------------------------------------------------
USERNAME = "admin"          # who has to log in
PASSWORD = "1337"           # CHANGE THIS to something only you know
# ---------------------------------------------------------------------------

app = Flask(__name__)

# ---- Shared state ----------------------------------------------------------
camera = None
camera_lock = threading.Lock()

mic_stream = None                   # None when OFF, sd.InputStream when ON
mic_lock = threading.Lock()
audio_subscribers = []              # one queue per connected /audio listener
subscribers_lock = threading.Lock()
audio_seq = 0

LOG_BUFFER = deque(maxlen=200)


def log(msg):
    line = f"{datetime.datetime.now():%H:%M:%S}  {msg}"
    LOG_BUFFER.append(line)
    if not QUIET_HOST:
        print(line, flush=True)


class _BufferHandler(logging.Handler):
    """Feeds Flask/Werkzeug's own per-request logs into the same buffer."""

    def emit(self, record):
        try:
            LOG_BUFFER.append(self.format(record))
        except Exception:
            pass


class _DropPolling(logging.Filter):
    """Keep the viewer's own /logs and /status polls out of the log noise."""

    def filter(self, record):
        msg = record.getMessage()
        return ("/logs" not in msg) and ("/status" not in msg)


_handler = _BufferHandler()
_handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))
_werkzeug_log = logging.getLogger("werkzeug")
_werkzeug_log.setLevel(logging.INFO)
_werkzeug_log.addHandler(_handler)
_werkzeug_log.addFilter(_DropPolling())
if QUIET_HOST:
    _werkzeug_log.propagate = False


# ---- Camera ----------------------------------------------------------------
def start_camera():
    """Turn the camera ON if it's off. Returns True if it actually changed."""
    global camera
    with camera_lock:
        if camera is None:
            camera = cv2.VideoCapture(CAMERA_INDEX)
            log("Camera turned ON.")
            return True
        return False


def stop_camera():
    """Turn the camera OFF if it's on. Returns True if it actually changed."""
    global camera
    with camera_lock:
        if camera is not None:
            camera.release()
            camera = None
            log("Camera turned OFF.")
            return True
        return False


# ---- Microphone --------------------------------------------------------------
def _mic_callback(indata, frames, time_info, status):
    """sounddevice calls this every BLOCK_SIZE frames. Stamp the chunk with the
    host clock and hand a copy to every connected /audio listener."""
    global audio_seq
    ts = time.monotonic() - BLOCK_SECONDS       # when this chunk STARTED
    pcm = indata.tobytes()
    packet = AUDIO_HEADER.pack(ts, audio_seq & 0xFFFFFFFF, len(pcm)) + pcm
    audio_seq += 1
    with subscribers_lock:
        subs = list(audio_subscribers)
    for q in subs:
        try:
            q.put_nowait(packet)
        except queue.Full:
            # Listener is falling behind: drop its oldest chunk so it catches
            # up instead of lagging further and further.
            try:
                q.get_nowait()
            except queue.Empty:
                pass
            try:
                q.put_nowait(packet)
            except queue.Full:
                pass


def start_mic():
    """Turn the mic ON if it's off. Returns True if it actually changed."""
    global mic_stream
    with mic_lock:
        if mic_stream is not None:
            return False
        try:
            stream = sd.InputStream(
                samplerate=SAMPLE_RATE, channels=CHANNELS, dtype=DTYPE,
                blocksize=BLOCK_SIZE, device=MIC_DEVICE, callback=_mic_callback,
            )
            stream.start()
        except Exception as exc:  # noqa: BLE001
            log(f"Mic could not start: {exc}")
            return False
        mic_stream = stream
        log("Mic turned ON.")
        return True


def stop_mic():
    """Turn the mic OFF if it's on. Returns True if it actually changed."""
    global mic_stream
    with mic_lock:
        if mic_stream is None:
            return False
        try:
            mic_stream.stop()
            mic_stream.close()
        except Exception as exc:  # noqa: BLE001
            log(f"Mic stop error: {exc}")
        mic_stream = None
        log("Mic turned OFF.")
        return True


# ---- Auth ------------------------------------------------------------------
def is_authorized(auth):
    if auth is None:
        return False
    user_ok = hmac.compare_digest(auth.username or "", USERNAME)
    pass_ok = hmac.compare_digest(auth.password or "", PASSWORD)
    return user_ok and pass_ok


@app.before_request
def require_login():
    if not is_authorized(request.authorization):
        return Response(
            "Login required.",
            401,
            {"WWW-Authenticate": 'Basic realm="Room Cam"'},
        )


# ---- Streams ---------------------------------------------------------------
def generate_frames():
    """Yield timestamped JPEG frames as an MJPEG stream while the camera is ON.

    Each part carries X-Timestamp (host clock, seconds) and Content-Length so
    the viewer can line frames up with the audio. Browsers ignore the extras.
    """
    global camera
    consecutive_failures = 0

    while True:
        cam = camera
        if cam is None:
            break

        try:
            success, frame = cam.read()
            captured_at = time.monotonic()

            if not success:
                consecutive_failures += 1
                time.sleep(0.1)
                if consecutive_failures >= REOPEN_AFTER_FAILURES:
                    log("Camera unresponsive; attempting to reopen...")
                    with camera_lock:
                        if camera is not None:
                            camera.release()
                            camera = cv2.VideoCapture(CAMERA_INDEX)
                    consecutive_failures = 0
                continue

            consecutive_failures = 0

            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cv2.putText(
                frame, timestamp, (10, 30), cv2.FONT_HERSHEY_SIMPLEX,
                1.0, (0, 255, 0), 2,
            )

            ok, buffer = cv2.imencode(
                ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY]
            )
            if not ok:
                continue
            jpeg = buffer.tobytes()

            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n"
                + f"X-Timestamp: {captured_at:.6f}\r\n".encode()
                + f"Content-Length: {len(jpeg)}\r\n\r\n".encode()
                + jpeg + b"\r\n"
            )

        except Exception as exc:  # noqa: BLE001
            log(f"[frame error] {exc}")
            time.sleep(0.1)
            continue


def generate_audio():
    """Yield audio chunks to one listener for as long as it stays connected.

    While the mic is OFF (or between chunks) we send a zero-length heartbeat
    once a second so a vanished listener is noticed and cleaned up.
    """
    q = queue.Queue(maxsize=64)
    with subscribers_lock:
        audio_subscribers.append(q)
    log("Audio listener connected.")
    try:
        while True:
            try:
                yield q.get(timeout=1.0)
            except queue.Empty:
                yield AUDIO_HEADER.pack(time.monotonic(), 0, 0)
    finally:
        with subscribers_lock:
            if q in audio_subscribers:
                audio_subscribers.remove(q)
        log("Audio listener disconnected.")


@app.route("/video")
def video():
    """The MJPEG stream. Opening it turns the camera on if it isn't already."""
    start_camera()
    return Response(
        generate_frames(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
    )


@app.route("/audio")
def audio():
    """The raw PCM audio stream (see AUDIO_HEADER). Does NOT turn the mic on
    by itself -- use /mic/start, so the mic can be toggled while listening."""
    return Response(
        generate_audio(),
        mimetype="application/octet-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---- Controls ----------------------------------------------------------------
@app.route("/start", methods=["GET", "POST"])
def start():
    changed = start_camera()
    return jsonify(active=camera is not None, changed=changed)


@app.route("/stop", methods=["GET", "POST"])
def stop():
    changed = stop_camera()
    return jsonify(active=camera is not None, changed=changed)


@app.route("/mic/start", methods=["GET", "POST"])
def mic_start():
    changed = start_mic()
    return jsonify(mic=mic_stream is not None, changed=changed)


@app.route("/mic/stop", methods=["GET", "POST"])
def mic_stop():
    changed = stop_mic()
    return jsonify(mic=mic_stream is not None, changed=changed)


@app.route("/status")
def status():
    return jsonify(active=camera is not None, mic=mic_stream is not None)


@app.route("/logs")
def logs():
    return jsonify(lines=list(LOG_BUFFER))


# ---- Discovery -------------------------------------------------------------
def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except OSError:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


def discovery_responder():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("", DISCOVERY_PORT))
    except OSError as exc:
        log(f"Discovery responder could not bind UDP {DISCOVERY_PORT}: {exc}")
        return

    log(f"Discovery responder listening on UDP {DISCOVERY_PORT}.")
    while True:
        try:
            data, addr = sock.recvfrom(1024)
        except OSError:
            continue
        if data.strip() == DISCOVERY_REQUEST:
            reply = DISCOVERY_REPLY_PREFIX + f":{get_local_ip()}:{PORT}".encode()
            try:
                sock.sendto(reply, addr)
            except OSError:
                pass


if __name__ == "__main__":
    ip = get_local_ip()
    print("=" * 60)
    print("  Camera server is running.  (Camera + mic OFF until a viewer asks.)")
    print(f"  This machine's IP address:  {ip}")
    print("  The viewer finds this host automatically -- no IP to enter.")
    print("  Just run viewer.py on any machine on the same network.")
    print("  Press Ctrl+C to stop the whole server.")
    print("=" * 60)

    if ENABLE_DISCOVERY:
        threading.Thread(target=discovery_responder, daemon=True).start()

    app.run(host="0.0.0.0", port=PORT, threaded=True)
