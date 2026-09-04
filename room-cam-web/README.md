# Room Cam Web

Watch your webcam from **anywhere on the internet** — not just the same Wi-Fi.
A spin-off of [room-cam](../room-cam) that adds a public HTTPS tunnel (ngrok) on
top of the same timestamped MJPEG feed and password gate.

- **Camera is OFF until someone watches** (webcam light dark when idle).
- **Password-gated** (`admin` / `1337` by default — change it, see Security).
- **Any device, any network** — open the public URL in a browser.

## Setup

1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

2. Make a free ngrok account and grab your authtoken:
   https://dashboard.ngrok.com/signup

3. Register the token **once** (either way works):
   ```bash
   ngrok config add-authtoken <YOUR_TOKEN>
   ```
   …or set an environment variable named `NGROK_AUTHTOKEN`.

## Run

```bash
python webcam_server.py
```

It prints a **public URL** like `https://ab12cd34.ngrok-free.app`. Open that in
a browser on any device (your phone on cellular, a friend's laptop, anywhere),
log in with `admin` / `1337`, and you're watching.

> **ngrok free tier:** the first visit shows an ngrok warning page — click
> **Visit Site** once, then the camera's own login appears. The public URL
> changes every time you restart the server (a paid ngrok plan gives a fixed
> subdomain).

## Local-only mode

Set `ENABLE_TUNNEL = False` in `webcam_server.py` to skip ngrok and serve on
your LAN only (`http://<your-ip>:5000`), same as room-cam.

## ⚠️ Security — read this

This puts your webcam on the **public internet** behind a single password.

- **Change `PASSWORD`** in `webcam_server.py`. The default `1337` is a demo and
  is publicly known (it's in this repo), so anyone who finds your URL could try
  it. Use something long and private.
- The tunnel is **HTTPS**, so your password is encrypted in transit — good.
- The random ngrok URL gives a little obscurity, but obscurity is not security.
  The password is what actually protects you.
- Only run this while you actually want the cam reachable. Stop the server
  (Ctrl+C) when you're done — that also drops the public tunnel.

## Files

| File | Role |
|------|------|
| `webcam_server.py` | The server + ngrok tunnel. Run this on the machine with the webcam. |
| `viewer.py` | Optional desktop viewer. A browser is the easier client. |
| `requirements.txt` | `flask`, `opencv-python`, `pyngrok`. |
