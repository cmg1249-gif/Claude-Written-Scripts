# Room Cam Web with Audio

[Room Cam Web](../room-cam-web) v2.0 plus the host's **microphone**. Watch and
listen from **anywhere on the internet** with **no accounts and no tokens** on
either side. Settings are promptable, so you never edit code.

- **Camera and mic are OFF until someone watches** (webcam light dark when idle).
- **Mic can be switched on/off** from the viewer (`m` key) or the web page.
- **Audio and video are synced** in `viewer.py`: every frame and audio chunk
  carries the host's clock; the viewer shows each frame when that instant's
  sound is playing.
- **Browser works too:** the page has a **Listen** button and a **Mic** button.
- **Tokenless the whole way:** Cloudflare quick tunnel + public ntfy.sh topic,
  exactly like Room Cam Web. Nothing to sign up for, no ngrok.
- **Password-gated** (`admin` / `1337` by default; you're asked to change it).

## How it works

```
host:   opens Cloudflare quick tunnel  ->  posts URL to ntfy.sh/<topic>
laptop: reads ntfy.sh/<topic>  ->  connects to /video + /audio with your password
host:   /video = MJPEG, each frame stamped with the host clock
        /audio = 16 kHz mono PCM chunks, each stamped with the host clock
laptop: audio plays through a jitter buffer; video waits for the audio to
        reach each frame's timestamp, then shows it
```

Audio is 16 kHz mono (voice quality, about 256 kbit/s upload from the host).

**Expect 1–3 seconds of delay.** Cloudflare quick tunnels deliver the audio in
bursts and sometimes stall for a second or more, so the viewer holds about a
second of audio before playing and raises that by itself if the link is worse.
Video is held back to match, so lips and sound still line up. The LAN version
([room-cam-with-audio](../room-cam-with-audio)) runs at about 50 ms instead.

## Run the host

```bash
pip install -r requirements.txt
python webcam_server.py
```
…or double-click `webcam_server.exe`. It runs silently, opens the tunnel, posts
its URL. Stop it via Task Manager.

**First run:** you're asked to set a password (terminal prompt, or a pop-up for
the exe). It's saved to `roomcam_config.ini` beside the server. Edit that file
to change anything later. Nothing lives in the code.

## Watch and listen from your laptop (any network)

```bash
pip install opencv-python sounddevice numpy
python viewer.py
```
…or run `viewer.exe`. It reads the mailbox, finds the host, asks for the host
password once (or reads it from a `roomcam_config.ini` beside it, or the
`ROOMCAM_PASSWORD` env var), then shows video and plays audio in sync.

Keys (with the video window focused):
- `m` — toggle the host **mic** on/off
- `q` — quit **and** turn the host camera + mic off
- `l` — quit but leave them running

`python viewer.py --browser` opens the page in your browser instead (the old
v2.0 behaviour). Use the **Listen** and **Mic** buttons there.

## Settings (`roomcam_config.ini`, or `ROOMCAM_*` env vars)

| Key | Default | Meaning |
|-----|---------|---------|
| `username` / `password` | `admin` / `1337` | the login. Set a real password. |
| `topic` | `roomcam-audio-relay-…` | ntfy rendezvous topic (host and viewer must match) |
| `port` | `5000` | local web server port |
| `camera_index` | `0` | which webcam |
| `mic_device` | *(blank = default)* | which microphone (`python -m sounddevice` lists them) |
| `audio_rate` | `16000` | mic sample rate; falls back automatically if the mic can't do it |
| `tunnel` | `yes` | `no` = LAN only, skip Cloudflare + ntfy |
| `video_fps` | `20` | frames per second sent. 20 fps / quality 70 / 640 px is about 3 Mbit/s of upload |
| `jpeg_quality` | `70` | 1–100. Higher = sharper and heavier |
| `video_width` | `640` | frames are scaled down to this width; `0` = camera's native size |

**Video must fit your upload speed.** If it doesn't, frames queue up in the
tunnel and the picture runs seconds behind the sound (the viewer delays the
audio to compensate, but it's still lag). Lower `video_fps` or `jpeg_quality`
if the viewer keeps printing "video is … behind the audio".

The viewer reads `topic`, `username`, `password` from the same file or env vars.

## Viewer options

| Flag | What it does |
|------|--------------|
| `--browser` | open the web page instead of the synced viewer |
| `--url URL` | skip the mailbox and connect straight to a URL |
| `--seconds N` | quit automatically after N seconds |
| `--record out.wav` | save received audio to a WAV file |
| `--device N` | pick a speaker |

## Host endpoints

| Endpoint | What it does |
|----------|--------------|
| `GET /` | web page with live video, Listen and Mic buttons |
| `GET /video` | MJPEG stream (turns camera on) |
| `GET /audio` | PCM audio stream (does **not** turn the mic on by itself) |
| `POST /start` / `POST /stop` | camera on / off |
| `POST /mic/start` / `POST /mic/stop` | mic on / off |
| `GET /status` | `{"active", "mic", "sample_rate", "channels"}` |
| `GET /logs` | host log lines |

## Troubleshooting

- **"Host not reachable yet" keeps repeating.** A fresh Cloudflare tunnel can
  take a few seconds to start routing; the viewer retries for a minute.
- **The viewer prints "your DNS could not resolve … using Cloudflare DNS
  instead."** Some home routers and ISP filters refuse to resolve brand-new
  `*.trycloudflare.com` names. The viewer works around it automatically. A
  **browser** on that same network will fail to open the page, though; either
  use `viewer.py`, use a phone on cellular, or set the PC's DNS to `1.1.1.1`.
- **The host asks for a password, then the window just closes.** Fixed in
  v1.0.1. Older builds crashed if the password contained a `%`. If a host still
  closes on startup, look for `roomcam_error.log` next to the exe; from v1.0.1
  on, every startup failure is written there and shown in a dialog.
- **Forgot the password, or want to start over.** Delete `roomcam_config.ini`
  next to the exe. The next run asks again.
- **No audio, video fine.** Check the host log for "Mic could not start" and
  set `mic_device` in `roomcam_config.ini` to the right index.

## ⚠️ Security — read this

This puts your webcam **and microphone** on the public internet behind a
single password.

- **Set a real password** at the first-run prompt. `1337` is a public demo value.
- The **ntfy topic is public**. Anyone who knows it can read the current tunnel
  URL. The password is what gates the feed.
- Cloudflare quick tunnels are free and best-effort. The URL changes each run;
  the mailbox handles that.
- Stop the host when you're done; that drops the tunnel.

## Files

| File | Role |
|------|------|
| `webcam_server.py` | Host: tunnel + ntfy publish + video + audio + mic on/off. |
| `viewer.py` | Laptop: reads ntfy, shows video, plays audio in sync. |
| `roomcam_config.ini` | Auto-created on first run; holds your password + topic. **Not committed.** |
| `requirements.txt` | `flask`, `opencv-python`, `pycloudflared`, `sounddevice`, `numpy`. |
