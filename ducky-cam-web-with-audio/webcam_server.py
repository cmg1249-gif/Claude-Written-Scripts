"""Camera/desktop host with listener-owned pairing and a visible tray stop control.

Microphone and desktop-speaker audio share a timestamped stereo stream.
Run webcam_server.exe, then choose the session password in viewer.exe.
"""

import argparse
import json
from pathlib import Path
import configparser
import datetime
import hmac
import logging
import os
import queue
import secrets
import struct
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
from collections import deque

import cv2
import sounddevice as sd
from flask import Flask, Response, jsonify, request
from werkzeug.serving import make_server
from combined_media import AudioMixer, desktop_frames, list_monitors, RATE, CHANNELS as MIX_CHANNELS
from connection import Tunnel, APP
from discovery import discovery_key, signature
from tls import HTTPS_CONTEXT

# ---- Keep helper processes windowless (Windows) ----------------------------
# pycloudflared starts cloudflared.exe with a plain subprocess.Popen, which on
# Windows opens a blank console window that then sits on the desktop for as
# long as the tunnel is up. There is no option to pass through, so add the
# "no window" creation flag to every child process this app starts.
if sys.platform == "win32":
    _CREATE_NO_WINDOW = 0x08000000

    class _WindowlessPopen(subprocess.Popen):
        def __init__(self, *args, **kwargs):
            kwargs["creationflags"] = kwargs.get("creationflags", 0) | _CREATE_NO_WINDOW
            super().__init__(*args, **kwargs)

    subprocess.Popen = _WindowlessPopen

# ---- Config: you should NEVER need to edit this code -----------------------
# Real settings are resolved at startup (see load_config) in this order:
#   1. environment variable  (ROOMCAM_PASSWORD, ROOMCAM_TOPIC, ROOMCAM_PORT, ...)
#   2. roomcam_config.ini     (written next to this file / the .exe)
#   3. the defaults below; passwords are set only by listener pairing.
DEFAULT_USERNAME = "admin"
DEFAULT_TOPIC = "roomcam-audio-relay-7hq2v9nk3d"  # ntfy rendezvous topic
DEFAULT_PORT = 5000
DEFAULT_CAMERA_INDEX = 0
DEFAULT_MIC_DEVICE = ""        # "" = default mic; or an index from `python -m sounddevice`
DEFAULT_AUDIO_RATE = 16000     # 16 kHz mono = voice quality, ~256 kbit/s upload
DEFAULT_TUNNEL = "yes"         # "no" = LAN only (skip Cloudflare + ntfy)
# Video over the internet must fit your UPLOAD speed or it queues up and lags
# seconds behind the audio. Measured at 640x480: quality 80 costs about
# 9 Mbit/s at 30 fps. Capturing at 1280x720 instead triples that to roughly
# 30 Mbit/s, which is why the capture size is left at the camera's default.
DEFAULT_VIDEO_FPS = 30
DEFAULT_JPEG_QUALITY = 80
DEFAULT_VIDEO_WIDTH = 640      # scale frames down to this width; 0 = no scaling
DEFAULT_CAPTURE_WIDTH = 0      # ask the camera for this size; 0 = its default
DEFAULT_CAPTURE_HEIGHT = 0     # both must be set to take effect
# video_fps and jpeg_quality are a CEILING, not a demand. With adaptive on,
# the host measures what it is actually achieving and backs off when the
# camera, the CPU or the uplink cannot keep up -- so the same settings work
# on a slow laptop as on a fast desktop. Set it to "no" to pin the values.
DEFAULT_ADAPTIVE = "yes"
MIN_FPS = 5                    # never drop below this
MIN_QUALITY = 40               # nor below this

# Filled in from config in __main__; functions read these globals at call time.
USERNAME = DEFAULT_USERNAME
PASSWORD = None
NTFY_TOPIC = DEFAULT_TOPIC
PORT = DEFAULT_PORT
CAMERA_INDEX = DEFAULT_CAMERA_INDEX
MIC_DEVICE = None
SAMPLE_RATE = DEFAULT_AUDIO_RATE
ENABLE_TUNNEL = True
VIDEO_FPS = DEFAULT_VIDEO_FPS
JPEG_QUALITY = DEFAULT_JPEG_QUALITY
VIDEO_WIDTH = DEFAULT_VIDEO_WIDTH
CAPTURE_WIDTH = DEFAULT_CAPTURE_WIDTH
CAPTURE_HEIGHT = DEFAULT_CAPTURE_HEIGHT
ADAPTIVE = True

