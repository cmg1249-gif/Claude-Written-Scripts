# Claude Written Scripts

Small, self-contained LAN tools written with Claude Code. Each folder is its
own project with its own README. Windows binaries for each are attached to the
matching GitHub release (built with PyInstaller `--onefile`).

| Project | What it does | Release tag |
|---------|--------------|-------------|
| [`room-cam/`](room-cam) | LAN webcam viewer with zero-config UDP auto-discovery. Camera stays off until a viewer connects; password-gated. | `room-cam-v2.0` |
| [`room-cam-web/`](room-cam-web) | Room Cam over the internet: tokenless Cloudflare quick tunnel + ntfy.sh rendezvous, promptable config. | `room-cam-web-v2.0` |
| [`room-cam-web-with-audio/`](room-cam-web-with-audio) | Room Cam Web plus the host mic over the same tunnel, synced, with mic on/off from the viewer or the web page. Promptable config, no tokens. | `room-cam-web-with-audio-v1.0.0` |
| [`telephone/`](telephone) | One-way live audio between two machines on the same network. Sender finds the receiver by LAN broadcast; no IP to type. | `telephone-v1.0.0` |
| [`room-cam-with-audio/`](room-cam-with-audio) | Room Cam v2 (auto-discovered LAN webcam viewer) plus the host microphone, streamed in sync, with a mic on/off key. | `room-cam-with-audio-v1.0.0` |

## Running from source

Every script lists its own `pip install` line in its docstring and README.
Python 3.12 was used for development and for the binaries.

## Building the binaries yourself

```bash
pip install pyinstaller
cd telephone
pyinstaller --onefile receiver.py
pyinstaller --onefile sender.py
cd ../room-cam
pyinstaller --onefile --noconsole camera_server.py
pyinstaller --onefile viewer.py
cd ../room-cam-web
pyinstaller --onefile --noconsole webcam_server.py
pyinstaller --onefile viewer.py
cd ../room-cam-web-with-audio
pyinstaller --onefile --noconsole --collect-all pycloudflared webcam_server.py
pyinstaller --onefile viewer.py
cd ../room-cam-with-audio
pyinstaller --onefile --noconsole camera_server.py
pyinstaller --onefile viewer.py
```

The host servers (`camera_server.exe`, `webcam_server.exe`) are built without a console so it can sit in the background
on the host; stop it from Task Manager. The other three keep a console because
they print status and take keyboard input.

## Security note

These are LAN-only tools with demo credentials (`admin` / `1337` in the Room
Cam scripts, none in Telephone). Change the password before relying on them,
and never expose them to the internet as-is.
