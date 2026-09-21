"""Desktop video and a fixed-format, timestamped microphone/speaker mix."""
import audioop
import queue
import struct
import threading
import time

import cv2
import mss
import numpy as np
from desktop_audio import AudioHub, AudioUnavailable

RATE = 48000
CHANNELS = 2
HEADER = struct.Struct('!dIH')
FRAMES = 960


def list_monitors():
    with mss.MSS() as capture:
        return [dict(id=i, name=monitor.get('name') or f'Monitor {i}',
                     **{key: monitor[key] for key in ('width', 'height', 'left', 'top')})
                for i, monitor in enumerate(capture.monitors[1:], 1)]


def desktop_frames(monitor, keep_running, width=1600, fps=12, quality=70):
    with mss.MSS() as capture:
        if not 1 <= monitor < len(capture.monitors):
            return
        while keep_running():
            started = time.monotonic()
            frame = np.asarray(capture.grab(capture.monitors[monitor]))[:, :, :3]
            if width and frame.shape[1] > width:
                frame = cv2.resize(frame, (width, round(frame.shape[0] * width / frame.shape[1])))
            ok, data = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
            if ok:
                jpeg = data.tobytes()
                yield (b'--frame\r\nContent-Type: image/jpeg\r\n'
                       + f'X-Timestamp: {started:.6f}\r\nContent-Length: {len(jpeg)}\r\n\r\n'.encode()
                       + jpeg + b'\r\n')
            time.sleep(max(0, 1 / fps - (time.monotonic() - started)))


class PCMBuffer:
    """Stateful resampling keeps fractional samples across callback boundaries."""
    def __init__(self):
        self.data = bytearray()
        self.state = None
        self.rate = None

    def clear(self):
        self.data.clear()
        self.state = None
        self.rate = None

    def feed(self, pcm, rate, channels):
        if self.rate != rate:
            self.clear()
            self.rate = rate
        if channels == 1:
            pcm = audioop.tostereo(pcm, 2, 1, 1)
        if rate != RATE:
            pcm, self.state = audioop.ratecv(pcm, 2, 2, rate, RATE, self.state)
        self.data.extend(pcm)
        # Bound latency to 200 ms if a source or client stalls.
        limit = RATE // 5 * 4
        if len(self.data) > limit:
            del self.data[:-limit]

    def take(self):
        size = FRAMES * 4
        result = bytes(self.data[:size])
        del self.data[:size]
        return result + bytes(size - len(result))


class AudioMixer:
    def __init__(self):
        self.lock = threading.RLock()
        self.desktop_lock = threading.Lock()
        self.mic = PCMBuffer()
        self.desktop = PCMBuffer()
        self.mic_enabled = False
        self.desktop_subscription = None
        self.hub = AudioHub()
        self.error = ''
        self.subscribers = set()
        self.stop = threading.Event()
        self.thread = None

    def start(self):
        with self.lock:
            if self.thread is None:
                self.thread = threading.Thread(target=self._run, daemon=True)
                self.thread.start()

    def feed_mic(self, pcm, rate):
        with self.lock:
            if self.mic_enabled:
                self.mic.feed(pcm, rate, 1)

    def set_mic(self, enabled):
        with self.lock:
            self.mic_enabled = enabled
            self.mic.clear()

    def set_desktop(self, enabled):
        with self.desktop_lock:
            if enabled:
                if self.desktop_subscription is not None:
                    return
                subscription = self.hub.subscribe()
                with self.lock:
                    self.desktop_subscription = subscription
                    self.error = ''
            else:
                with self.lock:
                    subscription, self.desktop_subscription = self.desktop_subscription, None
                    self.desktop.clear()
                if subscription:
                    subscription.close()
                self.hub.close()
                self.hub = AudioHub()

    def _mix(self):
        with self.lock:
            subscription = self.desktop_subscription
            if subscription:
                while True:
                    try:
                        chunk = subscription.chunks.get_nowait()
                    except queue.Empty:
                        break
                    if isinstance(chunk, Exception):
                        self.error = str(chunk)
                        self.desktop_subscription = None
                        self.desktop.clear()
                        subscription.close()
                        break
                    self.desktop.feed(chunk, subscription.sample_rate, 2)
            mic = np.frombuffer(self.mic.take(), dtype='<i2').astype(np.int32)
            desktop = np.frombuffer(self.desktop.take(), dtype='<i2').astype(np.int32)
            gain = 0.5 if self.mic_enabled and self.desktop_subscription else 1.0
            return np.clip((mic + desktop) * gain, -32768, 32767).astype('<i2').tobytes()

    def _run(self):
        seq = 0
        deadline = time.monotonic()
        while not self.stop.is_set():
            timestamp = time.monotonic() - FRAMES / RATE
            pcm = self._mix()
            packet = HEADER.pack(timestamp, seq & 0xffffffff, len(pcm)) + pcm
            seq += 1
            with self.lock:
                for subscriber in self.subscribers:
                    if subscriber.full():
                        try:
                            subscriber.get_nowait()
                        except queue.Empty:
                            pass
                    subscriber.put_nowait(packet)
            deadline += FRAMES / RATE
            now = time.monotonic()
            if deadline < now - .1:
                deadline = now
            self.stop.wait(max(0, deadline - now))

    def stream(self):
        self.start()
        subscriber = queue.Queue(maxsize=50)
        with self.lock:
            self.subscribers.add(subscriber)
        try:
            while not self.stop.is_set():
                try:
                    yield subscriber.get(timeout=1)
                except queue.Empty:
                    continue
        finally:
            with self.lock:
                self.subscribers.discard(subscriber)

    def close(self):
        self.stop.set()
        self.set_desktop(False)
        if self.thread:
            self.thread.join(timeout=2)
