"""
Room Cam Web with Audio — VIEWER (runs on your laptop, any network).

Tokenless: it reads the public ntfy.sh mailbox to find the host's current
public URL (same as Room Cam Web v2.0), then shows the live video and plays the
host's microphone in sync, with a key to switch the mic on and off.

    pip install opencv-python sounddevice numpy imageio-ffmpeg
    python viewer.py

Recording: click the on-screen "REC" button (top-left of the video) or press
r to start/stop. Each recording is one audio+video file dropped next to the
viewer (roomcam_YYYYMMDD_HHMMSS.<format>). imageio-ffmpeg supplies the ffmpeg
used to merge the two streams; without it the audio and video are saved as two
separate files instead.

Keys (with the video window focused):
    r  = start/stop RECORDING (same as the on-screen REC button)
    m  = toggle the host MIC on/off
    q  = quit AND turn the host camera + mic OFF
    l  = quit but LEAVE the host camera + mic running

Nothing to edit in code. The topic, username and password come from (first
match wins): ROOMCAM_* env vars -> roomcam_config.ini beside this file (the
same file the host writes) -> a prompt when you start it.

Options (optional):
    --browser          just open the page in your browser (old v2.0 behaviour)
    --url URL          skip the mailbox and connect to this URL
    --seconds N        quit automatically after N seconds
    --record out.wav   also save received audio to a WAV file
    --format FMT       recording container: mp4 (default), mkv, or avi
    --device N         speaker device index (see:  python -m sounddevice)
"""

import argparse
import base64
import configparser
import datetime
import getpass
import json
import os
import random
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import wave
import webbrowser
from collections import deque

import cv2
import numpy as np
import sounddevice as sd

DEFAULT_TOPIC = "roomcam-audio-relay-7hq2v9nk3d"
DEFAULT_USERNAME = "admin"
CONFIG_FILENAME = "roomcam_config.ini"
CONFIG_SECTION = "roomcam"


# ---- DNS fallback ----------------------------------------------------------
# Some home routers / ISP filters refuse to resolve brand-new *.trycloudflare.com
# names (they answer "non-existent domain" even though the tunnel is live). If
# the normal resolver fails, ask Cloudflare's resolver directly over HTTPS by
# IP address, so the viewer works on those networks with no settings changed.
_real_getaddrinfo = socket.getaddrinfo
_dns_cache = {}
PUBLIC_RESOLVERS = ("1.1.1.1", "8.8.8.8", "9.9.9.9")


def _skip_dns_name(data, i):
    """Step over a name in a DNS message, following compression pointers."""
    while i < len(data):
        length = data[i]
        if length == 0:
            return i + 1
        if length & 0xC0 == 0xC0:       # pointer: two bytes, and it ends here
            return i + 2
        i += length + 1
    return i


def _dns_query(hostname, server, timeout=4):
    """Ask a public resolver directly over UDP and return its A records.

    Plain UDP on purpose. DNS-over-HTTPS to a bare resolver IP fails
    certificate validation on this setup, and resolving the DoH server by name
    would need the very lookup we are trying to replace.
    """
    labels = b"".join(
        bytes([len(p)]) + p.encode() for p in hostname.split(".") if p
    ) + b"\x00"
    query_id = random.randint(0, 0xFFFF)
    packet = (struct.pack("!HHHHHH", query_id, 0x0100, 1, 0, 0, 0)
              + labels + struct.pack("!HH", 1, 1))     # QTYPE=A, QCLASS=IN

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(packet, (server, 53))
        data, _ = sock.recvfrom(4096)
    except OSError:
        return []
    finally:
        sock.close()

    if len(data) < 12 or struct.unpack("!H", data[:2])[0] != query_id:
        return []
    answer_count = struct.unpack("!H", data[6:8])[0]
    i = _skip_dns_name(data, 12) + 4                   # question name + type/class
    ips = []
    for _ in range(answer_count):
        i = _skip_dns_name(data, i)
        if i + 10 > len(data):
            break
        rtype, _rclass, _ttl, rdlength = struct.unpack("!HHIH", data[i:i + 10])
        i += 10
        if rtype == 1 and rdlength == 4:               # an A record
            ips.append(".".join(str(b) for b in data[i:i + 4]))
        i += rdlength
    return ips


def _public_dns_lookup(hostname):
    for resolver in PUBLIC_RESOLVERS:
        ips = _dns_query(hostname, resolver)
        if ips:
            return ips
    return []


def _getaddrinfo_with_fallback(host, port, family=0, type=0, proto=0, flags=0):
    try:
        return _real_getaddrinfo(host, port, family, type, proto, flags)
    except socket.gaierror:
        if host not in _dns_cache:
            ips = _public_dns_lookup(host)
            if not ips:
                print(f"[viewer] {host} could not be resolved, by your network's "
                      "DNS or by a public one.")
                raise
            _dns_cache[host] = ips
            print(f"[viewer] your network's DNS refused to resolve {host}; "
                  f"using a public resolver instead ({ips[0]}).")
        socktype = type or socket.SOCK_STREAM
        return [(socket.AF_INET, socktype, 0, "", (ip, port)) for ip in _dns_cache[host]]


