"""
Telephone — SENDER (the "mouthpiece"; runs on the machine with the microphone).

Fully automatic: it finds the receiver by UDP broadcast (no IP to type), then
streams the microphone to it as raw PCM over UDP. It keeps searching until the
receiver appears, so you can start either script first.

    pip install sounddevice numpy
    python sender.py

Options (all optional):
    --ip A.B.C.D      skip discovery and send straight to this receiver
    --test-tone       send a 440 Hz beep instead of the mic (no mic needed)
    --seconds N       stop automatically after N seconds (for tests)
    --device N        input device index (see:  python -m sounddevice)

Press Ctrl+C to stop.
"""

import argparse
import socket
import struct
import time

import numpy as np
import sounddevice as sd

# ---- Audio format (must match receiver.py exactly) ------------------------
SAMPLE_RATE = 44100
CHANNELS = 1
DTYPE = "int16"
BLOCK_SIZE = 512        # 1024 bytes per packet -> one Ethernet frame, ~11.6 ms

# ---- Network (must match receiver.py) --------------------------------------
AUDIO_PORT = 5005
DISCOVERY_PORT = 50506
DISCOVERY_REQUEST = b"TELEPHONE_DISCOVERY_V1"
DISCOVERY_REPLY_PREFIX = "TELEPHONE_HERE"

MAGIC = b"TEL1"
HEADER = struct.Struct("!4sI")      # magic + uint32 sequence number
# ---------------------------------------------------------------------------


def discover_receiver(timeout=5):
    """Broadcast a discovery ping and wait for the receiver to answer with its
    IP and audio port. Returns (ip, port) or None if nobody answered."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(timeout)
    try:
        sock.sendto(DISCOVERY_REQUEST, ("255.255.255.255", DISCOVERY_PORT))
        while True:
            data, addr = sock.recvfrom(1024)
            text = data.decode(errors="ignore").strip()
            if text.startswith(DISCOVERY_REPLY_PREFIX):
                parts = text.split(":")
                ip = parts[1] if len(parts) > 1 and parts[1] else addr[0]
                port = int(parts[2]) if len(parts) > 2 else AUDIO_PORT
                return ip, port
    except (socket.timeout, OSError):
        return None
    finally:
        sock.close()


def discover_receiver_retry(attempt_timeout=5):
    """Keep broadcasting until a receiver answers. Ctrl+C to give up."""
    attempt = 0
    while True:
        attempt += 1
        found = discover_receiver(timeout=attempt_timeout)
        if found:
            return found
        if attempt == 1 or attempt % 6 == 0:
            print("Still searching for the receiver... (Ctrl+C to stop)")
        time.sleep(1.0)


class Sender:
    """Wraps the UDP socket and the running packet counter."""

    def __init__(self, ip, port):
        self.target = (ip, port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.seq = 0
        self.sent = 0
        self.errors = 0

    def send_block(self, pcm_bytes):
        packet = HEADER.pack(MAGIC, self.seq & 0xFFFFFFFF) + pcm_bytes
        try:
            self.sock.sendto(packet, self.target)
            self.sent += 1
        except OSError:
            self.errors += 1
        self.seq += 1

    def close(self):
        self.sock.close()


def stream_microphone(sender, device, seconds):
    """Open the mic; sounddevice calls audio_callback each time BLOCK_SIZE
    frames are ready, and we ship them straight out as one packet."""
    status_shown = [False]

    def audio_callback(indata, frames, time_info, status):
        if status and not status_shown[0]:
            print(f"[sender] mic status: {status}")
            status_shown[0] = True
        sender.send_block(indata.tobytes())

    with sd.InputStream(
        samplerate=SAMPLE_RATE, channels=CHANNELS, dtype=DTYPE,
        blocksize=BLOCK_SIZE, device=device, callback=audio_callback,
    ):
        run_until_done(sender, seconds)


def stream_test_tone(sender, seconds):
    """Send a steady 440 Hz sine wave, paced in real time, so the link can be
    tested on a machine with no microphone (or by a script)."""
    freq = 440.0
    amplitude = 8000                   # about a quarter of full scale
    block_seconds = BLOCK_SIZE / SAMPLE_RATE
    n = 0
    started = time.monotonic()
    next_send = started
    print(f"[sender] sending {freq:.0f} Hz test tone")
    while True:
        if seconds and time.monotonic() - started >= seconds:
            break
        t = (np.arange(BLOCK_SIZE) + n) / SAMPLE_RATE
        block = (amplitude * np.sin(2 * np.pi * freq * t)).astype(DTYPE)
        sender.send_block(block.tobytes())
        n += BLOCK_SIZE
        next_send += block_seconds
        delay = next_send - time.monotonic()
        if delay > 0:
            time.sleep(delay)


def run_until_done(sender, seconds):
    """Keep the main thread alive while the mic callback does the work."""
    started = time.monotonic()
    last_report = started
    while True:
        if seconds and time.monotonic() - started >= seconds:
            return
        sd.sleep(200)
        now = time.monotonic()
        if now - last_report >= 5.0:
            last_report = now
            print(f"[sender] packets sent={sender.sent} errors={sender.errors}")


def main():
    ap = argparse.ArgumentParser(description="Telephone sender (microphone).")
    ap.add_argument("--ip", help="receiver IP (skips discovery)")
    ap.add_argument("--port", type=int, default=AUDIO_PORT, help="receiver port")
    ap.add_argument("--test-tone", action="store_true", help="send a beep, not the mic")
    ap.add_argument("--seconds", type=float, default=0, help="auto-stop after N s")
    ap.add_argument("--device", type=int, default=None, help="input device index")
    args = ap.parse_args()

    if args.ip:
        ip, port = args.ip, args.port
    else:
        print("Searching the network for the receiver...")
        try:
            ip, port = discover_receiver_retry()
        except KeyboardInterrupt:
            print("\nStopped searching.")
            return
    print(f"Found the receiver at {ip}:{port}")

    sender = Sender(ip, port)
    try:
        if args.test_tone:
            stream_test_tone(sender, args.seconds)
        else:
            print("Live. Talk into the mic. Press Ctrl+C to hang up.")
            stream_microphone(sender, args.device, args.seconds)
    except KeyboardInterrupt:
        print("\n[sender] hanging up...")
    finally:
        sender.close()
        print(f"[sender] done. packets sent={sender.sent} errors={sender.errors}")


if __name__ == "__main__":
    main()