# ---- Fixed knobs (rarely changed) ------------------------------------------
REOPEN_AFTER_FAILURES = 30
PUBLISH_TO_MAILBOX = True
REPUBLISH_SECONDS = 600
CHANNELS = 1
DTYPE = "int16"
BLOCK_SIZE = 640                # frames per audio chunk: 40 ms at 16 kHz
# Each audio chunk on the wire: host timestamp (double, START of the chunk),
# sequence number (uint32), payload length (uint16), then the PCM bytes.
# Length 0 = heartbeat ("still connected, mic is off").
AUDIO_HEADER = struct.Struct("!dIH")
# ---------------------------------------------------------------------------

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 4096
mixer = AudioMixer()
video_source = "camera"
monitor_id = 1
desktop_wanted = False
shutdown = threading.Event()
tunnel = None
session_id = secrets.token_urlsafe(16)
pair_token = secrets.token_urlsafe(32)
pair_started = time.monotonic()
pair_lock = threading.Lock()
reconnect_key = None

camera = None
camera_lock = threading.Lock()
# Whether a viewer WANTS video, as opposed to whether a camera is open right
# now. Selecting a camera that fails to open must not silently turn video off
# for good -- picking a working one afterwards has to bring it back.
camera_wanted = False
mic_wanted = False

mic_stream = None
mic_lock = threading.Lock()
audio_subscribers = []
subscribers_lock = threading.Lock()
audio_seq = 0

LOG_BUFFER = deque(maxlen=200)


def log(msg):
    line = f"{datetime.datetime.now():%H:%M:%S}  {msg}"
    LOG_BUFFER.append(line)
    if sys.stdout is not None:
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


# ---- Camera ----------------------------------------------------------------
def start_camera():
    """Turn the camera on. Returns False if there is no camera to turn on --
    a host with only a microphone is a perfectly good host."""
    global camera
    with camera_lock:
        if camera is not None:
            return False
        cam = cv2.VideoCapture(CAMERA_INDEX)
        if not cam.isOpened():
            cam.release()
            log(f"No camera at index {CAMERA_INDEX}; serving without video.")
            return False
        if CAPTURE_WIDTH and CAPTURE_HEIGHT:
            cam.set(cv2.CAP_PROP_FRAME_WIDTH, CAPTURE_WIDTH)
            cam.set(cv2.CAP_PROP_FRAME_HEIGHT, CAPTURE_HEIGHT)
        camera = cam
        log("Camera turned ON ({}x{}).".format(
            int(cam.get(cv2.CAP_PROP_FRAME_WIDTH)),
            int(cam.get(cv2.CAP_PROP_FRAME_HEIGHT))))
        return True


def stop_camera():
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
    mixer.feed_mic(indata.tobytes(), SAMPLE_RATE)


def start_mic():
    """Turn the mic ON. Tries the configured rate first, then common fallbacks
    (the actual rate is reported in /status so listeners can adapt)."""
    global mic_stream, SAMPLE_RATE
    with mic_lock:
        if mic_stream is not None:
            return False
        last_exc = None
        for rate in dict.fromkeys([SAMPLE_RATE, 16000, 44100, 48000]):
            stream = None
            try:
                stream = sd.InputStream(
                    samplerate=rate, channels=CHANNELS, dtype=DTYPE,
                    blocksize=int(rate * 0.04), device=MIC_DEVICE,
                    callback=lambda data, frames, info, status, rate=rate: mixer.feed_mic(data.tobytes(), rate),
                )
                stream.start()
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if stream is not None:
                    stream.close()
                continue
            if rate != SAMPLE_RATE:
                log(f"Mic does not support {SAMPLE_RATE} Hz; using {rate} Hz.")
                SAMPLE_RATE = rate
            mic_stream = stream
            mixer.set_mic(True)
            log("Mic turned ON.")
            return True
        log(f"Mic could not start: {last_exc}")
        return False


def stop_mic():
    global mic_stream
    mixer.set_mic(False)
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
    if auth is None or PASSWORD is None:
        return False
    user_ok = hmac.compare_digest((auth.username or "").encode(), USERNAME.encode())
    pass_ok = hmac.compare_digest((auth.password or "").encode(), PASSWORD.encode())
    return user_ok and pass_ok


