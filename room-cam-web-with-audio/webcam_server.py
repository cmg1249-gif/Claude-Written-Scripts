"""
Room Cam Web with Audio — internet-accessible webcam + microphone.

Same as Room Cam Web v2.0 (tokenless Cloudflare quick tunnel + public ntfy.sh
mailbox, promptable config, NO accounts), plus the host MICROPHONE:
    - GET  /audio        raw PCM chunks, each stamped with the host clock
    - POST /mic/start    mic ON
    - POST /mic/stop     mic OFF
    - GET  /status       {"active": .., "mic": .., "sample_rate": .., "channels": ..}
The browser page gets a "Listen" button and a "Mic on/off" button; viewer.py
plays video + audio in sync with an `m` key to toggle the mic.

Camera and mic both stay OFF until someone asks (webcam light dark when idle).

Run (or just double-click the exe):
    pip install -r requirements.txt
    python webcam_server.py

>>> SECURITY <<<
This exposes your webcam AND microphone to the public internet behind ONE
password. The ntfy topic is public, so the password is the real gate. '1337' is
a demo default -- you're asked to change it on first run.
"""

import configparser
import datetime
import hmac
import logging
import os
import queue
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
#   3. a first-run prompt      (console, or a pop-up for the no-console exe)
#   4. the defaults below
DEFAULT_USERNAME = "admin"
DEFAULT_PASSWORD = "1337"      # public demo value; you'll be asked to change it
DEFAULT_TOPIC = "roomcam-audio-relay-7hq2v9nk3d"  # ntfy rendezvous topic
DEFAULT_PORT = 5000
DEFAULT_CAMERA_INDEX = 0
DEFAULT_MIC_DEVICE = ""        # "" = default mic; or an index from `python -m sounddevice`
DEFAULT_AUDIO_RATE = 16000     # 16 kHz mono = voice quality, ~256 kbit/s upload
DEFAULT_TUNNEL = "yes"         # "no" = LAN only (skip Cloudflare + ntfy)
# Video over the internet must fit your UPLOAD speed or it queues up and lags
# seconds behind the audio. 640 px wide at 20 fps / quality 70 is ~3 Mbit/s;
# 30 fps / quality 80 is ~6 Mbit/s. Raise these only if your upload is fast.
DEFAULT_VIDEO_FPS = 20
DEFAULT_JPEG_QUALITY = 70
DEFAULT_VIDEO_WIDTH = 640      # 0 = camera's native width

# Filled in from config in __main__; functions read these globals at call time.
USERNAME = DEFAULT_USERNAME
PASSWORD = DEFAULT_PASSWORD
NTFY_TOPIC = DEFAULT_TOPIC
PORT = DEFAULT_PORT
CAMERA_INDEX = DEFAULT_CAMERA_INDEX
MIC_DEVICE = None
SAMPLE_RATE = DEFAULT_AUDIO_RATE
ENABLE_TUNNEL = True
VIDEO_FPS = DEFAULT_VIDEO_FPS
JPEG_QUALITY = DEFAULT_JPEG_QUALITY
VIDEO_WIDTH = DEFAULT_VIDEO_WIDTH

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

camera = None
camera_lock = threading.Lock()

mic_stream = None
mic_lock = threading.Lock()
audio_subscribers = []
subscribers_lock = threading.Lock()
audio_seq = 0

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


# ---- Camera ----------------------------------------------------------------
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


# ---- Microphone --------------------------------------------------------------
def _mic_callback(indata, frames, time_info, status):
    """Stamp each captured chunk with the host clock and hand a copy to every
    connected /audio listener."""
    global audio_seq
    ts = time.monotonic() - frames / SAMPLE_RATE      # when this chunk STARTED
    pcm = indata.tobytes()
    packet = AUDIO_HEADER.pack(ts, audio_seq & 0xFFFFFFFF, len(pcm)) + pcm
    audio_seq += 1
    with subscribers_lock:
        subs = list(audio_subscribers)
    for q in subs:
        try:
            q.put_nowait(packet)
        except queue.Full:
            try:
                q.get_nowait()          # listener too slow: drop its oldest
            except queue.Empty:
                pass
            try:
                q.put_nowait(packet)
            except queue.Full:
                pass