socket.getaddrinfo = _getaddrinfo_with_fallback
# ---------------------------------------------------------------------------

AUDIO_HEADER = struct.Struct("!dIH")
DTYPE = "int16"

# ---- Viewer behaviour ------------------------------------------------------
MIC_ON_AT_CONNECT = True
PREBUFFER_SECONDS = 1.0         # audio held before playback starts. Cloudflare
                                # quick tunnels deliver audio in bursts and can
                                # stall for a second or more, so this is much
                                # bigger than the LAN version's 0.06. It grows
                                # by itself if the link turns out burstier.
MAX_BUFFER_SECONDS = 6.0        # hard cap on audio held
LATENCY_SLACK = 0.75            # if the buffer holds this much MORE than its
                                # target (a stall just caught up in a burst),
                                # skip ahead so the delay doesn't stick around
SYNC_TOLERANCE = 0.03
AUDIO_STALE_AFTER = 2.0
# ---------------------------------------------------------------------------


# ---- Config (promptable; never edit code) ----------------------------------
def _config_file():
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, CONFIG_FILENAME)


def load_settings():
    """topic / username / password: env var -> .ini -> prompt -> default."""
    # interpolation=None so a password containing "%" reads back intact.
    cfg = configparser.ConfigParser(interpolation=None)
    path = _config_file()
    if os.path.exists(path):
        cfg.read(path)

    def get(key, default):
        env = os.environ.get("ROOMCAM_" + key.upper())
        if env:
            return env
        if cfg.has_option(CONFIG_SECTION, key) and cfg.get(CONFIG_SECTION, key):
            return cfg.get(CONFIG_SECTION, key)
        return default

    topic = get("topic", DEFAULT_TOPIC)
    username = get("username", DEFAULT_USERNAME)
    password = get("password", "")
    if not password:
        try:
            password = getpass.getpass(
                f"Host password for user '{username}' (set on the host): "
            )
        except (EOFError, KeyboardInterrupt):
            password = ""
    return topic, username, password


# ---- Mailbox (unchanged from Room Cam Web v2.0) ----------------------------
def fetch_url_from_mailbox(topic):
    req = urllib.request.Request(
        f"https://ntfy.sh/{topic}/json?poll=1&since=12h",
        headers={"User-Agent": "room-cam-web-audio"},
    )
    latest = None
    latest_time = -1
    with urllib.request.urlopen(req, timeout=15) as resp:
        for line in resp.read().decode().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if obj.get("event") != "message":
                continue
            msg = (obj.get("message") or "").strip()
            when = obj.get("time", 0)
            if msg.startswith("http") and when >= latest_time:
                latest_time = when
                latest = msg
    return latest


# ---- Host control ----------------------------------------------------------
class Host:
    def __init__(self, base, username, password):
        self.base = base.rstrip("/")
        self.auth = "Basic " + base64.b64encode(
            f"{username}:{password}".encode()
        ).decode()

    def api(self, path, quiet=False):
        """Call a control endpoint. Returns the JSON, or None (see last_error:
        'auth' = wrong password, 'net' = unreachable / timed out)."""
        req = urllib.request.Request(
            f"{self.base}{path}", headers={"Authorization": self.auth}
        )
        self.last_error = None
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                self.last_error = "auth"
                print("[viewer] wrong username/password for the host.")
            else:
                self.last_error = "net"
                if not quiet:
                    print(f"[viewer] control call {path} failed: {exc}")
            return None
        except Exception as exc:  # noqa: BLE001
            self.last_error = "net"
            if not quiet:
                print(f"[viewer] control call {path} failed: {exc}")
            return None

    def wait_until_reachable(self, attempts=12, delay=5.0):
        """A fresh Cloudflare quick tunnel can take a little while after its
        URL is published before it actually routes traffic. Keep knocking."""
        for attempt in range(1, attempts + 1):
            status = self.api("/status", quiet=True)
            if status is not None:
                return status
            if self.last_error == "auth":
                return None
            print(f"Host not reachable yet (tunnel warming up?) -- retry {attempt}/{attempts}...")
            time.sleep(delay)
        return None

    def open_stream(self, path):
        req = urllib.request.Request(
            f"{self.base}{path}", headers={"Authorization": self.auth}
        )
        return urllib.request.urlopen(req, timeout=20)


def stream_host_logs(host, stop_event):
    shown = 0
    while not stop_event.is_set():
        data = host.api("/logs")
        if data and "lines" in data:
            for line in data["lines"][shown:]:
                print(f"[host] {line}")
            shown = len(data["lines"])
        time.sleep(2.0)