@app.before_request
def require_login():
    if request.path in ('/pair', '/pair-info'):
        return None
    if not is_authorized(request.authorization):
        return Response(
            "Login required.",
            401,
            {"WWW-Authenticate": 'Basic realm="Ducky Cam Web"'},
        )


@app.get('/pair-info')
def pair_info():
    global pair_token, pair_started
    with pair_lock:
        if PASSWORD is not None:
            return jsonify(paired=True, session=session_id), 410
        if time.monotonic() - pair_started > 600:
            pair_token = secrets.token_urlsafe(32)
            pair_started = time.monotonic()
        return jsonify(paired=False, token=pair_token, session=session_id)


@app.post('/pair')
def pair():
    global PASSWORD, reconnect_key
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify(error='Expected pairing object'), 400
    token, password = payload.get('token'), payload.get('password')
    if not isinstance(token, str) or not isinstance(password, str) or not 8 <= len(password) <= 128:
        return jsonify(error='Choose an 8–128 character password in the listener.'), 400
    with pair_lock:
        if not hmac.compare_digest(token.encode(), pair_token.encode()):
            return jsonify(error='Pairing token rejected'), 403
        if PASSWORD is not None:
            if not hmac.compare_digest(PASSWORD.encode(), password.encode()):
                return jsonify(error='Already paired; restart the server to choose a new password.'), 410
        elif time.monotonic() - pair_started > 600:
            return jsonify(error='Pairing token expired; refresh discovery.'), 403
        else:
            PASSWORD = password
            reconnect_key = discovery_key(password, session_id)
            if tunnel:
                tunnel.changed.set()
    return jsonify(ok=True, session=session_id)


def advertisement(url):
    global pair_token, pair_started
    with pair_lock:
        data = dict(app=APP, url=url, session=session_id, paired=PASSWORD is not None, created=int(time.time()))
        if PASSWORD is None:
            pair_token = secrets.token_urlsafe(32)
            pair_started = time.monotonic()
            data['token'] = pair_token
        else:
            data['signature'] = signature(data, reconnect_key)
        return data


# ---- Streams ---------------------------------------------------------------
def generate_frames():
    """MJPEG stream. Each part carries X-Timestamp (host clock) and
    Content-Length so viewer.py can line frames up with the audio.

    The send rate adapts. VIDEO_FPS and JPEG_QUALITY are the ceiling; every
    few seconds we compare what we actually delivered against what we aimed
    for, and step down when the camera, the CPU or the uplink cannot keep up.
    Yielding a frame blocks until the client has taken it, so a slow viewer
    shows up here as a low achieved rate -- which is exactly the signal we
    want. When the pressure lifts we climb back toward the ceiling.
    """
    global camera
    consecutive_failures = 0
    target_fps = float(VIDEO_FPS) if VIDEO_FPS > 0 else 0.0
    quality = JPEG_QUALITY
    next_frame_at = time.monotonic()
    window_start = time.monotonic()
    window_frames = 0
    good_windows = 0
    lean_windows = 0
    first_window = True

    while True:
        cam = camera
        if cam is None:
            break
        try:
            # Read every frame the camera offers but only SEND on schedule, so
            # what goes out is always the freshest frame, never a stale queued
            # one. Reading and discarding is far cheaper than encoding.
            with camera_lock:
                if camera is not cam or video_source != 'camera':
                    break
                success, frame = cam.read()
            captured_at = time.monotonic()
            frame_interval = 1.0 / target_fps if target_fps > 0 else 0.0

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

            if frame_interval and captured_at < next_frame_at:
                continue
            next_frame_at = max(next_frame_at + frame_interval, captured_at)

            if VIDEO_WIDTH and frame.shape[1] > VIDEO_WIDTH:
                new_h = int(frame.shape[0] * VIDEO_WIDTH / frame.shape[1])
                frame = cv2.resize(frame, (VIDEO_WIDTH, new_h),
                                   interpolation=cv2.INTER_AREA)

            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cv2.putText(
                frame, timestamp, (10, 30),
                cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 0), 2,
            )
            ok, buffer = cv2.imencode(
                ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality]
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
            window_frames += 1

            # ---- adapt ------------------------------------------------------
            if not ADAPTIVE or target_fps <= 0:
                continue
            elapsed = time.monotonic() - window_start
            if elapsed < 3.0:
                continue
            achieved = window_frames / elapsed
            window_start = time.monotonic()
            window_frames = 0

            if first_window:
                # Cameras take a moment to hand over their first frames, and
                # judging the link on that would collapse the rate instantly.
                first_window = False
                continue

            if achieved < target_fps * 0.8:
                good_windows = 0
                lean_windows += 1
                if target_fps > MIN_FPS:
                    target_fps = max(MIN_FPS, min(float(VIDEO_FPS), achieved * 0.95))
                    log(f"Only managing {achieved:.1f} fps; aiming for "
                        f"{target_fps:.1f} instead.")
                elif quality > MIN_QUALITY and lean_windows >= 2:
                    # Already as slow as we go, so make each frame smaller.
                    # Two windows in a row, so one hiccup does not cost quality.
                    quality = max(MIN_QUALITY, quality - 10)
                    log(f"Still behind at {achieved:.1f} fps; "
                        f"dropping quality to {quality}.")
            elif achieved >= target_fps * 0.95:
                lean_windows = 0
                good_windows += 1
                if good_windows >= 3 and (target_fps < VIDEO_FPS or quality < JPEG_QUALITY):
                    good_windows = 0
                    if quality < JPEG_QUALITY:
                        quality = min(JPEG_QUALITY, quality + 10)
                        log(f"Link has room; quality back up to {quality}.")
                    else:
                        target_fps = min(float(VIDEO_FPS), target_fps * 1.25)
                        log(f"Link has room; aiming for {target_fps:.1f} fps.")

        except Exception as exc:  # noqa: BLE001
            log(f"[frame error] {exc}")
            time.sleep(0.1)
            continue


