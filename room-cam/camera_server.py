"""
Room Cam — CAMERA SERVER (always-on control server; runs on the host).

This lightweight server runs on the machine with the webcam ALL THE TIME, but
the camera itself stays OFF (webcam light dark, ~no CPU) until a viewer asks
for it. The laptop viewer can:
    - turn the camera ON        -> POST /start   (opening /video also turns it on)
    - turn the camera OFF        -> POST /stop
    - check whether it's live    -> GET  /status
    - read the host's log lines   -> GET  /logs
    - watch the live feed         -> GET  /video

Everything is behind the same password.

Run this ONCE on the HOST and leave it running (or set it to auto-start at
boot). It does not tie the camera up while idle:
    pip install opencv-python flask
    python camera_server.py

Press Ctrl+C to stop the whole server.
"""

import datetime
import hmac
import logging
import socket
import threading
import time
from collections import deque

import cv2
from flask import Flask, Response, jsonify, request

# ---- Settings you can tweak ------------------------------------------------
CAMERA_INDEX = 0        # 0 = default webcam; try 1, 2 if you have more
PORT = 5000             # the web server port
JPEG_QUALITY = 80       # 0-100; lower = smaller/faster, higher = crisper
REOPEN_AFTER_FAILURES = 30   # consecutive bad reads before we try to reopen
QUIET_HOST = True       # True = host terminal stays silent; logs still go to
                        #        the viewer via /logs. False = also print here.

# ---- LAN auto-discovery ----------------------------------------------------
# The viewer finds this host by UDP broadcast, so there's NO hardcoded IP to
# set. Same-network only, and it needs no tokens or config -- run and forget.
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
# camera is None when OFF, or a cv2.VideoCapture when ON. The lock keeps the
# start/stop calls from colliding across the server's worker threads.
camera = None
camera_lock = threading.Lock()

# A rolling buffer of recent log lines. We serve this to the viewer at /logs so
# the host's messages can show up on your laptop, not just the host terminal.
LOG_BUFFER = deque(maxlen=200)


def log(msg):
    """Record one message in the buffer (for the viewer), and print it on the
    host only when QUIET_HOST is off."""
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


# Wire Werkzeug's request logging into our buffer, minus the polling requests.
_handler = _BufferHandler()
_handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))
_werkzeug_log = logging.getLogger("werkzeug")
_werkzeug_log.setLevel(logging.INFO)     # ensure request lines reach our handler
_werkzeug_log.addHandler(_handler)
_werkzeug_log.addFilter(_DropPolling())
if QUIET_HOST:
    # Stop request logs from bubbling up to the console; our buffer handler is
    # attached directly, so /logs still receives them.
    _werkzeug_log.propagate = False


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


def is_authorized(auth):
    """True only if the request carried the correct username + password."""
    if auth is None:
        return False
    user_ok = hmac.compare_digest(auth.username or "", USERNAME)
    pass_ok = hmac.compare_digest(auth.password or "", PASSWORD)
    return user_ok and pass_ok


@app.before_request
def require_login():
    """Runs before EVERY request (including /start, /stop, /logs). No valid
    login -> 401 challenge, which makes a browser show its login box."""
    if not is_authorized(request.authorization):
        return Response(
            "Login required.",
            401,
            {"WWW-Authenticate": 'Basic realm="Room Cam"'},
        )


def generate_frames():
    """Yield timestamped JPEG frames as an MJPEG stream while the camera is ON.

    Resilient: a single bad frame never kills the stream. If the camera is
    turned OFF (camera becomes None), the stream ends cleanly.
    """
    global camera
    consecutive_failures = 0

    while True:
        cam = camera            # snapshot; may become None if someone hits /stop
        if cam is None:
            break               # camera turned off -> end the stream

        try:
            success, frame = cam.read()

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
                frame,
                timestamp,
                (10, 30),
                cv2.FONT_HERSHEY_SIMPLEX,
                1.0,
                (0, 255, 0),
                2,
            )

            ok, buffer = cv2.imencode(
                ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY]
            )
            if not ok:
                continue

            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n" + buffer.tobytes() + b"\r\n"
            )

        except Exception as exc:
            log(f"[frame error] {exc}")
            time.sleep(0.1)
            continue


@app.route("/video")
def video():
    """The MJPEG stream. Opening it turns the camera on if it isn't already."""
    start_camera()
    return Response(
        generate_frames(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
    )


@app.route("/start", methods=["GET", "POST"])
def start():
    """Turn the camera ON."""
    changed = start_camera()
    return jsonify(active=camera is not None, changed=changed)


@app.route("/stop", methods=["GET", "POST"])
def stop():
    """Turn the camera OFF (shut the feed down; the server keeps listening)."""
    changed = stop_camera()
    return jsonify(active=camera is not None, changed=changed)


@app.route("/status")
def status():
    """Report whether the camera is currently ON."""
    return jsonify(active=camera is not None)


@app.route("/logs")
def logs():
    """Return the host's recent log lines so the viewer can show them."""
    return jsonify(lines=list(LOG_BUFFER))


def get_local_ip():
    """Find this machine's LAN IP (used in the discovery reply)."""
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
    """Answer LAN discovery pings so the viewer can find this host with no
    hardcoded IP. Listens for a UDP broadcast request and replies to the sender
    with our IP and stream port. No auth here -- it only reveals the LAN IP;
    the stream itself is still password-protected.
    """
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
    print("  Camera server is running.  (Camera is OFF until a viewer asks.)")
    print(f"  This machine's IP address:  {ip}")
    print("  The viewer finds this host automatically -- no IP to enter.")
    print("  Just run viewer.py on any machine on the same network.")
    print("  Press Ctrl+C to stop the whole server.")
    print("=" * 60)

    # Answer LAN discovery pings so the viewer needs no hardcoded IP.
    if ENABLE_DISCOVERY:
        threading.Thread(target=discovery_responder, daemon=True).start()

    # host="0.0.0.0" makes it reachable from other devices on the LAN.
    # threaded=True lets it serve control calls and the stream at the same time.
    app.run(host="0.0.0.0", port=PORT, threaded=True)