# ---- Audio: buffer + clock -------------------------------------------------
class AudioClock:
    """FIFO of PCM bytes between the network thread and the speaker callback
    that also knows what HOST time is coming out of the speaker right now.
    That is the reference the video loop waits on."""

    def __init__(self, sample_rate, channels):
        self.rate = sample_rate
        self.channels = channels
        self._buf = bytearray()
        self._lock = threading.Lock()
        self.primed = False
        self.underruns = 0
        self.overflows = 0
        self.skips = 0                       # times we jumped ahead to cut delay
        self.chunks = 0
        self.dropped = 0
        self.prebuffer = PREBUFFER_SECONDS   # grows on underruns (bursty links)
        self.max_gap = 0.0                   # longest pause between chunks, recent
        self._last_seq = None
        self._last_ts = None
        self._last_frames = 0
        self._last_seen = 0.0

    def _bytes_per_sec(self):
        return self.rate * 2 * self.channels

    def push(self, ts, seq, pcm):
        with self._lock:
            now = time.monotonic()
            if self._last_seen and self._last_seq is not None:
                self.max_gap = max(self.max_gap * 0.98, now - self._last_seen)
            if self._last_seq is not None and seq > self._last_seq + 1:
                self.dropped += seq - self._last_seq - 1
            self._last_seq = seq
            self._last_ts = ts
            self._last_frames = len(pcm) // (2 * self.channels)
            self._last_seen = now
            self.chunks += 1
            self._buf += pcm
            bps = self._bytes_per_sec()
            max_bytes = int(MAX_BUFFER_SECONDS * bps)
            if len(self._buf) > max_bytes:
                del self._buf[: len(self._buf) - max_bytes]
                self.overflows += 1
            # A stall that caught up in one burst leaves extra audio queued,
            # which would play as permanent delay. Jump ahead to the target.
            soft_bytes = int((self.prebuffer + LATENCY_SLACK) * bps)
            if self.primed and len(self._buf) > soft_bytes:
                keep = int(self.prebuffer * bps)
                del self._buf[: len(self._buf) - keep]
                self.skips += 1
            if not self.primed and len(self._buf) >= int(self.prebuffer * bps):
                self.primed = True

    def _grow_prebuffer(self):
        """Called on underrun: the link is burstier than we assumed, so hold
        more audio before playing (trading a little lag for no stutter)."""
        target = min(MAX_BUFFER_SECONDS * 0.6, max(self.prebuffer * 1.5, self.max_gap + 0.2))
        if target > self.prebuffer + 0.01:
            self.prebuffer = target
            print(f"[viewer] audio arriving in bursts (gap {self.max_gap*1000:.0f}ms); "
                  f"buffer raised to {self.prebuffer*1000:.0f}ms")

    def reset(self):
        with self._lock:
            self._last_seq = None

    def set_rate(self, rate):
        """A different microphone may run at a different sample rate. The
        reader thread keeps this same object, so change it in place."""
        with self._lock:
            self.rate = rate
            self._buf.clear()
            self.primed = False
            self._last_seq = None

    def relax(self):
        """Give latency back once the link settles. The buffer only ever grew
        before, so a rough first half-minute meant seconds of delay for the
        rest of the session."""
        floor = max(PREBUFFER_SECONDS, self.max_gap * 2 + 0.2)
        if self.prebuffer > floor + 0.05:
            self.prebuffer = max(floor, self.prebuffer * 0.8)
            return True
        return False

    def pull(self, nbytes):
        with self._lock:
            if not self.primed:
                return bytes(nbytes)
            chunk = bytes(self._buf[:nbytes])
            del self._buf[:nbytes]
        if len(chunk) < nbytes:
            self.underruns += 1
            self.primed = False
            self._grow_prebuffer()
            chunk += bytes(nbytes - len(chunk))
        return chunk

    def buffered_seconds(self):
        with self._lock:
            return len(self._buf) / self._bytes_per_sec()

    def active(self):
        stale_after = max(AUDIO_STALE_AFTER, 2 * self.prebuffer)
        return (time.monotonic() - self._last_seen) < stale_after

    def playhead(self):
        with self._lock:
            if self._last_ts is None or not self.primed:
                return None
            newest_end = self._last_ts + self._last_frames / self.rate
            buffered = len(self._buf) / self._bytes_per_sec()
        return newest_end - buffered


def read_exact(resp, n):
    chunks = []
    remaining = n
    while remaining > 0:
        piece = resp.read(remaining)
        if not piece:
            return None
        chunks.append(piece)
        remaining -= len(piece)
    return b"".join(chunks)


