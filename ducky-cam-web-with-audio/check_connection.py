"""Packaged diagnostics. Public tests send generated media only."""
import argparse
import contextlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch

import cv2
import numpy as np
from werkzeug.serving import make_server
import combined_media as media
import viewer
import webcam_server as server
from desktop_audio import AudioOutput
from connection import Tunnel


def read_video(host, count=12):
    with host.open_stream('/video') as response:
        for _ in range(count):
            while True:
                line = response.readline()
                if not line:
                    raise RuntimeError('Video ended early')
                if line.startswith(b'--frame'):
                    break
            headers = {}
            while True:
                line = response.readline()
                if line in (b'\r\n', b'\n', b''):
                    break
                key, _, value = line.decode().partition(':')
                headers[key.lower()] = value.strip()
            data = viewer.read_exact(response, int(headers['content-length']))
            frame = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
            if frame is None:
                raise RuntimeError('Invalid video frame')
        print(f'PASS: {count} frames ({frame.shape[1]}x{frame.shape[0]})', flush=True)


def read_audio(host, frequency=None, play=False):
    with host.open_stream('/audio') as response:
        playback = None
        if play:
            def tone():
                output = AudioOutput(48000)
                try:
                    samples = (1500*np.sin(2*np.pi*997*np.arange(48000)/48000)).astype('<i2')
                    output.write(np.repeat(samples[:, None],2,axis=1).tobytes())
                finally:
                    output.close()
            playback = threading.Thread(target=tone)
            playback.start()
        pcm = bytearray()
        for _ in range(100):
            header = viewer.read_exact(response, media.HEADER.size)
            if header is None:
                raise RuntimeError('Audio ended early')
            timestamp, sequence, size = media.HEADER.unpack(header)
            chunk = viewer.read_exact(response, size)
            if chunk is None:
                raise RuntimeError('Audio body ended early')
            pcm.extend(chunk)
        if playback:
            playback.join(timeout=5)
        values = np.frombuffer(pcm, dtype='<i2').reshape(-1,2)[:,0].astype(float)
        rms = float(np.sqrt(np.mean(values**2)))
        if frequency:
            spectrum = abs(np.fft.rfft(values*np.hanning(len(values))))
            frequencies = np.fft.rfftfreq(len(values),1/48000)
            peak = spectrum[abs(frequencies-frequency)<4].max()
            noise = np.median(spectrum[(frequencies>frequency-100)&(frequencies<frequency+100)])+1
            if rms < 10 or peak/noise < 5:
                raise RuntimeError(f'Expected tone {frequency} Hz absent (RMS {rms:.1f})')
        print(f'PASS: 48000 Hz stereo, RMS {rms:.1f}'+(f', tone {frequency} Hz' if frequency else ''),flush=True)


@contextlib.contextmanager
def synthetic_host():
    password = secrets.token_urlsafe(24)
    server.PASSWORD = None
    server.USERNAME = 'admin'
    server.mixer = media.AudioMixer()
    stop = threading.Event()
    frame = np.zeros((360,640,3),dtype=np.uint8)
    cv2.putText(frame,'RoomCam generated test image',(30,180),cv2.FONT_HERSHEY_SIMPLEX,.8,(40,220,130),2)
    jpeg = cv2.imencode('.jpg',frame)[1].tobytes()
    def frames(*args, **kwargs):
        while not stop.is_set():
            yield (b'--frame\r\nContent-Type: image/jpeg\r\n'+f'X-Timestamp: {time.monotonic():.6f}\r\nContent-Length: {len(jpeg)}\r\n\r\n'.encode()+jpeg+b'\r\n')
            time.sleep(1/12)
    def fake_camera():
        server.camera = type('Camera',(),{'release':lambda self:None})()
        return True
    class Tone:
        sample_rate=48000
        def __init__(self): self.position=0
        def read(self):
            time.sleep(.02)
            indices=np.arange(960)+self.position;self.position+=960
            mono=(1500*np.sin(2*np.pi*997*indices/48000)).astype('<i2')
            return np.repeat(mono[:,None],2,axis=1).tobytes()
        def close(self): pass
    def desktop_on(enabled):
        if enabled:
            if server.mixer.desktop_subscription is None:
                server.mixer.desktop_subscription=server.mixer.hub.subscribe(Tone)
        else:
            if server.mixer.desktop_subscription:
                server.mixer.desktop_subscription.close()
            server.mixer.desktop_subscription=None
    monitors=[dict(id=i,name=f'Test monitor {i}',width=640,height=360,left=0,top=0) for i in (1,2)]
    with patch.object(server,'generate_frames',frames), patch.object(server,'desktop_frames',frames), patch.object(server,'start_camera',fake_camera), patch.object(server,'list_monitors',return_value=monitors), patch.object(server,'list_cameras',return_value=[0,1]), patch.object(server,'list_microphones',return_value=[]), patch.object(server.mixer,'set_desktop',side_effect=desktop_on):
        web=make_server('127.0.0.1',0,server.app,threaded=True)
        threading.Thread(target=web.serve_forever,daemon=True).start()
        try:
            yield web.server_port,password
        finally:
            stop.set()
            server.mixer.close()
            server.mixer.hub.close()
            web.shutdown();web.server_close()


