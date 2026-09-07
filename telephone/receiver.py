"""
Telephone — RECEIVER (the "earpiece"; runs on the machine with the speakers).

Run this first (or second -- the sender keeps looking until it finds you).
It answers LAN discovery pings so the sender needs no IP, then plays whatever
audio arrives over UDP through the speakers.

    pip install sounddevice numpy
    python receiver.py

Options (all optional):
    --record out.wav   also save everything received to a WAV file
    --no-play          don't open the speakers (useful with --record for tests)
    --seconds N        stop automatically after N seconds (for tests)
    --device N         output device index (see:  python -m sounddevice)
    --port N           audio port to listen on (default 5005)

Press Ctrl+C to stop.
"""

import argparse
import socket
import struct
import threading
import time
import wave

import numpy as np
import sounddevice as sd

# ---- Audio format (must match sender.py exactly) --------------------------
SAMPLE_RATE = 44100     # CD quality
CHANNELS = 1            # mono
DTYPE = "int16"         # 2 bytes per sample
BLOCK_SIZE = 512        # frames per packet: 512 * 2 bytes = 1024 bytes, which
                        # fits in one Ethernet frame (no IP fragmentation), and
                        # is ~11.6 ms of audio per packet.

# ---- Network ---------------------------------------------------------------
AUDIO_PORT = 5005
DISCOVERY_PORT = 50506
DISCOVERY_REQUEST = b"TELEPHONE_DISCOVERY_V1"
DISCOVERY_REPLY_PREFIX = b"TELEPHONE_HERE"

# Every packet is: MAGIC (4 bytes) + sequence number (uint32 big-endian) + PCM.
# The header lets us ignore stray packets and count drops.
MAGIC = b"TEL1"
HEADER = struct.Struct("!4sI")
PAYLOAD_BYTES = BLOCK_SIZE * 2 * CHANNELS

# ---- Jitter buffer ---------------------------------------------------------
# Wi-Fi delivers packets in bursts. We hold a little audio before playing so
# the speakers never starve on a small hiccup. Bigger = smoother but laggier.
PREBUFFER_PACKETS = 4               # ~46 ms before playback starts
MAX_BUFFER_PACKETS = 20             # ~230 ms; beyond this we drop the oldest
                                    # audio so lag can't creep upward forever
# ---------------------------------------------------------------------------


class AudioBuffer:
    """Thread-safe FIFO of raw PCM bytes between the network thread (producer)
    and the sounddevice callback (consumer)."""

    def __init__(self):
        self._buf = bytearray()
        self._lock = threading.Lock()
        self.primed = False         # becomes True once PREBUFFER is reached
        self.underruns = 0          # times the speaker wanted more than we had
        self.overflows = 0          # times we threw away old audio (too much lag)

    def push(self, pcm: bytes):
        with self._lock:
            self._buf += pcm
            max_bytes = MAX_BUFFER_PACKETS * PAYLOAD_BYTES
            if len(self._buf) > max_bytes:
                # Too far behind: keep only the freshest audio.
                del self._buf[: len(self._buf) - max_bytes]
                self.overflows += 1
            if not self.primed and len(self._buf) >= PREBUFFER_PACKETS * PAYLOAD_BYTES:
                self.primed = True

    def pull(self, nbytes: int) -> bytes:
        """Return exactly nbytes; pad with silence if we don't have enough."""
        with self._lock:
            if not self.primed:
                return bytes(nbytes)
            chunk = bytes(self._buf[:nbytes])
            del self._buf[:nbytes]
        if len(chunk) < nbytes:
            self.underruns += 1
            self.primed = False     # re-buffer before resuming
            chunk += bytes(nbytes - len(chunk))
        return chunk

    def buffered_ms(self) -> float:
        with self._lock:
            frames = len(self._buf) / (2 * CHANNELS)
        return 1000.0 * frames / SAMPLE_RATE


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