def audio_reader(host, clock, wav, recorder, stop_event):
    while not stop_event.is_set():
        try:
            resp = host.open_stream("/audio")
        except Exception as exc:  # noqa: BLE001
            print(f"[viewer] audio connect failed: {exc}")
            time.sleep(2.0)
            continue
        with resp:
            while not stop_event.is_set():
                hdr = read_exact(resp, AUDIO_HEADER.size)
                if hdr is None:
                    break
                ts, seq, nbytes = AUDIO_HEADER.unpack(hdr)
                if nbytes == 0:
                    clock.reset()
                    continue
                pcm = read_exact(resp, nbytes)
                if pcm is None:
                    break
                clock.push(ts, seq, pcm)
                if wav:
                    wav.writeframes(pcm)
                if recorder:
                    recorder.write_audio(pcm)
        if not stop_event.is_set():
            print("[viewer] audio stream ended; reconnecting...")
            time.sleep(1.0)


# ---- Video: MJPEG parser ---------------------------------------------------
def video_reader(host, frames, frames_lock, stop_event, counters):
    """Parse the MJPEG stream by hand (to get each frame's timestamp) and
    reconnect if the tunnel drops it. Gives up only after repeated failures."""
    failures = 0
    while not stop_event.is_set():
        try:
            resp = host.open_stream("/video")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"[viewer] video connect failed ({failures}/5): {exc}")
            if failures >= 5:
                counters["video_error"] = True
                return
            time.sleep(3.0)
            continue
        got_any = False
        try:
            with resp:
                while not stop_event.is_set():
                    line = resp.readline()
                    if not line:
                        break
                    if not line.startswith(b"--frame"):
                        continue
                    headers = {}
                    while True:
                        line = resp.readline()
                        if line in (b"\r\n", b"\n", b""):
                            break
                        key, _, value = line.decode(errors="ignore").partition(":")
                        headers[key.strip().lower()] = value.strip()
                    length = int(headers.get("content-length", "0") or 0)
                    if length <= 0:
                        continue
                    data = read_exact(resp, length)
                    if data is None:
                        break
                    try:
                        ts = float(headers.get("x-timestamp", "nan"))
                    except ValueError:
                        ts = float("nan")
                    # Keep the JPEG bytes, not a decoded frame: frames may wait
                    # several seconds for the audio, and 400 JPEGs is ~8 MB
                    # where 400 decoded frames would be ~370 MB.
                    with frames_lock:
                        frames.append((ts, data))
                    counters["frames_received"] += 1
                    got_any = True
                    failures = 0
        except Exception as exc:  # noqa: BLE001
            print(f"[viewer] video stream error: {exc}")
        if stop_event.is_set():
            break
        if not got_any:
            failures += 1
            if failures >= 5:
                counters["video_ended"] = True
                return
        print("[viewer] video stream ended; reconnecting...")
        time.sleep(2.0)


def pick_frame(frames, frames_lock, clock, counters):
    """Choose the frame to show now. Returns (frame, lag) where lag is how far
    the frame's host time is BEHIND the audio playhead (None if unsynced)."""
    playhead = clock.playhead() if clock.active() else None
    with frames_lock:
        if not frames:
            return None, None
        if playhead is None:
            chosen = frames[-1]
            frames.clear()
            counters["shown_unsynced"] += 1
            return chosen[1], None
        chosen = None
        while frames and (frames[0][0] != frames[0][0] or frames[0][0] <= playhead + SYNC_TOLERANCE):
            chosen = frames.popleft()
        if chosen is None and frames and frames[0][0] - playhead > MAX_BUFFER_SECONDS + 2.0:
            # Frames further ahead than the audio could ever be delayed: the
            # clocks disagree (host restarted?). Don't freeze, just show it.
            chosen = frames.popleft()
        if chosen is None:
            return None, None
        if chosen[0] != chosen[0]:      # NaN timestamp
            return chosen[1], None
        lag = playhead - chosen[0]
        counters["shown_synced"] += 1
        counters["sync_error_sum"] += abs(lag)
        return chosen[1], lag