def generate_audio():
    yield from mixer.stream()


@app.route("/")
def index():
    return (Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "page.html").read_text(encoding="utf-8")


@app.route("/video")
def video():
    global camera_wanted, video_source, desktop_wanted
    if video_source == 'camera':
        camera_wanted = True
        start_camera()
        if camera is not None:
            return Response(generate_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')
        video_source = 'desktop'
        log('No camera: using desktop video instead.')
    desktop_wanted = True
    selected = monitor_id
    monitors = list_monitors()
    if selected not in [m['id'] for m in monitors]:
        return jsonify(error='Selected monitor unavailable; refresh displays.'), 404
    return Response(desktop_frames(selected, lambda: desktop_wanted and video_source == 'desktop'
                                   and monitor_id == selected and not shutdown.is_set()),
                    mimetype='multipart/x-mixed-replace; boundary=frame')


@app.get('/monitors')
def monitors():
    return jsonify(monitors=list_monitors(), selected=monitor_id)


@app.post('/source/select')
def source_select():
    global video_source, monitor_id, desktop_wanted, camera_wanted
    source = request.args.get('source', video_source)
    if source not in ('camera', 'desktop'):
        return jsonify(error='source must be camera or desktop'), 400
    try:
        selected = int(request.args.get('monitor', monitor_id))
    except (TypeError, ValueError):
        return jsonify(error='monitor must be a whole number'), 400
    if source == 'desktop' and selected not in [m['id'] for m in list_monitors()]:
        return jsonify(error='Monitor unavailable; refresh displays.'), 404
    video_source, monitor_id = source, selected
    desktop_wanted = source == 'desktop'
    camera_wanted = source == 'camera'
    if source == 'desktop':
        stop_camera()
    elif camera is None:
        start_camera()
    return jsonify(source=video_source, monitor=monitor_id, camera=camera is not None)


@app.post('/desktop-audio/start')
def desktop_audio_start():
    try:
        mixer.set_desktop(True)
        return jsonify(desktop_audio=True)
    except (OSError, ValueError, RuntimeError) as exc:
        return jsonify(desktop_audio=False, error=str(exc)), 503


@app.post('/desktop-audio/stop')
def desktop_audio_stop():
    mixer.set_desktop(False)
    return jsonify(desktop_audio=False)


@app.route("/audio")
def audio():
    """PCM audio stream. Does NOT turn the mic on by itself (use /mic/start),
    so the mic can be toggled while a listener stays connected."""
    # Content-Type is text/event-stream on purpose: cloudflared (and other
    # proxies) flush event streams write-by-write, but buffer other streaming
    # bodies into bursts, which makes the audio stutter. The body is still the
    # binary chunk format described at AUDIO_HEADER; listeners ignore the type.
    return Response(
        generate_audio(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---- Device discovery and selection ------------------------------------------
_camera_probe = None


def list_cameras(max_index=4):
    """Which camera indices actually open. OpenCV gives no portable way to
    read a camera's name, so they are offered by index. Probing opens each
    device briefly, so the answer is cached for the life of the process."""
    global _camera_probe
    if _camera_probe is not None:
        return _camera_probe
    found = []
    for i in range(max_index + 1):
        if camera is not None and i == CAMERA_INDEX:
            found.append(i)              # in use by us, so certainly present
            continue
        cap = cv2.VideoCapture(i)
        if cap.isOpened():
            found.append(i)
        cap.release()
    _camera_probe = found
    return found


def list_microphones():
    """Input devices, limited to the default host API so the list stays short.
    Windows reports the same microphone once per audio backend."""
    out = []
    try:
        devices_info = sd.query_devices()
    except Exception as exc:  # noqa: BLE001
        log(f"Could not list microphones: {exc}")
        return out
    try:
        preferred = sd.default.hostapi
    except Exception:  # noqa: BLE001
        preferred = None
    for idx, dev in enumerate(devices_info):
        if dev.get("max_input_channels", 0) < 1:
            continue
        if preferred is not None and dev.get("hostapi") != preferred and idx != MIC_DEVICE:
            continue
        out.append({"index": idx, "name": str(dev.get("name", "Input " + str(idx)))})
    return out


@app.route("/devices")
def devices():
    return jsonify(
        cameras=[{"index": i, "name": "Camera " + str(i)} for i in (list_cameras() if request.args.get("probe") == "1" else (_camera_probe or [CAMERA_INDEX]))],
        microphones=list_microphones(),
        monitors=list_monitors(), source=video_source, monitor=monitor_id,
        current_camera=CAMERA_INDEX,
        current_mic=MIC_DEVICE if MIC_DEVICE is not None else -1,
    )


@app.route("/camera/select", methods=["POST"])
def camera_select():
    """Switch to another camera, restarting it only if it was already on."""
    global CAMERA_INDEX
    try:
        index = int(request.values.get("index"))
    except (TypeError, ValueError):
        return jsonify(error="index must be a whole number"), 400
    was_on = camera is not None or camera_wanted
    stop_camera()
    CAMERA_INDEX = index
    started = start_camera() if was_on else True
    log("Camera selection set to index " + str(index) + ".")
    return jsonify(current_camera=CAMERA_INDEX, active=camera is not None,
                   ok=bool(started))


@app.route("/mic/select", methods=["POST"])
def mic_select():
    """Switch to another microphone. -1 or blank means the system default."""
    global MIC_DEVICE
    raw = request.values.get("index")
    try:
        index = None if raw in (None, "", "-1") else int(raw)
    except ValueError:
        return jsonify(error="index must be a whole number"), 400
    was_on = mic_stream is not None or mic_wanted
    stop_mic()
    MIC_DEVICE = index
    started = start_mic() if was_on else True
    log("Microphone selection set to " + (str(index) if index is not None else "default") + ".")
    return jsonify(current_mic=MIC_DEVICE if MIC_DEVICE is not None else -1,
                   mic=mic_stream is not None, ok=bool(started),
                   sample_rate=RATE)


@app.route("/start", methods=["POST"])
def start():
    global camera_wanted
    camera_wanted = True
    changed = start_camera()
    return jsonify(active=camera is not None, changed=changed)


@app.route("/stop", methods=["POST"])
def stop():
    global camera_wanted, desktop_wanted
    desktop_wanted = False
    camera_wanted = False
    changed = stop_camera()
    return jsonify(active=camera is not None, changed=changed)


@app.route("/mic/start", methods=["POST"])
def mic_start():
    global mic_wanted
    mic_wanted = True
    changed = start_mic()
    return jsonify(mic=mic_stream is not None, changed=changed,
                   sample_rate=RATE, channels=MIX_CHANNELS)


@app.route("/mic/stop", methods=["POST"])
def mic_stop():
    global mic_wanted
    mic_wanted = False
    changed = stop_mic()
    return jsonify(mic=mic_stream is not None, changed=changed)


@app.route("/status")
def status():
    return jsonify(active=camera is not None or desktop_wanted, mic=mic_stream is not None,
                   sample_rate=RATE, channels=MIX_CHANNELS, source=video_source, monitor=monitor_id,
                   desktop_audio=mixer.desktop_subscription is not None, audio_error=mixer.error,
                   session=tunnel.session if tunnel else session_id)


@app.route("/logs")
def logs():
    return jsonify(lines=list(LOG_BUFFER))


# ---- Config (promptable; never edit code) ----------------------------------
CONFIG_FILENAME = "roomcam_config.ini"
CONFIG_SECTION = "roomcam"


def _config_path():
    if os.environ.get("ROOMCAM_CONFIG"):
        return os.environ["ROOMCAM_CONFIG"]
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, CONFIG_FILENAME)


def _has_console():
    return sys.stdin is not None and sys.stdin.isatty()


def report_fatal(summary, detail="", title="Ducky Cam Web could not start"):
    """Make a failure visible.

    The host is built with --noconsole, so a problem has nowhere to print and
    the app just sits there doing nothing -- which is exactly what a broken
    tunnel or a bad config used to look like. Write the details to a log beside
    the exe and pop up a dialog.
    """
    path = os.path.join(os.path.dirname(_config_path()), "roomcam_error.log")
    stamp = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}"
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"\n===== {stamp} =====\n{summary}\n{detail}\n")
    except OSError:
        path = "(could not write a log file)"
    if sys.stdout is not None:
        print(f"{summary}\n{detail}")
    if not _has_console():
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(
                title, f"{summary}\n\nFull details were saved to:\n{path}"
            )
            root.destroy()
        except Exception:  # noqa: BLE001
            pass


