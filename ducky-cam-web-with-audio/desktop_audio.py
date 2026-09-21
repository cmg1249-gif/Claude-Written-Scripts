"""Windows speaker-output capture; never opens a microphone input."""
import queue
import threading

import numpy as np
import pyaudiowpatch as pa


class AudioUnavailable(RuntimeError):
    pass


def default_loopback(manager):
    try:
        device = manager.get_default_wasapi_loopback()
        if not device.get('isLoopbackDevice') or not device.get('maxInputChannels'):
            raise AudioUnavailable('No desktop-audio loopback device is available.')
        return device
    except (OSError, ValueError, LookupError) as exc:
        raise AudioUnavailable('No Windows speaker-output device is available.') from exc


def audio_info():
    with pa.PyAudio() as manager:
        device = default_loopback(manager)
        return {'available': True, 'device': device['name'],
                'sample_rate': int(device['defaultSampleRate']), 'channels': 2, 'format': 's16le'}


def stereo_pcm(data, channels):
    samples = np.frombuffer(data, dtype='<i2').reshape(-1, channels)
    if channels == 2:
        return data
    if channels == 1:
        return np.repeat(samples, 2, axis=1).tobytes()
    # Preserve the front stereo pair and include the remaining surround channels.
    extra = samples[:, 2:].astype(np.float32).mean(axis=1, keepdims=True) * 0.5
    mixed = samples[:, :2].astype(np.float32) + extra
    return np.clip(mixed, -32768, 32767).astype('<i2').tobytes()


class LoopbackCapture:
    def __init__(self):
        self.manager = pa.PyAudio()
        self.stream = None
        self.closed = threading.Event()
        self.chunks = queue.Queue(maxsize=8)
        try:
            device = default_loopback(self.manager)
            self.sample_rate = int(device['defaultSampleRate'])
            self.input_channels = int(device['maxInputChannels'])
            self.channels = 2
            self.device_name = device['name']
            self.stream = self.manager.open(
                format=pa.paInt16, channels=self.input_channels, rate=self.sample_rate,
                input=True, input_device_index=device['index'], frames_per_buffer=512,
                stream_callback=self._callback,
            )
        except Exception:
            self.close()
            raise

    def _callback(self, data, frame_count, time_info, status):
        if self.closed.is_set():
            return None, pa.paComplete
        try:
            self.chunks.put_nowait(data)
        except queue.Full:
            try:
                self.chunks.get_nowait()
            except queue.Empty:
                pass
            try:
                self.chunks.put_nowait(data)
            except queue.Full:
                pass
        return None, pa.paContinue

    def read(self):
        if self.closed.is_set():
            return b''
        try:
            data = self.chunks.get(timeout=0.02)
        except queue.Empty:
            if self.stream is not None and not self.stream.is_active():
                raise AudioUnavailable('The desktop-audio device disconnected.')
            # WASAPI can stop callbacks during silence. Keep the HTTP stream alive.
            return bytes((self.sample_rate // 50) * self.channels * 2)
        return stereo_pcm(data, self.input_channels)

    def close(self):
        if self.closed.is_set():
            return
        self.closed.set()
        if self.stream is not None:
            self.stream.close()
        self.manager.terminate()


class AudioOutput:
    def __init__(self, sample_rate, channels=2):
        self.manager = pa.PyAudio()
        self.stream = None
        try:
            self.stream = self.manager.open(format=pa.paInt16, channels=channels,
                                            rate=sample_rate, output=True, frames_per_buffer=512)
        except Exception:
            self.manager.terminate()
            raise

    def write(self, data):
        self.stream.write(data, exception_on_underflow=False)

    def close(self):
        if self.stream is not None:
            try:
                self.stream.close()
            finally:
                self.stream = None
                self.manager.terminate()


class AudioSubscription:
    def __init__(self, hub, sample_rate):
        self.hub = hub
        self.sample_rate = sample_rate
        self.chunks = queue.Queue(maxsize=8)
        self.closed = threading.Event()

    def read(self):
        while not self.closed.is_set():
            try:
                chunk = self.chunks.get(timeout=0.2)
            except queue.Empty:
                if self.hub.stopped.is_set():
                    return b''
                continue
            if isinstance(chunk, Exception):
                raise chunk
            return chunk
        return b''

    def close(self):
        self.closed.set()
        with self.hub.lock:
            self.hub.subscribers.discard(self)


class AudioHub:
    """One device-owning thread; reconnecting clients receive independent queues."""
    def __init__(self):
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.ready = threading.Event()
        self.thread = None
        self.error = None
        self.sample_rate = None
        self.subscribers = set()

    def subscribe(self, factory=LoopbackCapture):
        with self.lock:
            if self.stopped.is_set():
                raise AudioUnavailable('Audio server is stopping.')
            if self.thread is None or not self.thread.is_alive():
                self.ready.clear()
                self.error = None
                self.thread = threading.Thread(target=self._run, args=(factory,), daemon=True)
                self.thread.start()
        if not self.ready.wait(10):
            raise AudioUnavailable('Speaker capture startup timed out.')
        with self.lock:
            if self.error is not None:
                raise AudioUnavailable(str(self.error)) from self.error
            subscription = AudioSubscription(self, self.sample_rate)
            self.subscribers.add(subscription)
            return subscription

    @staticmethod
    def _deliver(subscription, chunk):
        if subscription.chunks.full():
            try:
                subscription.chunks.get_nowait()
            except queue.Empty:
                pass
        subscription.chunks.put_nowait(chunk)

    def _run(self, factory):
        capture = None
        try:
            capture = factory()
            with self.lock:
                self.sample_rate = capture.sample_rate
            self.ready.set()
            while not self.stopped.is_set():
                chunk = capture.read()
                if not chunk:
                    raise AudioUnavailable('Speaker capture ended.')
                with self.lock:
                    for subscriber in self.subscribers:
                        self._deliver(subscriber, chunk)
        except Exception as exc:
            with self.lock:
                self.error = exc
                for subscriber in self.subscribers:
                    self._deliver(subscriber, AudioUnavailable(str(exc)))
                self.subscribers.clear()
        finally:
            self.ready.set()
            if capture is not None:
                capture.close()

    def close(self):
        self.stopped.set()
        if self.thread is not None:
            self.thread.join(timeout=3)