def discovery_responder(audio_port, stop_event):
    """Answer LAN discovery pings so the sender can find us with no hardcoded
    IP. Listens for a UDP broadcast and replies to whoever sent it."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.settimeout(0.5)
    try:
        sock.bind(("", DISCOVERY_PORT))
    except OSError as exc:
        print(f"[receiver] discovery could not bind UDP {DISCOVERY_PORT}: {exc}")
        return
    while not stop_event.is_set():
        try:
            data, addr = sock.recvfrom(1024)
        except socket.timeout:
            continue
        except OSError:
            break
        if data.strip() == DISCOVERY_REQUEST:
            reply = DISCOVERY_REPLY_PREFIX + f":{get_local_ip()}:{audio_port}".encode()
            try:
                sock.sendto(reply, addr)
            except OSError:
                pass
    sock.close()


def main():
    ap = argparse.ArgumentParser(description="Telephone receiver (speakers).")
    ap.add_argument("--record", metavar="FILE.wav", help="save received audio")
    ap.add_argument("--no-play", action="store_true", help="don't open speakers")
    ap.add_argument("--seconds", type=float, default=0, help="auto-stop after N s")
    ap.add_argument("--device", type=int, default=None, help="output device index")
    ap.add_argument("--port", type=int, default=AUDIO_PORT, help="audio UDP port")
    args = ap.parse_args()

    buf = AudioBuffer()
    stop_event = threading.Event()
    stats = {"packets": 0, "dropped": 0, "bad": 0, "last_seq": None, "peer": None}

    # Speaker stream: sounddevice calls this whenever it wants more audio.
    def callback(outdata, frames, time_info, status):
        nbytes = frames * 2 * CHANNELS
        outdata[:] = np.frombuffer(buf.pull(nbytes), dtype=DTYPE).reshape(frames, CHANNELS)

    wav = None
    if args.record:
        wav = wave.open(args.record, "wb")
        wav.setnchannels(CHANNELS)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", args.port))
    sock.settimeout(0.5)

    threading.Thread(
        target=discovery_responder, args=(args.port, stop_event), daemon=True
    ).start()

    print("=" * 60)
    print("  Telephone receiver is listening.")
    print(f"  This machine's IP:  {get_local_ip()}   audio port: {args.port}")
    print("  The sender finds this machine automatically -- just run")
    print("  sender.py on any machine on the same network.")
    print("  Press Ctrl+C to stop.")
    print("=" * 60)

    stream = None
    if not args.no_play:
        stream = sd.OutputStream(
            samplerate=SAMPLE_RATE, channels=CHANNELS, dtype=DTYPE,
            blocksize=BLOCK_SIZE, device=args.device, callback=callback,
        )
        stream.start()

    started = time.monotonic()
    last_report = started
    try:
        while True:
            if args.seconds and time.monotonic() - started >= args.seconds:
                break
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue

            if len(data) != HEADER.size + PAYLOAD_BYTES:
                stats["bad"] += 1
                continue
            magic, seq = HEADER.unpack_from(data)
            if magic != MAGIC:
                stats["bad"] += 1
                continue

            if stats["peer"] != addr[0]:
                stats["peer"] = addr[0]
                stats["last_seq"] = None
                print(f"[receiver] audio from {addr[0]}")
            if stats["last_seq"] is not None and seq > stats["last_seq"] + 1:
                stats["dropped"] += seq - stats["last_seq"] - 1
            stats["last_seq"] = seq
            stats["packets"] += 1

            pcm = data[HEADER.size:]
            if stream:                  # only queue audio if someone is playing it
                buf.push(pcm)
            if wav:
                wav.writeframes(pcm)

            now = time.monotonic()
            if now - last_report >= 5.0:
                last_report = now
                print(
                    f"[receiver] packets={stats['packets']} dropped={stats['dropped']} "
                    f"underruns={buf.underruns} overflows={buf.overflows} "
                    f"buffer={buf.buffered_ms():.0f}ms"
                )
    except KeyboardInterrupt:
        print("\n[receiver] stopping...")
    finally:
        stop_event.set()
        if stream:
            stream.stop()
            stream.close()
        if wav:
            wav.close()
        sock.close()
        print(
            f"[receiver] done. packets={stats['packets']} dropped={stats['dropped']} "
            f"bad={stats['bad']} underruns={buf.underruns} overflows={buf.overflows}"
        )


if __name__ == "__main__":
    main()
