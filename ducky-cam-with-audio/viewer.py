"""
Ducky Cam with Audio — VIEWER (runs on any machine on the same network).

Same as Ducky Cam v2, plus sound. It finds the host by UDP broadcast (no IP to
enter), turns the host camera AND mic on, shows the live feed, plays the audio,
and mirrors the host's log lines here. It keeps retrying discovery until the
host appears.

Audio is the clock: every frame and audio chunk carries the host's timestamp,
and the viewer shows each frame when the audio from that same instant is
coming out of the speakers, so lips and sound line up.

    pip install opencv-python sounddevice numpy
    python viewer.py

Keys (with the video window focused):
    m  = toggle the host MIC on/off
    q  = quit AND turn the host camera + mic OFF
    l  = quit but LEAVE the host camera + mic running

Test options (optional):
    --seconds N        quit automatically after N seconds
    --record out.wav   also save received audio to a WAV file
    --device N         speaker device index (see:  python -m sounddevice)
"""

import argparse
import base64
import json
import select
import socket
import struct
import threading
import time
import urllib.error
import urllib.request
import wave
from collections import deque

import cv2
import numpy as np
import sounddevice as sd

# ---- Must match camera_server.py -------------------------------------------
USERNAME = "admin"
PASSWORD = "1337"
DISCOVERY_PORT = 50505
DISCOVERY_REQUEST = b"ROOMCAM_DISCOVERY_V1"
DISCOVERY_REPLY_PREFIX = "ROOMCAM_HERE"

SAMPLE_RATE = 44100
CHANNELS = 1
DTYPE = "int16"
BLOCK_SIZE = 1024
BLOCK_SECONDS = BLOCK_SIZE / SAMPLE_RATE
AUDIO_HEADER = struct.Struct("!dIH")

# ---- Viewer behaviour ------------------------------------------------------
MIC_ON_AT_CONNECT = True        # turn the host mic on as soon as we connect
PREBUFFER_SECONDS = 0.06        # audio held before playback starts (jitter)
MAX_BUFFER_SECONDS = 0.30       # beyond this, old audio is dropped (no lag creep)
SYNC_TOLERANCE = 0.02           # show a frame when it's within 20 ms of the audio
AUDIO_STALE_AFTER = 0.5         # no audio for this long -> show video immediately
# ---------------------------------------------------------------------------

_AUTH_HEADER = "Basic " + base64.b64encode(
    f"{USERNAME}:{PASSWORD}".encode()
).decode()


