"""
Room Cam Web — internet-accessible webcam security camera.

A spin-off of room-cam. Same timestamped MJPEG feed and password gate
(admin / 1337), but it opens a PUBLIC https tunnel with ngrok so you can watch
from ANY network — not just the same Wi-Fi. The camera stays OFF until someone
opens the feed (webcam light dark when nobody's watching).

Setup (once):
    pip install -r requirements.txt
    # Free ngrok account -> copy your authtoken:
    #   https://dashboard.ngrok.com/signup
    # Then register it ONE of these ways:
    #   ngrok config add-authtoken <YOUR_TOKEN>
    #   -- or set an environment variable named NGROK_AUTHTOKEN

Run:
    python webcam_server.py

It prints a PUBLIC url like https://ab12cd34.ngrok-free.app — open that in a
browser on any device, log in with admin / 1337, and you're watching.

>>> SECURITY <<<
This exposes your webcam to the public internet behind ONE password. The
default 'admin' / '1337' is a demo and is publicly known (it's in the repo),
so anyone who finds your URL could try it. CHANGE PASSWORD below to something
strong and private before you rely on this.
"""

import datetime
import hmac
import json
import logging
import os
import threading
import time
import urllib.request
from collections import deque

import cv2
from flask import Flask, Response, jsonify, request

# ---- Settings you can tweak ------------------------------------------------
CAMERA_INDEX = 0
PORT = 5000
JPEG_QUALITY = 80
REOPEN_AFTER_FAILURES = 30
ENABLE_TUNNEL = True    # False = run local-only (same-network), skip ngrok

# ---- Rendezvous mailbox (auto-tells the listener where to connect) ---------
# On startup the server writes its current public URL into this gist. The
# listener reads the gist to find the server automatically. Publishing needs a
# GITHUB_TOKEN env var with 'gist' scope; reading (the listener) needs neither.
PUBLISH_TO_GIST = True
GIST_ID = "51afc120ecb7715badd3c0ac391d8bdc"
GIST_FILENAME = "roomcam_url.txt"

# ---- Login -----------------------------------------------------------------
USERNAME = "admin"
PASSWORD = "1337"       # CHANGE THIS before exposing to the internet!
# ---------------------------------------------------------------------------

app = Flask(__name__)

camera = None
camera_lock = threading.Lock()
LOG_BUFFER = deque(maxlen=200)


def log(msg):
    line = f"{datetime.datetime.now():%H:%M:%S}  {msg}"
    LOG_BUFFER.append(line)
    print(line, flush=True)


class _BufferHandler(logging.Handler):
    def emit(self, record):
        try:
            LOG_BUFFER.append(self.format(record))
        except Exception:
            pass


class _DropPolling(logging.Filter):
    def filter(self, record):
        msg = record.getMessage()
        return ("/logs" not in msg) and ("/status" not in msg)


_handler = _BufferHandler()
_handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))
_werkzeug_log = logging.getLogger("werkzeug")
_werkzeug_log.setLevel(logging.INFO)
_werkzeug_log.addHandler(_handler)
_werkzeug_log.addFilter(_DropPolling())


def start_camera():
    global camera
    with camera_lock:
        if camera is None:
            camera = cv2.VideoCapture(CAMERA_INDEX)
            log("Camera turned ON.")
            return True
        return False


def stop_camera():
    global camera
    with camera_lock:
        if camera is not None:
            camera.release()
            camera = None
            log("Camera turned OFF.")
            return True
        return False


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
            {"WWW-Authenticate": 'Basic realm="Room Cam Web"'},
        )


def generate_frames():
    global camera
    consecutive_failures = 0
    while True:
        cam = camera
        if cam is None:
            break
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
                frame, timestamp, (10, 30),
                cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 0), 2,
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