def start_mic():
    """Turn the mic ON. Tries the configured rate first, then common fallbacks
    (the actual rate is reported in /status so listeners can adapt)."""
    global mic_stream, SAMPLE_RATE
    with mic_lock:
        if mic_stream is not None:
            return False
        last_exc = None
        for rate in dict.fromkeys([SAMPLE_RATE, 16000, 44100, 48000]):
            try:
                stream = sd.InputStream(
                    samplerate=rate, channels=CHANNELS, dtype=DTYPE,
                    blocksize=int(rate * 0.04), device=MIC_DEVICE,
                    callback=_mic_callback,
                )
                stream.start()
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                continue
            if rate != SAMPLE_RATE:
                log(f"Mic does not support {SAMPLE_RATE} Hz; using {rate} Hz.")
                SAMPLE_RATE = rate
            mic_stream = stream
            log("Mic turned ON.")
            return True
        log(f"Mic could not start: {last_exc}")
        return False


def stop_mic():
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
            {"WWW-Authenticate": 'Basic realm="Room Cam Web"'},
        )


# ---- Streams ---------------------------------------------------------------
def generate_frames():
    """MJPEG stream. Each part carries X-Timestamp (host clock) and
    Content-Length so viewer.py can line frames up with the audio."""
    global camera
    consecutive_failures = 0
    frame_interval = 1.0 / VIDEO_FPS if VIDEO_FPS > 0 else 0.0
    next_frame_at = time.monotonic()
    while True:
        cam = camera
        if cam is None:
            break
        try:
            # Pace to VIDEO_FPS. The camera still runs at its own rate; we just
            # read (and discard) frames until it's time to send one, so what
            # we send is always the freshest frame, not a stale queued one.
            success, frame = cam.read()
            captured_at = time.monotonic()
            if success and frame_interval and captured_at < next_frame_at:
                continue
            next_frame_at = max(next_frame_at + frame_interval, captured_at)
            if success and VIDEO_WIDTH and frame.shape[1] > VIDEO_WIDTH:
                new_h = int(frame.shape[0] * VIDEO_WIDTH / frame.shape[1])
                frame = cv2.resize(frame, (VIDEO_WIDTH, new_h), interpolation=cv2.INTER_AREA)
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
    """Audio chunks to one listener for as long as it stays connected. While
    the mic is OFF we send a zero-length heartbeat once a second so a vanished
    listener is noticed and cleaned up."""
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
    .bar { padding:10px; display:flex; gap:10px; justify-content:center; }
    button { background:#1c1c22; color:#ddd; border:1px solid #444;
             border-radius:6px; padding:8px 14px; font-size:.95rem; }
    button.on { border-color:#8f8; color:#8f8; }
    #st { font-size:.8rem; color:#888; padding-bottom:10px; }
  </style>
</head>
<body>
  <h1>ROOM CAM &mdash; LIVE</h1>
  <img src="/video" alt="Live feed">
  <div class="bar">
    <button id="listen">&#128264; Listen</button>
    <button id="mic">&#127908; Mic: ?</button>
  </div>
  <div id="st"></div>
<script>
// Browser audio: pull /audio (host-stamped PCM chunks) and play it with the
// Web Audio API. Chunks are queued back-to-back with a small lead so network
// jitter doesn't cause gaps. viewer.py does the same with proper A/V sync.
const listenBtn = document.getElementById('listen');
const micBtn = document.getElementById('mic');
const st = document.getElementById('st');
let ctx = null, listening = false, nextTime = 0, abort = null;
let rate = 16000, channels = 1;
const LEAD = 0.25;                       // seconds of lead / jitter buffer
const u = p => new URL(p, location.origin).href;   // origin only, never creds

async function status() {
  const r = await fetch(u('/status')); const s = await r.json();
  rate = s.sample_rate; channels = s.channels;
  micBtn.textContent = '\\u{1F3A4} Mic: ' + (s.mic ? 'ON' : 'OFF');
  micBtn.className = s.mic ? 'on' : '';
  return s;
}
async function toggleMic() {
  const s = await status();
  await fetch(u(s.mic ? '/mic/stop' : '/mic/start'), {method: 'POST'});
  await status();
}
function playChunk(pcm) {
  const n = pcm.length / channels;
  const buf = ctx.createBuffer(channels, n, rate);
  for (let c = 0; c < channels; c++) {
    const ch = buf.getChannelData(c);
    for (let i = 0; i < n; i++) ch[i] = pcm[i * channels + c] / 32768;
  }
  const src = ctx.createBufferSource(); src.buffer = buf; src.connect(ctx.destination);
  const now = ctx.currentTime;
  if (nextTime < now + 0.02) nextTime = now + LEAD;   // (re)start with lead
  src.start(nextTime); nextTime += buf.duration;
}
async function listen() {
  if (listening) { abort.abort(); listening = false; listenBtn.textContent = '\\u{1F508} Listen'; listenBtn.className=''; return; }
  const s = await status();
  if (!s.mic) { await fetch(u('/mic/start'), {method:'POST'}); await status(); }
  ctx = ctx || new (window.AudioContext || window.webkitAudioContext)({sampleRate: rate});
  await ctx.resume();
  listening = true; listenBtn.textContent = '\\u{1F507} Stop listening'; listenBtn.className='on';
  abort = new AbortController();
  let chunks = 0, dropped = 0, lastSeq = null;
  try {
    const resp = await fetch(u('/audio'), {signal: abort.signal});
    const reader = resp.body.getReader();
    let pending = new Uint8Array(0);
    while (listening) {
      const {value, done} = await reader.read(); if (done) break;
      const merged = new Uint8Array(pending.length + value.length);
      merged.set(pending); merged.set(value, pending.length); pending = merged;
      while (pending.length >= 14) {
        const dv = new DataView(pending.buffer, pending.byteOffset, 14);
        const seq = dv.getUint32(8), len = dv.getUint16(12);
        if (pending.length < 14 + len) break;
        if (len > 0) {
          if (lastSeq !== null && seq > lastSeq + 1) dropped += seq - lastSeq - 1;
          lastSeq = seq; chunks++;
          const bytes = pending.slice(14, 14 + len);
          playChunk(new Int16Array(bytes.buffer, bytes.byteOffset, len / 2));
        }
        pending = pending.slice(14 + len);
      }
      if (chunks % 25 === 0) st.textContent = 'audio chunks ' + chunks + ' | dropped ' + dropped + ' | ' + rate + ' Hz';
    }
  } catch (e) { if (e.name !== 'AbortError') st.textContent = 'audio error: ' + e; }
  listening = false; listenBtn.textContent = '\\u{1F508} Listen'; listenBtn.className='';
}
listenBtn.onclick = listen; micBtn.onclick = toggleMic;
status(); setInterval(status, 5000);
</script>
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
    return jsonify(mic=mic_stream is not None, changed=changed,
                   sample_rate=SAMPLE_RATE, channels=CHANNELS)


@app.route("/mic/stop", methods=["GET", "POST"])
def mic_stop():
    changed = stop_mic()
    return jsonify(mic=mic_stream is not None, changed=changed)


@app.route("/status")
def status():
    return jsonify(active=camera is not None, mic=mic_stream is not None,
                   sample_rate=SAMPLE_RATE, channels=CHANNELS)


@app.route("/logs")
def logs():
    return jsonify(lines=list(LOG_BUFFER))


# ---- Tunnel + mailbox (unchanged from Room Cam Web v2.0) -------------------
def open_public_tunnel(port):
    """Open a Cloudflare quick tunnel and return the public https URL, or None.
    Quick tunnels need NO account and NO token."""
    # Catch Exception, not just ImportError: a PyInstaller build that misses
    # pycloudflared's bundled data files fails here with FileNotFoundError,
    # which used to kill this thread outright and leave the host running with
    # no tunnel and no explanation.
    try:
        from pycloudflared import try_cloudflare
    except Exception as exc:  # noqa: BLE001
        log(f"pycloudflared unavailable: {exc}")
        return None
    try:
        return try_cloudflare(port=port).tunnel
    except Exception as exc:  # noqa: BLE001
        log(f"Could not open Cloudflare tunnel: {exc}")
        return None


def publish_url_to_mailbox(url):
    if not PUBLISH_TO_MAILBOX or not NTFY_TOPIC:
        return False
    req = urllib.request.Request(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=url.encode(),
        method="POST",
        headers={"Title": "roomcam-url", "User-Agent": "room-cam-web-audio"},
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
    try:
        public_url = open_public_tunnel(PORT)
    except Exception:  # noqa: BLE001 - a dead thread here must not be silent
        public_url = None
        log(f"Tunnel thread failed:\n{traceback.format_exc()}")
    if not public_url:
        log("No public tunnel -> serving on the local network only.")
        report_fatal(
            "The internet tunnel could not be opened, so there is no public "
            "address for this camera.\n\nThe camera is still reachable on your "
            f"local network at port {PORT}.",
            "\n".join(LOG_BUFFER),
            title="Room Cam Web: no public address",
        )
        return
    log(f"PUBLIC url: {public_url}  (log in {USERNAME} / {PASSWORD})")
    while True:
        publish_url_to_mailbox(public_url)
        time.sleep(REPUBLISH_SECONDS)


# ---- Config (promptable; never edit code) ----------------------------------
CONFIG_FILENAME = "roomcam_config.ini"
CONFIG_SECTION = "roomcam"


def _config_path():
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, CONFIG_FILENAME)


def _has_console():
    return sys.stdin is not None and sys.stdin.isatty()


def report_fatal(summary, detail="", title="Room Cam Web could not start"):
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


def _ask(prompt_text, default):
    """Console prompt when there is a console, a pop-up for the no-console
    exe, else the default."""
    if _has_console():
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
    """env var -> .ini -> first-run prompt -> default; then write the .ini."""
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
        "password": DEFAULT_PASSWORD,
        "topic": DEFAULT_TOPIC,
        "port": str(DEFAULT_PORT),
        "camera_index": str(DEFAULT_CAMERA_INDEX),
        "mic_device": DEFAULT_MIC_DEVICE,
        "audio_rate": str(DEFAULT_AUDIO_RATE),
        "tunnel": DEFAULT_TUNNEL,
        "video_fps": str(DEFAULT_VIDEO_FPS),
        "jpeg_quality": str(DEFAULT_JPEG_QUALITY),
        "video_width": str(DEFAULT_VIDEO_WIDTH),
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

    _cfg = load_config()
    USERNAME = _cfg["username"]
    PASSWORD = _cfg["password"]
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

    log("Room Cam Web with Audio starting. Camera + mic OFF until a viewer connects.")
    log(f"Config file: {_config_path()}")
    log("Change settings there or via ROOMCAM_* env vars -- no code edits.")
    if PASSWORD == DEFAULT_PASSWORD:
        log("WARNING: password is still the public demo value -- set a real one "
            "in the config file before relying on internet access.")

    if ENABLE_TUNNEL:
        threading.Thread(target=_startup_tunnel_and_publish, daemon=True).start()
    else:
        log("Tunnel disabled in config -> local network only.")

    try:
        app.run(host="0.0.0.0", port=PORT, threaded=True)
    except OSError as exc:
        # WSAEADDRINUSE on Windows, EADDRINUSE on Linux.
        if getattr(exc, "winerror", None) == 10048 or getattr(exc, "errno", None) in (48, 98):
            raise SystemExit(
                f"Port {PORT} is already in use, so the server could not start.\n\n"
                "Another copy of Room Cam is probably already running -- check "
                "Task Manager for webcam_server.exe and end it, or set a "
                f"different 'port' in {_config_path()}."
            )
        raise


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
            "Room Cam Web hit an unexpected error and stopped.",
            traceback.format_exc(),
        )
        sys.exit(1)