def load_config():
    """Read optional device/network settings. Passwords are listener-owned."""
    path = _config_path()
    # interpolation=None: without it, configparser treats "%" as a variable
    # reference, so a password containing one raises ValueError on write and
    # on the next read. That crashed the no-console exe with no visible error.
    cfg = configparser.ConfigParser(interpolation=None)
    if os.path.exists(path):
        cfg.read(path)
    if not cfg.has_section(CONFIG_SECTION):
        cfg.add_section(CONFIG_SECTION)

    defaults = {
        "username": DEFAULT_USERNAME,
        "topic": DEFAULT_TOPIC,
        "port": str(DEFAULT_PORT),
        "camera_index": str(DEFAULT_CAMERA_INDEX),
        "mic_device": DEFAULT_MIC_DEVICE,
        "audio_rate": str(DEFAULT_AUDIO_RATE),
        "tunnel": DEFAULT_TUNNEL,
        "video_fps": str(DEFAULT_VIDEO_FPS),
        "jpeg_quality": str(DEFAULT_JPEG_QUALITY),
        "video_width": str(DEFAULT_VIDEO_WIDTH),
        "capture_width": str(DEFAULT_CAPTURE_WIDTH),
        "capture_height": str(DEFAULT_CAPTURE_HEIGHT),
        "adaptive": DEFAULT_ADAPTIVE,
    }
    values = {}
    for key, dflt in defaults.items():
        env = os.environ.get("ROOMCAM_" + key.upper())
        if env is not None and env != "":
            values[key] = env
        elif cfg.has_option(CONFIG_SECTION, key) and cfg.get(CONFIG_SECTION, key):
            values[key] = cfg.get(CONFIG_SECTION, key)
        else:
            values[key] = dflt

    return values