# A small browser page so opening the public URL "just works" on any device.
INDEX_HTML = """<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Room Cam</title>
  <style>
    body { margin:0; background:#0b0b0d; color:#ddd;
           font-family: system-ui, sans-serif; text-align:center; }
    h1 { font-size:1rem; font-weight:600; padding:10px; margin:0;
         letter-spacing:.05em; color:#8f8; }
    img { max-width:100%; height:auto; display:block; margin:0 auto; }
  </style>
</head>
<body>
  <h1>ROOM CAM &mdash; LIVE</h1>
  <img src="/video" alt="Live feed">
</body>
</html>
"""


@app.route("/")
def index():
    """Browser-friendly page that embeds the live stream."""
    return INDEX_HTML


@app.route("/video")
def video():
    start_camera()
    return Response(
        generate_frames(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
    )


@app.route("/start", methods=["GET", "POST"])
def start():
    changed = start_camera()
    return jsonify(active=camera is not None, changed=changed)


@app.route("/stop", methods=["GET", "POST"])
def stop():
    changed = stop_camera()
    return jsonify(active=camera is not None, changed=changed)


@app.route("/status")
def status():
    return jsonify(active=camera is not None)


@app.route("/logs")
def logs():
    return jsonify(lines=list(LOG_BUFFER))


def open_public_tunnel(port):
    """Open an ngrok https tunnel and return the public URL, or None on failure.

    Requires pyngrok installed and an ngrok authtoken registered (via
    `ngrok config add-authtoken` or the NGROK_AUTHTOKEN environment variable).
    """
    try:
        from pyngrok import ngrok
    except ImportError:
        log("pyngrok not installed -> running local-only. `pip install pyngrok`")
        return None

    token = os.environ.get("NGROK_AUTHTOKEN")
    if token:
        ngrok.set_auth_token(token)

    try:
        tunnel = ngrok.connect(port, "http")
        return tunnel.public_url
    except Exception as exc:  # noqa: BLE001
        log(f"Could not open ngrok tunnel: {exc}")
        log("Make an account + register your authtoken, then retry.")
        return None


def publish_url_to_gist(url):
    """Write the current public URL into the gist mailbox so the listener can
    discover it. Needs GIST_ID set and a GITHUB_TOKEN env var with 'gist' scope.
    Returns True on success.
    """
    if not PUBLISH_TO_GIST or not GIST_ID:
        return False
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        log("GITHUB_TOKEN not set -> can't publish to the gist mailbox.")
        return False

    body = json.dumps({"files": {GIST_FILENAME: {"content": url}}}).encode()
    req = urllib.request.Request(
        f"https://api.github.com/gists/{GIST_ID}",
        data=body,
        method="PATCH",
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "room-cam-web",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            ok = resp.status == 200
        if ok:
            log(f"Published public URL to gist mailbox: {url}")
        else:
            log(f"Gist publish returned HTTP {resp.status}")
        return ok
    except Exception as exc:  # noqa: BLE001
        log(f"Could not publish URL to gist: {exc}")
        return False


def _startup_tunnel_and_publish():
    """Open the public tunnel and drop its URL in the mailbox. Runs in a
    background thread so the web server starts serving immediately."""
    public_url = open_public_tunnel(PORT)
    if public_url:
        log(f"PUBLIC url: {public_url}  (log in {USERNAME} / {PASSWORD})")
        publish_url_to_gist(public_url)
    else:
        log("No public tunnel -> serving on the local network only.")


if __name__ == "__main__":
    log("Room Cam Web starting. Camera is OFF until a viewer connects.")
    if PASSWORD == "1337":
        log("WARNING: default password '1337' is public — change it before "
            "relying on internet access.")

    # Open the tunnel + publish the URL in the background so the web server is
    # up instantly (and a headless exe never blocks on ngrok).
    if ENABLE_TUNNEL:
        threading.Thread(target=_startup_tunnel_and_publish, daemon=True).start()

    app.run(host="0.0.0.0", port=PORT, threaded=True)
