"""
Room Cam Web — internet-accessible webcam (install, run, forget).

The host needs NO account, NO token, and NO configuration:
  1. It opens a Cloudflare "quick tunnel" -> a public https URL (no login).
  2. It posts that URL to a public ntfy.sh topic (no login) so the listener can
     find it, and re-posts every few minutes to keep it fresh.
The camera stays OFF until someone opens the feed (webcam light dark when idle).

Run (or just double-click the exe):
    pip install -r requirements.txt
    python webcam_server.py

The listener (viewer.py, on your laptop) reads the ntfy topic, finds this
server automatically, and opens the feed. Log in with admin / 1337.

>>> SECURITY <<<
This exposes your webcam to the public internet behind ONE password. The ntfy
topic is public (anyone who knows it can read the current URL), so the password
is the real gate. '1337' is a demo default and is publicly known -- change
PASSWORD below before you rely on this.
"""

import configparser
import datetime
import hmac
import json
import logging
import os
import sys
import threading
import time
import urllib.request
from collections import deque

import cv2
from flask import Flask, Response, jsonify, request

# ---- Config: you should NEVER need to edit this code -----------------------
# Real settings are resolved at startup (see load_config) in this order:
#   1. environment variable  (ROOMCAM_PASSWORD, ROOMCAM_TOPIC, ROOMCAM_PORT, ...)
#   2. roomcam_config.ini     (written next to this file / the .exe)
#   3. a first-run prompt      (console, or a pop-up for the no-console exe)
#   4. the defaults below
# The values below are only starting points; the .ini is the source of truth.
DEFAULT_USERNAME = "admin"
DEFAULT_PASSWORD = "1337"      # public demo value; you'll be asked to change it
DEFAULT_TOPIC = "roomcam-relay-3f9k2m7qx4"  # ntfy rendezvous topic (shared name)
DEFAULT_PORT = 5000
DEFAULT_CAMERA_INDEX = 0

# Filled in from config in __main__; functions read these globals at call time.
USERNAME = DEFAULT_USERNAME
PASSWORD = DEFAULT_PASSWORD
NTFY_TOPIC = DEFAULT_TOPIC
PORT = DEFAULT_PORT
CAMERA_INDEX = DEFAULT_CAMERA_INDEX

# ---- Fixed knobs (rarely changed) ------------------------------------------
JPEG_QUALITY = 80
REOPEN_AFTER_FAILURES = 30
ENABLE_TUNNEL = True        # False = local-only (same-network), skip the tunnel
PUBLISH_TO_MAILBOX = True   # post the public URL to the ntfy rendezvous topic
REPUBLISH_SECONDS = 600     # re-post the URL this often so it stays fresh
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
    """Open a Cloudflare quick tunnel and return the public https URL, or None.

    Quick tunnels need NO account and NO token -- just the cloudflared binary,
    which pycloudflared downloads automatically on first use.
    """
    try:
        from pycloudflared import try_cloudflare
    except ImportError:
        log("pycloudflared not installed -> local only. `pip install pycloudflared`")
        return None
    try:
        return try_cloudflare(port=port).tunnel
    except Exception as exc:  # noqa: BLE001
        log(f"Could not open Cloudflare tunnel: {exc}")
        return None