def _setting(cfg, key, convert, label):
    """Read one setting, and say which setting is wrong rather than dying with
    a bare ValueError the user can't act on."""
    raw = str(cfg[key]).strip()
    try:
        return convert(raw)
    except (TypeError, ValueError):
        raise SystemExit(
            f"The '{key}' setting in {_config_path()} is '{raw}', which is not "
            f"{label}. Fix that line (or delete the file to start fresh)."
        )


def main():
    global USERNAME, PASSWORD, NTFY_TOPIC, PORT, CAMERA_INDEX, MIC_DEVICE
    global SAMPLE_RATE, ENABLE_TUNNEL, VIDEO_FPS, JPEG_QUALITY, VIDEO_WIDTH
    global CAPTURE_WIDTH, CAPTURE_HEIGHT, ADAPTIVE

    _cfg = load_config()
    USERNAME = _cfg["username"]
    PASSWORD = None
    NTFY_TOPIC = _cfg["topic"]
    PORT = _setting(_cfg, "port", int, "a whole number")
    CAMERA_INDEX = _setting(_cfg, "camera_index", int, "a whole number")
    MIC_DEVICE = _setting(_cfg, "mic_device", int, "a whole number") \
        if str(_cfg["mic_device"]).strip() else None
    SAMPLE_RATE = _setting(_cfg, "audio_rate", int, "a whole number")
    ENABLE_TUNNEL = str(_cfg["tunnel"]).strip().lower() in ("1", "yes", "true", "on")
    VIDEO_FPS = _setting(_cfg, "video_fps", float, "a number")
    JPEG_QUALITY = _setting(_cfg, "jpeg_quality", int, "a whole number")
    VIDEO_WIDTH = _setting(_cfg, "video_width", int, "a whole number")
    CAPTURE_WIDTH = _setting(_cfg, "capture_width", int, "a whole number")
    CAPTURE_HEIGHT = _setting(_cfg, "capture_height", int, "a whole number")
    ADAPTIVE = str(_cfg["adaptive"]).strip().lower() in ("1", "yes", "true", "on")

    log("Ducky Cam Web with Audio starting. Camera + mic OFF until a viewer connects.")
    log(f"Config file: {_config_path()}")
    log("Change settings there or via ROOMCAM_* env vars -- no code edits.")
    parser = argparse.ArgumentParser(description='Camera and desktop sharing host')
    parser.add_argument('--local', action='store_true', help='Loopback only; no public tunnel')
    parser.add_argument('--port', type=int)
    parser.add_argument('--session-file', help='Write local test address')
    parser.add_argument('--stop-after', type=float, default=0)
    args = parser.parse_args()
    if args.local:
        ENABLE_TUNNEL = False
    if args.port is not None:
        PORT = args.port
    web = make_server('127.0.0.1' if ENABLE_TUNNEL or args.local else '0.0.0.0', PORT, app, threaded=True)
    PORT = web.server_port
    if args.session_file:
        Path(args.session_file).write_text(json.dumps({'url': f'http://127.0.0.1:{PORT}'}))
    global tunnel
    network = None
    if ENABLE_TUNNEL:
        tunnel = Tunnel(PORT, NTFY_TOPIC, None, log, advertisement, session_id)
        network = threading.Thread(target=tunnel.run, daemon=True)
        network.start()
    threading.Thread(target=web.serve_forever, daemon=True).start()
    mixer.start()
    try:
        run_tray(args.stop_after)
    finally:
        shutdown.set()
        stop_camera()
        stop_mic()
        mixer.close()
        if tunnel:
            tunnel.close()
            network.join(timeout=15)
        web.shutdown()
        web.server_close()