class VideoLagTracker:
    """Over the internet the video can arrive later than the audio (it is the
    heavier stream). Audio is the clock, so the only way to line them up is to
    hold the audio longer. Watch the typical lag and, every few seconds, delay
    the audio by that much (never more than the buffer can hold)."""

    def __init__(self, clock):
        self.clock = clock
        self.lags = deque(maxlen=90)
        self.last_adjust = time.monotonic()

    def observe(self, lag):
        if lag is not None:
            self.lags.append(lag)
        now = time.monotonic()
        if now - self.last_adjust < 5.0 or len(self.lags) < 30:
            return
        self.last_adjust = now
        typical = sorted(self.lags)[len(self.lags) // 2]
        self.lags.clear()
        if typical <= 0.15:
            return
        cap = MAX_BUFFER_SECONDS * 0.9
        target = min(cap, self.clock.prebuffer + typical)
        if target <= self.clock.prebuffer + 0.05:
            return
        self.clock.prebuffer = target
        self.clock.primed = False           # pause playback to hold more audio
        print(f"[viewer] video is {typical*1000:.0f}ms behind the audio; "
              f"delaying audio to match (buffer {target*1000:.0f}ms)")


# ---- Recording (audio + video -> one file) ---------------------------------
def _base_dir():
    """Where a recording lands: next to viewer.exe when frozen, else next to
    this script (the same folder as roomcam_config.ini)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def find_ffmpeg():
    """Locate an ffmpeg binary to merge audio + video into one file. Order:
    env var -> the copy imageio-ffmpeg bundles -> an ffmpeg next to the viewer
    -> one on PATH. Returns None if nothing is found (then the recorder leaves
    the audio and video as two separate files)."""
    for env in ("IMAGEIO_FFMPEG_EXE", "FFMPEG_BINARY", "FFMPEG"):
        p = os.environ.get(env)
        if p and os.path.exists(p):
            return p
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.exists(exe):
            return exe
    except Exception:  # noqa: BLE001
        pass
    local = os.path.join(_base_dir(), "ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    if os.path.exists(local):
        return local
    return shutil.which("ffmpeg")


# Container -> (video codec, audio codec) for the final file. The temp video is
# always MJPEG in an AVI at a placeholder rate; ffmpeg re-times it on the way
# out, so these codecs are the only per-format choice.
RECORD_FORMATS = {
    "mp4": ("libx264", "aac"),
    "mkv": ("libx264", "aac"),
    "avi": ("mjpeg", "pcm_s16le"),
}
DEFAULT_RECORD_FORMAT = "mp4"
NOMINAL_FPS = 20.0          # placeholder rate for the temp AVI; ffmpeg re-times

REC_BTN = (12, 12, 118, 46)     # x1, y1, x2, y2 of the on-screen Record button


def _in_button(x, y, rect=REC_BTN):
    return rect[0] <= x <= rect[2] and rect[1] <= y <= rect[3]


def draw_rec_button(img, recording, elapsed):
    """Draw the REC button (and, while recording, a blinking dot + timer) onto
    a copy of the frame that is shown but never saved."""
    x1, y1, x2, y2 = REC_BTN
    panel = img.copy()
    cv2.rectangle(panel, (x1, y1), (x2, y2), (28, 28, 34), -1)
    cv2.addWeighted(panel, 0.55, img, 0.45, 0, img)
    cy = (y1 + y2) // 2
    if recording:
        blink = int(time.monotonic() * 2) % 2 == 0
        cv2.circle(img, (x1 + 15, cy), 7, (0, 0, 235) if blink else (0, 0, 90), -1)
        cv2.putText(img, "REC", (x1 + 30, y2 - 12),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (235, 235, 235), 1, cv2.LINE_AA)
        mm, ss = divmod(int(elapsed), 60)
        cv2.putText(img, f"{mm:02d}:{ss:02d}", (x2 + 10, y2 - 12),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (60, 60, 235), 1, cv2.LINE_AA)
        border = (60, 60, 235)
    else:
        cv2.circle(img, (x1 + 15, cy), 7, (60, 60, 235), -1)
        cv2.putText(img, "REC", (x1 + 30, y2 - 12),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (200, 200, 200), 1, cv2.LINE_AA)
        border = (95, 95, 95)
    cv2.rectangle(img, (x1, y1), (x2, y2), border, 1)


class Recorder:
    """Toggle-on/off recording of the live view to ONE audio+video file in the
    output folder. Decoded video frames go to a temp MJPEG AVI, received PCM to
    a temp WAV; on stop the two are muxed with ffmpeg and the video is re-timed
    so its length matches the audio (audio is the master clock, same as
    playback). If ffmpeg can't be found, the two temp files are kept instead."""

    def __init__(self, out_dir, fmt):
        self.out_dir = out_dir
        self.fmt = fmt if fmt in RECORD_FORMATS else DEFAULT_RECORD_FORMAT
        self.ffmpeg = find_ffmpeg()
        self._lock = threading.Lock()
        self.recording = False
        self._reset()

    def _reset(self):
        self.vw = None
        self.wav = None
        self.size = None            # (w, h) the video writer was opened with
        self.channels = 1
        self.rate = 16000
        self.video_frames = 0
        self.audio_frames = 0       # sample frames -> exact audio duration
        self.t0 = 0.0
        self._vpath = self._apath = self._base = None

    def start(self, frame, rate, channels):
        """main thread: open the temp writers sized to the current frame."""
        with self._lock:
            if self.recording:
                return
            h, w = frame.shape[:2]
            stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            self._base = f"roomcam_{stamp}"
            self._vpath = os.path.join(self.out_dir, self._base + ".video.avi")
            self._apath = os.path.join(self.out_dir, self._base + ".audio.wav")
            vw = cv2.VideoWriter(
                self._vpath, cv2.VideoWriter_fourcc(*"MJPG"), NOMINAL_FPS, (w, h)
            )
            if not vw.isOpened():
                print("[viewer] could not open a video writer; recording aborted.")
                self._reset()
                return
            wav = wave.open(self._apath, "wb")
            wav.setnchannels(channels)
            wav.setsampwidth(2)
            wav.setframerate(rate)
            self.vw, self.wav = vw, wav
            self.size = (w, h)
            self.rate, self.channels = rate, channels
            self.video_frames = self.audio_frames = 0
            self.t0 = time.monotonic()
            self.recording = True
            print(f"[viewer] RECORDING -> {self._base}.{self.fmt}")

    def write_video(self, frame):
        """main thread: called once per newly shown frame."""
        if not self.recording or self.vw is None:
            return
        if (frame.shape[1], frame.shape[0]) != self.size:
            frame = cv2.resize(frame, self.size)
        self.vw.write(frame)
        self.video_frames += 1

    def write_audio(self, pcm):
        """audio thread: called for every received PCM chunk."""
        with self._lock:
            if not self.recording or self.wav is None:
                return
            self.wav.writeframes(pcm)
            self.audio_frames += len(pcm) // (2 * self.channels)

    def elapsed(self):
        return time.monotonic() - self.t0 if self.recording else 0.0

    def toggle(self, frame, rate, channels):
        if self.recording:
            return self.stop()
        if frame is None:
            print("[viewer] no video yet -- can't start recording.")
            return None
        self.start(frame, rate, channels)
        return None

    def stop(self):
        """Close the writers, then mux. Returns the saved path (or the kept
        temp video path if the merge could not run)."""
        with self._lock:
            if not self.recording:
                return None
            self.recording = False
            vw, wav = self.vw, self.wav
            vpath, apath, base = self._vpath, self._apath, self._base
            vframes, aframes = self.video_frames, self.audio_frames
            rate = self.rate
            elapsed = max(time.monotonic() - self.t0, 0.001)
            fmt = self.fmt
            self.vw = self.wav = None
        if vw is not None:
            vw.release()
        if wav is not None:
            wav.close()
        if vframes == 0:
            print("[viewer] recording had no video frames; nothing saved.")
            _quiet_remove(vpath, apath)
            return None
        out = os.path.join(self.out_dir, f"{base}.{fmt}")
        if self._mux(vpath, apath, out, vframes, aframes, rate, elapsed, fmt):
            _quiet_remove(vpath, apath)
            print(f"[viewer] saved {out}")
            return out
        print("[viewer] could not merge audio+video; kept them separately:")
        print(f"          {vpath}")
        print(f"          {apath}")
        return vpath

    def _mux(self, vpath, apath, out, vframes, aframes, rate, elapsed, fmt):
        if not self.ffmpeg:
            print("[viewer] ffmpeg not found (pip install imageio-ffmpeg) --"
                  " can't merge.")
            return False
        have_audio = aframes > 0
        # The video should last as long as the audio it lines up with; with no
        # audio, fall back to real wall-clock fps.
        if have_audio:
            audio_secs = aframes / float(rate)
            fps = vframes / audio_secs if audio_secs > 0 else NOMINAL_FPS
        else:
            fps = vframes / elapsed
        fps = min(max(fps, 1.0), 120.0)
        vcodec, acodec = RECORD_FORMATS.get(fmt, RECORD_FORMATS[DEFAULT_RECORD_FORMAT])
        cmd = [self.ffmpeg, "-y", "-loglevel", "error",
               "-r", f"{fps:.4f}", "-i", vpath]
        if have_audio:
            cmd += ["-i", apath]
        cmd += ["-c:v", vcodec]
        if vcodec == "libx264":
            cmd += ["-preset", "veryfast", "-pix_fmt", "yuv420p"]
        elif vcodec == "mjpeg":
            cmd += ["-q:v", "5"]
        if have_audio:
            cmd += ["-c:a", acodec]
            if acodec == "aac":
                cmd += ["-b:a", "128k"]
            cmd += ["-shortest"]
        cmd.append(out)
        flags = 0x08000000 if os.name == "nt" else 0    # CREATE_NO_WINDOW
        try:
            res = subprocess.run(cmd, capture_output=True, creationflags=flags)
        except Exception as exc:  # noqa: BLE001
            print(f"[viewer] ffmpeg failed to run: {exc}")
            return False
        if res.returncode != 0:
            tail = res.stderr.decode(errors="ignore").strip().splitlines()
            print(f"[viewer] ffmpeg error: {tail[-1] if tail else res.returncode}")
            return False
        return os.path.exists(out) and os.path.getsize(out) > 0


def _quiet_remove(*paths):
    for p in paths:
        try:
            if p:
                os.remove(p)
        except OSError:
            pass


# ---- Main --------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Room Cam Web with Audio viewer.")
    ap.add_argument("--browser", action="store_true", help="open in browser only")
    ap.add_argument("--url", help="host URL (skips the ntfy mailbox)")
    ap.add_argument("--seconds", type=float, default=0, help="auto-quit after N s")
    ap.add_argument("--record", metavar="FILE.wav", help="save received audio")
    ap.add_argument("--format", choices=sorted(RECORD_FORMATS), default=DEFAULT_RECORD_FORMAT,
                    help="recording container (default: mp4)")
    ap.add_argument("--device", type=int, default=None, help="speaker device index")
    args = ap.parse_args()

    topic, username, password = load_settings()

    if args.url:
        url = args.url
    else:
        print("Reading the rendezvous mailbox (ntfy)...")
        try:
            url = fetch_url_from_mailbox(topic)
        except Exception as exc:  # noqa: BLE001
            print(f"Couldn't read the mailbox: {exc}")
            return
        if not url:
            print("No URL in the mailbox yet.")
            print("Start webcam_server.py on the host, wait a few seconds, re-run this.")
            return
    print(f"Host is live at: {url}")

    if args.browser:
        print("Opening it in your browser. Use the Listen / Mic buttons on the page.")
        webbrowser.open(url)
        return

    host = Host(url, username, password)
    status = host.wait_until_reachable()
    if status is None:
        print("Found the host's URL but couldn't reach it. If the host just "
              "started, wait a moment and try again.")
        return
    if status.get("active"):
        print("Host camera is already ON.")
    else:
        print("Host camera is OFF -> turning it ON...")
        host.api("/start")
    mic_on = bool(status.get("mic"))
    if mic_on:
        print("Host mic is already ON.")
    elif MIC_ON_AT_CONNECT:
        print("Host mic is OFF -> turning it ON...")
        reply = host.api("/mic/start")
        mic_on = bool(reply and reply.get("mic"))
        if reply:
            status.update(reply)          # picks up the actual sample rate
    rate = int(status.get("sample_rate", 16000))
    channels = int(status.get("channels", 1))
    print(f"Audio: {rate} Hz, {channels} channel(s)")

    stop_event = threading.Event()
    threading.Thread(
        target=stream_host_logs, args=(host, stop_event), daemon=True
    ).start()

    clock = AudioClock(rate, channels)
    wav = None
    if args.record:
        wav = wave.open(args.record, "wb")
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(rate)

    recorder = Recorder(_base_dir(), args.format)
    if recorder.ffmpeg:
        print(f"Recording -> {recorder.out_dir}  (format: {recorder.fmt}). "
              "Press r or click REC.")
    else:
        print("Recording available, but ffmpeg was not found: audio and video "
              "will be saved as two files. (pip install imageio-ffmpeg)")

    def speaker_callback(outdata, frames_n, time_info, status_flags):
        nbytes = frames_n * 2 * channels
        outdata[:] = np.frombuffer(clock.pull(nbytes), dtype=DTYPE).reshape(frames_n, channels)

    def _open_speaker(rate_hz, chans, device):
        """Open the speaker at a given rate. Reopened if a different mic is
        picked, since a stream's sample rate is fixed once it starts."""
        try:
            stream = sd.OutputStream(
                samplerate=rate_hz, channels=chans, dtype=DTYPE,
                blocksize=int(rate_hz * 0.04), device=device,
                callback=speaker_callback,
            )
            stream.start()
            return stream
        except Exception as exc:  # noqa: BLE001
            print(f"[viewer] no speaker output ({exc}); video only.")
            return None

    speaker = _open_speaker(rate, channels, args.device)

    threading.Thread(
        target=audio_reader, args=(host, clock, wav, recorder, stop_event), daemon=True
    ).start()

    frames = deque(maxlen=400)
    frames_lock = threading.Lock()
    counters = {
        "frames_received": 0, "shown_synced": 0, "shown_unsynced": 0,
        "sync_error_sum": 0.0, "video_ended": False, "video_error": False,
    }
    threading.Thread(
        target=video_reader,
        args=(host, frames, frames_lock, stop_event, counters), daemon=True,
    ).start()

    device_info = host.api("/devices", quiet=True) or {}
    cameras = [c["index"] for c in device_info.get("cameras", [])]
    mics = [-1] + [m["index"] for m in device_info.get("microphones", [])]
    mic_names = {m["index"]: m["name"] for m in device_info.get("microphones", [])}
    mic_names[-1] = "default microphone"
    if cameras:
        print(f"Cameras on the host: {', '.join('Camera ' + str(c) for c in cameras)}")
    if len(mics) > 1:
        print(f"Microphones on the host: {len(mics) - 1} found")

    print("Live. Keys:  r = record on/off  |  m = mic on/off  |  c = next camera")
    print("             n = next mic  |  q = quit + all off  |  l = quit, leave on")
    shut_down = False
    started = time.monotonic()
    last_report = started
    window = "Room Cam Web with Audio"
    lag_tracker = VideoLagTracker(clock)

    ui = {"frame": None}            # newest clean (un-annotated) frame

    def handle_toggle():
        recorder.toggle(ui["frame"], clock.rate, clock.channels)

    def on_mouse(event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN and _in_button(x, y):
            handle_toggle()

    cv2.namedWindow(window)
    cv2.setMouseCallback(window, on_mouse)
    current_cam = device_info.get("current_camera", 0)
    cam_pos = cameras.index(current_cam) if current_cam in cameras else 0
    current_mic = device_info.get("current_mic", -1)
    mic_pos = mics.index(current_mic) if current_mic in mics else 0
    try:
        while True:
            if args.seconds and time.monotonic() - started >= args.seconds:
                break
            if counters["video_error"]:
                print("Couldn't open the video stream.")
                break
            if counters["video_ended"]:
                print("Stream ended.")
                break

            jpeg, lag = pick_frame(frames, frames_lock, clock, counters)
            lag_tracker.observe(lag)
            if jpeg is not None:
                frame = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
                if frame is not None:
                    ui["frame"] = frame
                    recorder.write_video(frame)   # save the clean frame
            # Redraw every loop so the REC button (and its blink/timer) stays
            # live and clickable even between incoming frames.
            if ui["frame"] is not None:
                display = ui["frame"].copy()
                draw_rec_button(display, recorder.recording, recorder.elapsed())
                cv2.imshow(window, display)

            key = cv2.waitKey(5) & 0xFF
            if key == ord("q"):
                shut_down = True
                break
            if key == ord("l"):
                break
            if key == ord("r"):
                handle_toggle()
            if key == ord("m"):
                if mic_on:
                    host.api("/mic/stop")
                    mic_on = False
                    print("[viewer] host mic OFF")
                else:
                    host.api("/mic/start")
                    mic_on = True
                    print("[viewer] host mic ON")
            if key == ord("c") and len(cameras) > 1:
                cam_pos = (cam_pos + 1) % len(cameras)
                reply = host.api(f"/camera/select?index={cameras[cam_pos]}")
                print(f"[viewer] host camera -> Camera {cameras[cam_pos]}"
                      + ("" if reply and reply.get("ok") else "  (it did not open)"))
            if key == ord("n") and len(mics) > 1:
                mic_pos = (mic_pos + 1) % len(mics)
                reply = host.api(f"/mic/select?index={mics[mic_pos]}")
                print(f"[viewer] host mic -> {mic_names.get(mics[mic_pos], mics[mic_pos])}")
                if reply and reply.get("sample_rate") and reply["sample_rate"] != clock.rate:
                    new_rate = int(reply["sample_rate"])
                    print(f"[viewer] that mic runs at {new_rate} Hz; reopening the speaker.")
                    clock.set_rate(new_rate)
                    if speaker is not None:
                        speaker.stop(); speaker.close()
                        speaker = _open_speaker(new_rate, channels, args.device)
                mic_on = bool(reply and reply.get("mic"))

            now = time.monotonic()
            if now - last_report >= 5.0:
                last_report = now
                clock.relax()
                synced = counters["shown_synced"]
                avg = (counters["sync_error_sum"] / synced * 1000) if synced else 0
                print(
                    f"[viewer] frames={counters['frames_received']} "
                    f"audio chunks={clock.chunks} dropped={clock.dropped} "
                    f"underruns={clock.underruns} skips={clock.skips} buffer={clock.buffered_seconds()*1000:.0f}ms "
                    f"(target {clock.prebuffer*1000:.0f}ms, max gap {clock.max_gap*1000:.0f}ms) "
                    f"synced={synced} avg A/V offset={avg:.0f}ms"
                )
    except KeyboardInterrupt:
        pass

    stop_event.set()
    if recorder.recording:
        print("[viewer] finishing the recording...")
        recorder.stop()
    if speaker:
        speaker.stop()
        speaker.close()
    if wav:
        wav.close()
    cv2.destroyAllWindows()
    if shut_down:
        print("Turning host camera + mic OFF...")
        host.api("/stop")
        host.api("/mic/stop")
    synced = counters["shown_synced"]
    avg = (counters["sync_error_sum"] / synced * 1000) if synced else 0
    print(
        f"Viewer closed. frames={counters['frames_received']} shown synced={synced} "
        f"unsynced={counters['shown_unsynced']} avg A/V offset={avg:.0f}ms | "
        f"audio chunks={clock.chunks} dropped={clock.dropped} underruns={clock.underruns}"
    )


if __name__ == "__main__":
    main()
