# Room Cam with Audio

[Room Cam](../room-cam) v2, plus the host's **microphone**. Watch and listen
from another device on the **same network** with **zero config**. The viewer
finds the host by LAN broadcast; there's no IP to type.

- **Camera and mic are OFF until someone watches** (webcam light dark when idle).
- **Mic can be toggled on/off** from the viewer with the `m` key.
- **Audio and video are synced**: every frame and audio chunk carries the
  host's clock, and the viewer shows each frame when the sound from that
  instant is coming out of the speakers.
- **Password-gated** (`admin` / `1337` by default; change it).
- Same discovery, same keys, same endpoints as Room Cam, plus the mic ones.

## How it works

```
laptop: broadcasts "who is the room cam?"  ->  host replies with its IP + port
laptop: turns camera + mic on, opens /video and /audio
host:   /video = MJPEG, each frame stamped with the host clock
        /audio = raw 44.1 kHz mono PCM chunks, each stamped with the host clock
laptop: audio plays through a small jitter buffer; video waits for the audio
        to catch up to each frame's timestamp, then shows it
```

Both streams go over the same HTTP server and the same password, so only the
host needs a firewall opening (port 5000, same as Room Cam).

## Run the host

```bash
pip install opencv-python flask sounddevice numpy
python camera_server.py
```

## Watch and listen from another device (same network)

```bash
pip install opencv-python sounddevice numpy
python viewer.py
```

Keys (with the video window focused):
- `m` — toggle the host **mic** on/off
- `q` — quit **and** turn the host camera + mic off
- `l` — quit but leave the host camera + mic running

Every 5 seconds the viewer prints frames received, audio chunks, drops,
underruns, buffer depth, and the average audio/video offset in ms.

## Host endpoints

| Endpoint | What it does |
|----------|--------------|
| `GET /video` | MJPEG stream (turns camera on) |
| `GET /audio` | PCM audio stream (does **not** turn the mic on by itself) |
| `POST /start` / `POST /stop` | camera on / off |
| `POST /mic/start` / `POST /mic/stop` | mic on / off |
| `GET /status` | `{"active": bool, "mic": bool}` |
| `GET /logs` | host log lines |

## Viewer options

| Flag | What it does |
|------|--------------|
| `--ip A.B.C.D` | connect straight to the host, skipping discovery |
| `--port N` | host's port if you changed it (default 5000) |
| `--seconds N` | quit automatically after N seconds |
| `--record out.wav` | save the received audio to a WAV file |
| `--device N` | pick a speaker (`python -m sounddevice` lists them) |

## Tuning (constants at the top of the files)

- `MIC_DEVICE` (host): which microphone; `None` = default.
- `MIC_ON_AT_CONNECT` (viewer): `True` = mic comes on with the camera.
- `PREBUFFER_SECONDS` / `MAX_BUFFER_SECONDS` (viewer): audio jitter buffer.
  Bigger = smoother on bad Wi-Fi, but more lag.

## Troubleshooting

- **The viewer never finds the host.** Some networks drop broadcast traffic
  between devices; guest Wi-Fi and "client isolation" on the access point are
  the usual culprits. A laptop with VirtualBox, VPN, WSL or Hyper-V adapters
  can also send the broadcast out a virtual adapter instead of the real one.
  Skip discovery and name the host directly:

  ```
  viewer.exe --ip 192.168.0.77
  ```

  The host prints its own address when it starts.
- **Found the host but can't reach it.** Windows Firewall needs an inbound
  rule for `camera_server.exe` on the network profile in use. The rule is tied
  to the exe's **path**, so a fresh download or a moved folder needs a new rule
  even though the old one is still listed.
- **Both machines must be on the same subnet.** The laptop's address should
  start with the same three numbers as the host's.

## ⚠️ Security

- **LAN-only.** Nothing is exposed to the internet.
- **Change `PASSWORD`** in `camera_server.py` and `viewer.py`. `1337` is a
  public demo value.
- Discovery is unauthenticated but only reveals the host's LAN IP. Video and
  audio are both password-protected.

## Files

| File | Role |
|------|------|
| `camera_server.py` | Host: answers discovery, streams camera + mic, on/off controls. |
| `viewer.py` | Any device on the LAN: finds the host, shows video, plays audio in sync. |