def publish_url_to_mailbox(url):
    """Post the current public URL to the ntfy.sh topic. No token needed."""
    if not PUBLISH_TO_MAILBOX or not NTFY_TOPIC:
        return False
    req = urllib.request.Request(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=url.encode(),
        method="POST",
        headers={"Title": "roomcam-url", "User-Agent": "room-cam-web"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            ok = resp.status == 200
        if ok:
            log(f"Published public URL to mailbox: {url}")
        return ok
    except Exception as exc:  # noqa: BLE001
        log(f"Could not publish URL to mailbox: {exc}")
        return False


def _startup_tunnel_and_publish():
    """Open the tunnel, then keep the URL fresh in the mailbox. Runs in a
    background thread so the web server starts serving immediately."""
    public_url = open_public_tunnel(PORT)
    if not public_url:
        log("No public tunnel -> serving on the local network only.")
        return
    log(f"PUBLIC url: {public_url}  (log in {USERNAME} / {PASSWORD})")
    while True:
        publish_url_to_mailbox(public_url)
        time.sleep(REPUBLISH_SECONDS)


CONFIG_FILENAME = "roomcam_config.ini"
CONFIG_SECTION = "roomcam"


def _config_path():
    """Where the config lives: next to the .exe when frozen, else next to this
    script. So a double-clicked exe just reads a plain .ini beside it."""
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, CONFIG_FILENAME)


def _ask(prompt_text, default):
    """Ask for one value. Uses a console prompt when there is a console, falls
    back to a small pop-up for the no-console exe, else keeps the default."""
    if sys.stdin is not None and sys.stdin.isatty():
        try:
            return input(f"{prompt_text} [{default}]: ").strip() or default
        except EOFError:
            return default
    try:
        import tkinter as tk
        from tkinter import simpledialog
        root = tk.Tk()
        root.withdraw()
        entered = simpledialog.askstring(
            "Room Cam Web setup", prompt_text, initialvalue=default
        )
        root.destroy()
        return (entered or default).strip()
    except Exception:
        return default


def load_config():
    """Resolve settings (env var -> .ini -> first-run prompt -> default) and
    write them back to roomcam_config.ini, so nothing has to live in the code."""
    path = _config_path()
    cfg = configparser.ConfigParser()
    if os.path.exists(path):
        cfg.read(path)
    if not cfg.has_section(CONFIG_SECTION):
        cfg.add_section(CONFIG_SECTION)

    defaults = {
        "username": DEFAULT_USERNAME,
        "password": DEFAULT_PASSWORD,
        "topic": DEFAULT_TOPIC,
        "port": str(DEFAULT_PORT),
        "camera_index": str(DEFAULT_CAMERA_INDEX),
    }
    values = {}
    for key, dflt in defaults.items():
        env = os.environ.get("ROOMCAM_" + key.upper())
        if env:
            values[key] = env
        elif cfg.has_option(CONFIG_SECTION, key) and cfg.get(CONFIG_SECTION, key):
            values[key] = cfg.get(CONFIG_SECTION, key)
        else:
            values[key] = dflt

    # First run with no config: prompt for a real password. This camera goes on
    # the public internet -- the password is the only gate, so don't ship 1337.
    if not os.path.exists(path) and values["password"] == DEFAULT_PASSWORD:
        values["password"] = _ask(
            "Set a viewer password (gates internet access)", DEFAULT_PASSWORD
        )

    for key in defaults:
        cfg.set(CONFIG_SECTION, key, str(values[key]))
    try:
        with open(path, "w", encoding="utf-8") as fh:
            cfg.write(fh)
    except OSError as exc:
        log(f"Could not write config {path}: {exc}")
    return values


if __name__ == "__main__":
    _cfg = load_config()
    USERNAME = _cfg["username"]
    PASSWORD = _cfg["password"]
    NTFY_TOPIC = _cfg["topic"]
    PORT = int(_cfg["port"])
    CAMERA_INDEX = int(_cfg["camera_index"])

    log("Room Cam Web starting. Camera is OFF until a viewer connects.")
    log(f"Config file: {_config_path()}")
    log("Change settings there or via ROOMCAM_* env vars -- no code edits.")
    if PASSWORD == DEFAULT_PASSWORD:
        log("WARNING: password is still the public demo value -- set a real one "
            "in the config file before relying on internet access.")

    if ENABLE_TUNNEL:
        threading.Thread(target=_startup_tunnel_and_publish, daemon=True).start()

    app.run(host="0.0.0.0", port=PORT, threaded=True)
