import base64
import io
import json
import os
from pathlib import Path
import queue
import ssl
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import cv2
import numpy as np
import combined_media as media
import webcam_server as server
import viewer
from connection import APP, decode_advertisement
from discovery import discovery_key, signature
from tls import make_context


class CombinedTests(unittest.TestCase):
    def setUp(self):
        server.mixer = media.AudioMixer()
        self.addCleanup(server.mixer.close)
        server.camera = None
        server.mic_stream = None
        server.video_source = 'camera'
        server.desktop_wanted = False
        server.monitor_id = 1
        server.USERNAME, server.PASSWORD = 'admin', 'test-password'
        self.client = server.app.test_client()
        self.auth = {'Authorization': 'Basic ' + base64.b64encode(b'admin:test-password').decode()}

    def test_new_endpoints_require_password(self):
        for endpoint in ('/source/select', '/desktop-audio/start', '/desktop-audio/stop'):
            self.assertEqual(self.client.post(endpoint).status_code, 401)
        self.assertEqual(self.client.get('/monitors').status_code, 401)

    def test_server_does_not_prompt_or_store_a_password(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'config.ini'
            with patch.dict(os.environ, {'ROOMCAM_CONFIG': str(path), 'ROOMCAM_PASSWORD': 'ignored-password'}):
                config = server.load_config()
            self.assertNotIn('password', config)
            self.assertFalse(path.exists())

    def test_listener_pairing_and_lost_response_retry(self):
        server.PASSWORD = None
        server.pair_started = server.time.monotonic()
        token = self.client.get('/pair-info').json['token']
        body = dict(token=token, password='listener-password')
        self.assertEqual(self.client.post('/pair', json=body).status_code, 200)
        self.assertEqual(self.client.post('/pair', json=body).status_code, 200)
        self.assertEqual(self.client.post('/pair', json=dict(token=token,password='different-password')).status_code, 410)
        self.assertEqual(self.client.get('/pair-info').status_code, 410)

    def test_invalid_pairing_is_rejected(self):
        server.PASSWORD = None
        for body in ([], {}, {'token':3,'password':'abcdefgh'}, {'token':'bad','password':'short'}):
            self.assertEqual(self.client.post('/pair', json=body).status_code, 400)
        self.assertEqual(self.client.post('/pair', json=dict(token='bad', password='abcdefgh')).status_code, 403)

    def test_unclaimed_pairing_is_renewed(self):
        server.PASSWORD = None
        original = server.pair_token
        server.pair_started -= 1000
        result = self.client.get('/pair-info').json
        self.assertNotEqual(result['token'], original)
        self.assertEqual(self.client.post('/pair', json=dict(token=result['token'],password='abcdefgh')).status_code, 200)

    def test_invalid_monitor_does_not_change_source(self):
        with patch.object(server, 'list_monitors', return_value=[{'id': 1}]):
            for value, code in [('abc', 400), ('0', 404), ('2', 404)]:
                result = self.client.post('/source/select?source=desktop&monitor='+value, headers=self.auth)
                self.assertEqual(result.status_code, code)
                self.assertEqual(server.video_source, 'camera')

    def test_monitor_names_from_current_mss_are_preserved(self):
        capture = Mock()
        capture.monitors = [{}, dict(name='Physical monitor', width=1920, height=1080, left=-1920, top=0)]
        with patch.object(media.mss, 'MSS') as factory:
            factory.return_value.__enter__.return_value = capture
            result = media.list_monitors()
        self.assertEqual(result[0]['name'], 'Physical monitor')
        self.assertEqual(result[0]['id'], 1)
        self.assertEqual(result[0]['left'], -1920)

    def test_switch_desktop_stops_camera(self):
        camera = Mock()
        server.camera = camera
        with patch.object(server, 'list_monitors', return_value=[{'id': 2}]):
            result = self.client.post('/source/select?source=desktop&monitor=2', headers=self.auth)
        self.assertEqual(result.json['monitor'], 2)
        camera.release.assert_called_once()
        self.assertTrue(server.desktop_wanted)

    def test_camera_missing_falls_back_to_desktop(self):
        with patch.object(server, 'start_camera'), patch.object(server, 'list_monitors', return_value=[{'id': 1}]), patch.object(server, 'desktop_frames', return_value=iter([b'frame'])):
            result = self.client.get('/video', headers=self.auth)
            self.assertEqual(result.data, b'frame')
            self.assertEqual(server.video_source, 'desktop')

    def test_desktop_audio_failure_leaves_video_available(self):
        with patch.object(server.mixer, 'set_desktop', side_effect=RuntimeError('No speakers')):
            self.assertEqual(self.client.post('/desktop-audio/start', headers=self.auth).status_code, 503)
        self.assertEqual(self.client.get('/status', headers=self.auth).status_code, 200)

    def test_status_keeps_wire_format_when_mic_rate_changes(self):
        with patch.object(server, 'SAMPLE_RATE', 44100):
            result = self.client.get('/status', headers=self.auth).json
        self.assertEqual((result['sample_rate'], result['channels']), (48000, 2))

    def test_pcm_resampling_preserves_stereo_and_sample_count(self):
        buffer = media.PCMBuffer()
        samples = np.full(441, 1000, dtype='<i2').tobytes()
        for _ in range(2):
            buffer.feed(samples, 44100, 1)
        converted = np.frombuffer(buffer.data, dtype='<i2').reshape(-1, 2)
        self.assertLessEqual(abs(len(converted)-960), 1)
        np.testing.assert_array_equal(converted[:, 0], converted[:, 1])

    def test_rate_change_discards_old_format(self):
        buffer = media.PCMBuffer()
        buffer.feed(np.full(320, 1000, dtype='<i2').tobytes(), 16000, 1)
        buffer.feed(np.full(960, 2000, dtype='<i2').tobytes(), 48000, 1)
        self.assertTrue(np.all(np.frombuffer(buffer.take(), dtype='<i2') == 2000))

    def test_mic_disabled_discards_callback(self):
        server.mixer.feed_mic(bytes(1280), 16000)
        self.assertEqual(server.mixer.mic.data, b'')

    def test_mixing_keeps_both_sources_without_overflow(self):
        mixer = server.mixer
        mixer.set_mic(True)
        mixer.feed_mic(np.full(960, 24000, dtype='<i2').tobytes(), 48000)
        subscription = Mock(sample_rate=48000, chunks=queue.Queue())
        subscription.chunks.put(np.full(1920, 20000, dtype='<i2').tobytes())
        mixer.desktop_subscription = subscription
        mixed = np.frombuffer(mixer._mix(), dtype='<i2')
        self.assertTrue(np.all(mixed == 22000))

    def test_mix_silence_and_packet_timestamps(self):
        stream = server.mixer.stream()
        first, second = next(stream), next(stream)
        a = media.HEADER.unpack(first[:14]); b = media.HEADER.unpack(second[:14])
        self.assertGreater(b[0], a[0])
        self.assertEqual(b[1], a[1]+1)
        self.assertEqual(a[2], 3840)
        self.assertEqual(first[14:], bytes(3840))
        stream.close()
        self.assertFalse(server.mixer.subscribers)

    def test_audio_reconnect_does_not_reopen_speaker_capture(self):
        subscription = Mock(sample_rate=48000, chunks=queue.Queue())
        with patch.object(server.mixer.hub, 'subscribe', return_value=subscription) as subscribe:
            server.mixer.set_desktop(True)
            server.mixer.set_desktop(True)
            for _ in range(3):
                stream = server.mixer.stream()
                self.assertEqual(len(next(stream)), 3854)
                stream.close()
            subscribe.assert_called_once()

    def test_signed_discovery_rejects_tampering(self):
        payload = dict(app=APP, url='https://test-room.trycloudflare.com', session='demo', paired=True, created=1)
        payload['signature'] = signature(payload, discovery_key('password', 'demo'))
        self.assertIsNotNone(decode_advertisement(json.dumps(payload), 'password'))
        self.assertIsNone(decode_advertisement(json.dumps(payload), 'wrong'))
        self.assertIsNone(decode_advertisement(json.dumps(payload), 'password', 'other'))
        payload['url'] = 'https://attacker.trycloudflare.com'
        self.assertIsNone(decode_advertisement(json.dumps(payload), 'password'))

    def test_bundled_ca_without_os_roots(self):
        with patch.object(ssl.SSLContext, 'load_default_certs'):
            context = make_context()
        self.assertGreater(context.cert_store_stats()['x509_ca'], 50)
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)

    def test_control_calls_use_post(self):
        host = viewer.Host('http://127.0.0.1:1234', 'admin', 'password')
        with patch.object(viewer.urllib.request, 'urlopen', return_value=io.BytesIO(b'{}')) as opened:
            host.api('/source/select?source=desktop')
            self.assertEqual(opened.call_args.args[0].method, 'POST')

    def test_browser_has_source_and_separate_audio_controls(self):
        page = self.client.get('/', headers=self.auth).data.decode()
        for control in ('id="source"', 'id="monitor"', 'id="mic"', 'id="desktop"', 'id="stop"'):
            self.assertIn(control, page)

    def test_recording_remains_playable_after_source_size_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = viewer.Recorder(directory, 'mp4')
            frame = np.zeros((180, 320, 3), dtype=np.uint8)
            recorder.start(frame, 48000, 2)
            for i in range(20):
                recorder.write_video(frame if i < 10 else np.zeros((360, 640, 3), dtype=np.uint8))
                recorder.write_audio(bytes(4800*4))
            path = recorder.stop()
            self.assertTrue(Path(path).is_file())
            capture = cv2.VideoCapture(path)
            try:
                ok, decoded = capture.read()
                self.assertTrue(ok)
                self.assertEqual(decoded.shape[:2], (180, 320))
            finally:
                capture.release()

    def test_recording_name_collision_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = viewer.Recorder(directory, 'mp4')
            (Path(directory)/'roomcam_test.mp4').write_bytes(b'previous')
            self.assertEqual(recorder._unique_base('test'), 'roomcam_test_2')


if __name__ == '__main__':
    unittest.main()