def internet_check():
    topic='roomcam-check-'+secrets.token_hex(12)
    with synthetic_host() as (port,password):
        tunnel=Tunnel(port,topic,None,print,server.advertisement,server.session_id)
        server.tunnel=tunnel
        try:
            for attempt in range(2):
                url=tunnel.open()
                host=viewer.Host(url,'admin',password,topic)
                tunnel.publish()
                deadline=time.monotonic()+45
                found=None
                while time.monotonic()<deadline and found != url:
                    found=viewer.fetch_url_from_mailbox(topic,password,tunnel.session if attempt else None)
                    if found != url: time.sleep(5)
                if found!=url: raise RuntimeError('Signed discovery failed')
                deadline=time.monotonic()+60
                while True:
                    try:
                        host.pair()
                        break
                    except OSError:
                        if time.monotonic()>deadline: raise
                        time.sleep(3)
                if host.wait_until_reachable() is None: raise RuntimeError('Tunnel unreachable')
                tunnel.publish()
                host.api('/source/select?source=camera');read_video(host)
                host.api('/source/select?source=desktop&monitor=2');read_video(host)
                if not host.api('/desktop-audio/start'): raise RuntimeError('Synthetic audio failed')
                read_audio(host,997)
                tunnel.close_process()
                print('PASS: '+('replacement tunnel' if attempt else 'initial tunnel')+' signed discovery, camera, desktop and audio',flush=True)
        finally:
            tunnel.close();tunnel.close_process()


def local_check():
    directory=Path(sys.executable).parent if getattr(sys,'frozen',False) else Path(__file__).parent/'dist'
    with tempfile.TemporaryDirectory(prefix='roomcam-test-') as scratch:
        password=secrets.token_urlsafe(24)
        env=os.environ.copy();env.update(ROOMCAM_PASSWORD=password,ROOMCAM_CONFIG=str(Path(scratch)/'test.ini'))
        session=Path(scratch)/'session.json'
        process=subprocess.Popen([str(directory/'webcam_server.exe'),'--local','--port','0','--session-file',str(session),'--stop-after','100'],env=env)
        try:
            deadline=time.monotonic()+25
            while not session.exists():
                if process.poll() is not None or time.monotonic()>deadline: raise RuntimeError('Packaged host failed to start')
                time.sleep(.2)
            url=json.loads(session.read_text())['url']
            host=viewer.Host(url,'admin',password)
            host.pair()
            for monitor in host.api('/monitors')['monitors']:
                host.api(f'/source/select?source=desktop&monitor={monitor["id"]}')
                read_video(host)
            if not host.api('/desktop-audio/start'): raise RuntimeError('No speaker-loopback device')
            read_audio(host,997,play=True)
            for _ in range(3): read_audio(host)
            result=subprocess.run([str(directory/'viewer.exe'),'--url',url,'--source','desktop','--headless','--mute','--seconds','5'],env=env,capture_output=True,text=True,timeout=35)
            if result.returncode or 'frames=0 ' in result.stdout: raise RuntimeError(result.stdout+result.stderr)
            print('PASS: packaged viewer decoded desktop with muted playback',flush=True)
        finally:
            if process.poll() is None:
                subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],capture_output=True)
                process.wait(timeout=10)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--internet',action='store_true')
    parser.add_argument('--self-test',action='store_true')
    args=parser.parse_args()
    directory=Path(sys.executable).parent if getattr(sys,'frozen',False) else Path(__file__).parent/'.build'
    name='internet-results.txt' if args.internet else ('unit-results.txt' if args.self_test else 'local-results.txt')
    with (directory/name).open('w',encoding='utf-8',buffering=1) as report,contextlib.redirect_stdout(report),contextlib.redirect_stderr(report):
        try:
            if args.self_test:
                import unittest,test_combined
                result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromModule(test_combined))
                if not result.wasSuccessful(): raise SystemExit(1)
            elif args.internet: internet_check()
            else: local_check()
            print('PASS: combined media checks complete',flush=True)
        except Exception:
            import traceback
            traceback.print_exc()
            raise SystemExit(1)



if __name__=='__main__':
    main()
