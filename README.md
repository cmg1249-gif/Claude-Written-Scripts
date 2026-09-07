# Claude Written Scripts

Small, self-contained LAN tools written with Claude Code. Each folder is its
own project with its own README. Windows binaries for each are attached to the
matching GitHub release (built with PyInstaller `--onefile`).

| Project | What it does | Release tag |
|---------|--------------|-------------|
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
cd ../room-cam-with-audio
pyinstaller --onefile --noconsole camera_server.py
pyinstaller --onefile viewer.py
```

`camera_server.exe` is built without a console so it can sit in the background
on the host; stop it from Task Manager. The other three keep a console because
they print status and take keyboard input.

## Security note

These are LAN-only tools with demo credentials (`admin` / `1337` in the Room
Cam scripts, none in Telephone). Change the password before relying on them,
and never expose them to the internet as-is.