# ---- Discovery (unchanged from Ducky Cam v2) --------------------------------
def local_ipv4_addresses():
    """Every IPv4 address this machine holds, plus 0.0.0.0 (let the OS pick).

    We ask two ways because neither is complete on its own: getaddrinfo misses
    some adapters, and the connect-to-8.8.8.8 trick only reveals the one that
    routes to the internet.
    """
    ips = {"0.0.0.0"}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        ips.add(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()
    return sorted(ips)


def discover_host(timeout=5):
    """Broadcast a discovery ping and wait for the host to answer.

    Returns (advertised_ip, port, replying_ip) or None.

    The ping goes out EVERY local interface, not just the default one. That
    matters on machines with VirtualBox, VPN, WSL or Hyper-V adapters: Windows
    sends a plain 255.255.255.255 broadcast out only ONE interface, and it is
    often a virtual one, so the real network never sees the request. Binding a
    socket to each address forces the broadcast out that specific adapter. We
    also aim at each interface's own x.y.z.255, which some networks pass when
    they drop the all-ones address.
    """
    socks = []
    for local_ip in local_ipv4_addresses():
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.bind((local_ip, 0))
            sock.setblocking(False)
        except OSError:
            continue                      # adapter went away; just skip it
        targets = ["255.255.255.255"]
        if local_ip != "0.0.0.0":
            targets.append(".".join(local_ip.split(".")[:3]) + ".255")
        for target in targets:
            try:
                sock.sendto(DISCOVERY_REQUEST, (target, DISCOVERY_PORT))
            except OSError:
                pass                      # e.g. link down; other sockets remain
        socks.append(sock)

    if not socks:
        return None
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ready, _, _ = select.select(socks, [], [], 0.3)
            for sock in ready:
                try:
                    data, addr = sock.recvfrom(1024)
                except OSError:
                    continue
                text = data.decode(errors="ignore").strip()
                if not text.startswith(DISCOVERY_REPLY_PREFIX):
                    continue
                parts = text.split(":")
                ip = parts[1] if len(parts) > 1 and parts[1] else addr[0]
                port = parts[2] if len(parts) > 2 else "5000"
                return ip, port, addr[0]
        return None
    finally:
        for sock in socks:
            sock.close()


def discover_host_retry(attempt_timeout=5):
    attempt = 0
    while True:
        attempt += 1
        found = discover_host(timeout=attempt_timeout)
        if found:
            return found
        if attempt == 1 or attempt % 6 == 0:
            print("Still searching for the camera host... (Ctrl+C to stop)")
        if attempt == 6:
            print("   Tip: if the host is running, some networks block broadcast.")
            print("   Run it with the host's address instead, e.g.  viewer --ip 192.168.0.77")
        time.sleep(1.0)


# ---- Host control ----------------------------------------------------------
def api(base, path, quiet=False):
    req = urllib.request.Request(
        f"{base}{path}", headers={"Authorization": _AUTH_HEADER}
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode())
    except Exception as exc:  # noqa: BLE001
        if not quiet:
            print(f"[viewer] control call {path} failed: {exc}")
        return None


def open_stream(base, path):
    """Open a long-lived streaming response (video or audio)."""
    req = urllib.request.Request(
        f"{base}{path}", headers={"Authorization": _AUTH_HEADER}
    )
    return urllib.request.urlopen(req, timeout=10)


def stream_host_logs(base, stop_event):
    shown = 0
    while not stop_event.is_set():
        data = api(base, "/logs")
        if data and "lines" in data:
            for line in data["lines"][shown:]:
                print(f"[host] {line}")
            shown = len(data["lines"])
        time.sleep(1.0)


# ---- Audio: buffer + clock -------------------------------------------------
class AudioClock:
    """FIFO of PCM bytes between the network thread and the speaker callback,
    which also knows *what host time* is currently coming out of the speaker.
    That number is the sync reference the video loop waits on."""

    def __init__(self):
        self._buf = bytearray()
        self._lock = threading.Lock()
        self.primed = False
        self.underruns = 0
        self.overflows = 0
        self.chunks = 0
        self.dropped = 0
        self._last_seq = None
        self._last_ts = None            # host time at START of last pushed chunk
        self._last_seen = 0.0           # our monotonic when that chunk arrived

    def push(self, ts, seq, pcm):
        with self._lock:
            if self._last_seq is not None and seq > self._last_seq + 1:
                self.dropped += seq - self._last_seq - 1
            self._last_seq = seq
            self._last_ts = ts
            self._last_seen = time.monotonic()
            self.chunks += 1
            self._buf += pcm
            max_bytes = int(MAX_BUFFER_SECONDS * SAMPLE_RATE) * 2 * CHANNELS
            if len(self._buf) > max_bytes:
                del self._buf[: len(self._buf) - max_bytes]
                self.overflows += 1
            if not self.primed and len(self._buf) >= int(PREBUFFER_SECONDS * SAMPLE_RATE) * 2 * CHANNELS:
                self.primed = True

    def reset(self):
        """Mic went off: forget the sequence so the next on doesn't count a gap."""
        with self._lock:
            self._last_seq = None

    def pull(self, nbytes):
        with self._lock:
            if not self.primed:
                return bytes(nbytes)
            chunk = bytes(self._buf[:nbytes])
            del self._buf[:nbytes]
        if len(chunk) < nbytes:
            self.underruns += 1
            self.primed = False
            chunk += bytes(nbytes - len(chunk))
        return chunk

    def buffered_seconds(self):
        with self._lock:
            return len(self._buf) / (2 * CHANNELS) / SAMPLE_RATE

    def active(self):
        return (time.monotonic() - self._last_seen) < AUDIO_STALE_AFTER

    def playhead(self):
        """Host time of the audio leaving the speaker right now, or None."""
        with self._lock:
            if self._last_ts is None or not self.primed:
                return None
            newest_end = self._last_ts + BLOCK_SECONDS
            buffered = len(self._buf) / (2 * CHANNELS) / SAMPLE_RATE
        return newest_end - buffered


def read_exact(resp, n):
    """Read exactly n bytes from a streaming response, or None on EOF."""
    chunks = []
    remaining = n
    while remaining > 0:
        piece = resp.read(remaining)
        if not piece:
            return None
        chunks.append(piece)
        remaining -= len(piece)
    return b"".join(chunks)


def audio_reader(base, clock, wav, stop_event):
    """Pull /audio and feed the clock. Reconnects if the host drops us."""
    while not stop_event.is_set():
        try:
            resp = open_stream(base, "/audio")
        except Exception as exc:  # noqa: BLE001
            print(f"[viewer] audio connect failed: {exc}")
            time.sleep(1.0)
            continue
        with resp:
            while not stop_event.is_set():
                hdr = read_exact(resp, AUDIO_HEADER.size)
                if hdr is None:
                    break
                ts, seq, nbytes = AUDIO_HEADER.unpack(hdr)
                if nbytes == 0:             # heartbeat: mic is off / quiet
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
            time.sleep(0.5)


# ---- Video: MJPEG parser ---------------------------------------------------
def video_reader(base, frames, frames_lock, stop_event, counters):
    """Parse the host's MJPEG stream by hand so we get each frame's timestamp.
    (cv2.VideoCapture would hide the headers and add its own buffering lag.)"""
    try:
        resp = open_stream(base, "/video")
    except Exception as exc:  # noqa: BLE001
        print(f"[viewer] video connect failed: {exc}")
        counters["video_error"] = True
        return
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
            frame = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
            if frame is None:
                continue
            with frames_lock:
                frames.append((ts, frame))
            counters["frames_received"] += 1
    counters["video_ended"] = True


def pick_frame(frames, frames_lock, clock, counters):
    """Choose which frame to show now. With audio playing, show the newest
    frame whose host time has already been heard; otherwise show the newest."""
    playhead = clock.playhead() if clock.active() else None
    with frames_lock:
        if not frames:
            return None
        if playhead is None:
            chosen = frames[-1]
            frames.clear()
            counters["shown_unsynced"] += 1
            return chosen[1]
        chosen = None
        while frames and (frames[0][0] != frames[0][0] or frames[0][0] <= playhead + SYNC_TOLERANCE):
            chosen = frames.popleft()
        if chosen is None and frames and frames[0][0] - playhead > 1.0:
            # Clocks disagree wildly (host restarted?) -- don't freeze, just show it.
            chosen = frames.popleft()
        if chosen is None:
            return None
        if chosen[0] == chosen[0]:      # not NaN
            counters["shown_synced"] += 1
            counters["sync_error_sum"] += abs(chosen[0] - playhead)
        return chosen[1]


# ---- Main --------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Ducky Cam with Audio viewer.")
    ap.add_argument("--seconds", type=float, default=0, help="auto-quit after N s")
    ap.add_argument("--record", metavar="FILE.wav", help="save received audio")
    ap.add_argument("--device", type=int, default=None, help="speaker device index")
    ap.add_argument("--ip", help="host's IP address (skips discovery)")
    ap.add_argument("--port", default="5000", help="host's port (default 5000)")
    args = ap.parse_args()

    if args.ip:
        candidates = [(args.ip, args.port)]
        print(f"Connecting to {args.ip}:{args.port} (discovery skipped)...")
    else:
        print("Searching the network for the camera host...")
        try:
            ip, port, replied_from = discover_host_retry()
        except KeyboardInterrupt:
            print("\nStopped searching.")
            return
        print(f"Found the host at {ip}:{port}")
        # The host advertises the address of whichever adapter routes to the
        # internet. On a machine with virtual adapters that is not always the
        # one we can reach, so keep the address the reply actually came from
        # as a backup.
        candidates = [(ip, port)]
        if replied_from and replied_from != ip:
            candidates.append((replied_from, port))

    base = None
    status = None
    for cand_ip, cand_port in candidates:
        trial = f"http://{cand_ip}:{cand_port}"
        status = api(trial, "/status", quiet=True)
        if status is not None:
            base = trial
            if (cand_ip, cand_port) != candidates[0]:
                print(f"Host advertised an unreachable address; using {cand_ip} instead.")
            break
    if base is None:
        tried = ", ".join(f"{i}:{p}" for i, p in candidates)
        print(f"Couldn't reach the host's control API (tried {tried}).")
        print("Check that camera_server is running and that Windows Firewall "
              "allows it on this network.")
        return
    if status.get("active"):
        print("Host camera is already ON.")
    else:
        print("Host camera is OFF -> turning it ON...")
        api(base, "/start")
    if status.get("mic"):
        print("Host mic is already ON.")
    elif MIC_ON_AT_CONNECT:
        print("Host mic is OFF -> turning it ON...")
        api(base, "/mic/start")

    stop_event = threading.Event()
    threading.Thread(
        target=stream_host_logs, args=(base, stop_event), daemon=True
    ).start()

    # Audio out
    clock = AudioClock()
    wav = None
    if args.record:
        wav = wave.open(args.record, "wb")
        wav.setnchannels(CHANNELS)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)

    def speaker_callback(outdata, frames_n, time_info, status_flags):
        nbytes = frames_n * 2 * CHANNELS
        outdata[:] = np.frombuffer(clock.pull(nbytes), dtype=DTYPE).reshape(frames_n, CHANNELS)

    speaker = None
    try:
        speaker = sd.OutputStream(
            samplerate=SAMPLE_RATE, channels=CHANNELS, dtype=DTYPE,
            blocksize=BLOCK_SIZE, device=args.device, callback=speaker_callback,
        )
        speaker.start()
    except Exception as exc:  # noqa: BLE001
        print(f"[viewer] no speaker output ({exc}); video only.")
        speaker = None

    threading.Thread(
        target=audio_reader, args=(base, clock, wav, stop_event), daemon=True
    ).start()

    # Video in
    frames = deque(maxlen=60)
    frames_lock = threading.Lock()
    counters = {
        "frames_received": 0, "shown_synced": 0, "shown_unsynced": 0,
        "sync_error_sum": 0.0, "video_ended": False, "video_error": False,
    }
    threading.Thread(
        target=video_reader,
        args=(base, frames, frames_lock, stop_event, counters), daemon=True,
    ).start()

    print("Live. Keys:  m = mic on/off  |  q = quit + all off  |  l = quit, leave on")
    shut_down = False
    mic_on = bool(status.get("mic")) or MIC_ON_AT_CONNECT
    started = time.monotonic()
    last_report = started
    window = "Ducky Cam with Audio (auto-discovered)"
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

            frame = pick_frame(frames, frames_lock, clock, counters)
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
                    api(base, "/mic/stop")
                    mic_on = False
                    print("[viewer] host mic OFF")
                else:
                    api(base, "/mic/start")
                    mic_on = True
                    print("[viewer] host mic ON")

            now = time.monotonic()
            if now - last_report >= 5.0:
                last_report = now
                synced = counters["shown_synced"]
                avg = (counters["sync_error_sum"] / synced * 1000) if synced else 0
                print(
                    f"[viewer] frames={counters['frames_received']} "
                    f"audio chunks={clock.chunks} dropped={clock.dropped} "
                    f"underruns={clock.underruns} buffer={clock.buffered_seconds()*1000:.0f}ms "
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
        api(base, "/stop")
        api(base, "/mic/stop")
    synced = counters["shown_synced"]
    avg = (counters["sync_error_sum"] / synced * 1000) if synced else 0
    print(
        f"Viewer closed. frames={counters['frames_received']} shown synced={synced} "
        f"unsynced={counters['shown_unsynced']} avg A/V offset={avg:.0f}ms | "
        f"audio chunks={clock.chunks} dropped={clock.dropped} underruns={clock.underruns}"
    )


if __name__ == "__main__":
    main()
