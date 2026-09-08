# Ducky Cam Web with Audio

> ⚠️ **Proof of concept — for learning and authorized use only.** This is a
> personal project built to explore webcam/mic streaming, tunneling, and A/V
> sync. It is **not** hardened for production and must **never** be used to
> watch, listen to, or record anyone without their clear knowledge and consent.
> Only ever point it at a device **you own** or have explicit permission to use.
> Covert surveillance is illegal in most places and is not what this tool is
> for. Use it responsibly, on yourself and your own equipment.

[Ducky Cam Web](../ducky-cam-web) v2.0 plus the host's **microphone**. Watch and
listen from **anywhere on the internet** with **no accounts and no tokens** on
either side. Settings are promptable, so you never edit code.

- **Camera and mic are OFF until someone watches** (webcam light dark when idle).
- **Mic can be switched on/off** from the viewer (`m` key) or the web page.
- **Record to one file** from the viewer: an on-screen **REC** button (or `r`)
  saves synced audio+video next to the viewer as `roomcam_<timestamp>.mp4`.
- **Audio and video are synced** in `viewer.py`: every frame and audio chunk
  carries the host's clock; the viewer shows each frame when that instant's
  sound is playing.
- **Browser works too:** the page has a **Listen** button and a **Mic** button.
- **Tokenless the whole way:** Cloudflare quick tunnel + public ntfy.sh topic,
  exactly like Ducky Cam Web. Nothing to sign up for, no ngrok.
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
([ducky-cam-with-audio](../ducky-cam-with-audio)) runs at about 50 ms instead.

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
pip install opencv-python sounddevice numpy imageio-ffmpeg
python viewer.py
```
…or run `viewer.exe`. It reads the mailbox, finds the host, asks for the host
password once (or reads it from a `roomcam_config.ini` beside it, or the
`ROOMCAM_PASSWORD` env var), then shows video and plays audio in sync.

Keys (with the video window focused):
- `r` — start/stop **recording** (same as the on-screen REC button)
- `m` — toggle the host **mic** on/off
- `c` — switch to the host's next **camera**
- `n` — switch to the host's next **microphone**
- `q` — quit **and** turn the host camera + mic off
- `l` — quit but leave them running

The web page has dropdowns for the same two things.

## Recording

Click the **REC** button in the top-left of the video (or press `r`) to start;
click again (or `r`) to stop. Each recording is written as **one file** —
synced audio + video — into the folder the viewer runs from
(`roomcam_YYYYMMDD_HHMMSS.mp4` by default), named by the time it started. A red
dot and a running timer show while it's recording, and a still-recording session
is finished off cleanly when you quit.

The two streams are merged with **ffmpeg**, supplied by the `imageio-ffmpeg`
package (bundled into `viewer.exe`), so nothing extra needs installing. If no
ffmpeg can be found, the audio and video are kept as two separate files instead
of one, so a recording is never lost.

Pick the container with `--format`: `mp4` (default, H.264 + AAC), `mkv` (same
codecs), or `avi` (MJPEG + PCM — larger, but no re-encoding of the video).
Video length is tied to the audio, which is the same master clock playback uses,
so the recording stays in sync even when the tunnel delivers frames unevenly.

If you switch the host to a mic with a **different sample rate** (`n`) while
recording, the current file is finished off and a fresh one is started at the
new rate automatically — the recording carries on, split into two files at the
switch, each in sync.

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
| `video_fps` | `30` | **ceiling** for frames per second. At 640x480 quality 80 that is about 9 Mbit/s of upload |
| `jpeg_quality` | `80` | 1–100 ceiling. Higher = sharper and heavier |
| `video_width` | `640` | frames are scaled down to this width before sending; `0` = no scaling |
| `capture_width` / `capture_height` | `0` | ask the camera for a specific size; `0` = the camera's default |
| `adaptive` | `yes` | back off automatically when the machine or the link cannot keep up |

**The fps and quality settings are a ceiling, not a demand.** With `adaptive`
on, the host measures what it is actually delivering every few seconds and
steps down when the camera, the CPU or the uplink cannot keep up, then climbs
back when there is room. That means the same settings work on a slow laptop
and a fast desktop without touching the config. Set `adaptive = no` to pin the
numbers exactly.

Capturing at a higher resolution is possible but expensive: 640x480 costs
roughly 9 Mbit/s at 30 fps, while 1280x720 costs about 30 Mbit/s, which is
more than most home uploads. That is why the capture size is left alone by
default and only the send size is scaled.

The viewer reads `topic`, `username`, `password` from the same file or env vars.

## Viewer options

| Flag | What it does |
|------|--------------|
| `--browser` | open the web page instead of the synced viewer |
| `--url URL` | skip the mailbox and connect straight to a URL |
| `--seconds N` | quit automatically after N seconds |
| `--record out.wav` | save the whole session's received audio to a WAV file |
| `--format mp4\|mkv\|avi` | container for REC-button recordings (default `mp4`) |
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
| `GET /devices` | cameras and microphones the host can see |
| `POST /camera/select?index=N` | switch camera |
| `POST /mic/select?index=N` | switch microphone (`-1` = system default) |
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
- **Missing camera or microphone.** Either one alone is fine. A host with no
  camera still streams audio, and `/video` answers 503 rather than hanging. A
  host with no microphone still streams video. Picking a device that fails to
  open leaves that half switched off, and picking a working one turns it back
  on.
- **No audio, video fine.** Check the host log for "Mic could not start" and
  set `mic_device` in `roomcam_config.ini` to the right index.

## ⚠️ Security — read this

This is a **proof of concept**, and it puts your webcam **and microphone** on
the public internet behind a single password. Use it only on hardware you own
or are authorized to use, and only with the knowledge and consent of anyone it
can see or hear. Do not use it to surveil people — that is illegal in most
places and is not the point of this project.

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
| `requirements.txt` | `flask`, `opencv-python`, `pycloudflared`, `sounddevice`, `numpy`, `imageio-ffmpeg`. |