def run_tray(stop_after=0):
    import pystray
    from PIL import Image, ImageDraw
    image = Image.new('RGB', (64, 64), '#172b3a')
    draw = ImageDraw.Draw(image)
    draw.rectangle((10, 12, 54, 45), outline='#65e3a0', width=4)
    draw.ellipse((24, 22, 40, 38), fill='#65e3a0')
    def stop(icon=None, item=None):
        shutdown.set()
        if icon:
            icon.stop()
    icon = pystray.Icon('roomcam', image, 'RoomCam ready',
                        menu=pystray.Menu(pystray.MenuItem('Stop sharing', stop)))
    def setup(tray):
        tray.visible = True
        if stop_after:
            timer = threading.Timer(stop_after, lambda: stop(tray))
            timer.daemon = True
            timer.start()
        while not shutdown.wait(.5):
            active = camera is not None or desktop_wanted or mic_stream is not None or mixer.desktop_subscription is not None
            state = f'LIVE: {video_source}; mic {"on" if mic_stream else "off"}; desktop audio {"on" if mixer.desktop_subscription else "off"}' if active else (tunnel.status if tunnel else 'Local ready')
            tray.title = ('RoomCam - ' + state)[:127]
    icon.run(setup=setup)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except SystemExit as exc:          # our own friendly messages
        if exc.code not in (0, None):
            report_fatal(str(exc.code))
            sys.exit(1)
    except BaseException:              # noqa: BLE001 - last resort, must be seen
        report_fatal(
            "Ducky Cam Web hit an unexpected error and stopped.",
            traceback.format_exc(),
        )
        sys.exit(1)
