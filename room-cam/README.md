# Room Cam

Watch your webcam from another device on the **same network** — with **zero
config**. The viewer finds the host automatically by LAN broadcast; there's no
IP to type in.

- **Camera is OFF until someone watches** (webcam light dark when idle).
- **Password-gated** (`admin` / `1337` by default — change it, see Security).
- **No config, no tokens:** pure local UDP discovery. Run and forget.

## How it works

```
laptop: broadcasts "who is the room cam?"  ->  host replies with its IP + port
laptop: connects to the stream  ->  turns the camera on, shows the feed
```

Everything is a constant at the top of the `.py` files, so there's nothing to
edit on either machine.

## Run the host

```bash
pip install opencv-python flask
python camera_server.py
```
…or just double-click `camera_server.exe` (standalone, no Python needed). It
runs silently and answers discovery pings. Stop it via Task Manager.

## Watch from another device (same network)

```bash
pip install opencv-python
python viewer.py
```
It finds the host, turns the camera on, and shows the live feed in a window.

Keys (with the video window focused):
- `q` — quit **and** turn the host camera off
- `l` — quit but leave the host camera running

## ⚠️ Security

- This is **LAN-only** — it does not expose anything to the internet. For that,
  see [`room-cam-web`](../room-cam-web).
- **Change `PASSWORD`** in `camera_server.py`. `1337` is a public demo value; in
  the exe it's compiled in, so changing it means rebuilding.
- Discovery is unauthenticated but only reveals the host's LAN IP — the stream
  itself is still password-protected.

## Files

| File | Role |
|------|------|
| `camera_server.py` | Host: answers discovery + streams. Runs on the machine with the webcam. |
| `viewer.py` | Any device on the LAN: finds the host and shows the feed. |
