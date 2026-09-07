"""
Room Cam Web with Audio — VIEWER (runs on your laptop, any network).

Tokenless: it reads the public ntfy.sh mailbox to find the host's current
public URL (same as Room Cam Web v2.0), then shows the live video and plays the
host's microphone in sync, with a key to switch the mic on and off.

    pip install opencv-python sounddevice numpy
    python viewer.py

Keys (with the video window focused):
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
    --device N         speaker device index (see:  python -m sounddevice)
"""

import argparse
import base64
import configparser
import getpass
import json
import os
import random
import socket
import struct
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


def audio_reader(host, clock, wav, stop_event):
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


# ---- Main --------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Room Cam Web with Audio viewer.")
    ap.add_argument("--browser", action="store_true", help="open in browser only")
    ap.add_argument("--url", help="host URL (skips the ntfy mailbox)")
    ap.add_argument("--seconds", type=float, default=0, help="auto-quit after N s")
    ap.add_argument("--record", metavar="FILE.wav", help="save received audio")
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
        target=audio_reader, args=(host, clock, wav, stop_event), daemon=True
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

    print("Live. Keys:  m = mic on/off  |  c = next camera  |  n = next mic")
    print("             q = quit + all off  |  l = quit, leave on")
    shut_down = False
    started = time.monotonic()
    last_report = started
    window = "Room Cam Web with Audio"
    lag_tracker = VideoLagTracker(clock)
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
                    cv2.imshow(window, frame)

            key = cv2.waitKey(5) & 0xFF
            if key == ord("q"):
                shut_down = True
                break
            if key == ord("l"):
                break
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
