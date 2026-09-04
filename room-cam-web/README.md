# Room Cam Web

Watch your webcam from **anywhere on the internet** — with **no accounts and no
tokens** on either side. Settings are promptable, so you never edit code.

- **Camera is OFF until someone watches** (webcam light dark when idle).
- **Password-gated** (`admin` / `1337` by default — change it, see Security).
- **Tokenless the whole way:** a Cloudflare quick tunnel gives a public URL with
  no login; the host posts that URL to a public ntfy.sh topic; the listener
  reads the topic. Nothing to sign up for.

## How it works

```
host: opens Cloudflare quick tunnel  ->  posts URL to ntfy.sh/<topic>
laptop: reads ntfy.sh/<topic>  ->  opens the feed in your browser
```

All settings are **promptable — you never edit code.** On its first run the host
asks you to set a password and writes a `roomcam_config.ini` next to it; after
that it reads that file, and `ROOMCAM_*` environment variables override it. The
camera password is the real security gate.

## Run the host

```bash
pip install -r requirements.txt
python webcam_server.py
```
…or just double-click `webcam_server.exe` (standalone, no Python needed). It
runs silently, opens the tunnel, and posts its URL. Stop it via Task Manager.

**First run:** you're asked to set a password — a prompt in the terminal, or a
small pop-up for the double-click exe. It's saved to `roomcam_config.ini` beside
the server; edit that file (not the code) to change anything later. The first
run also downloads the small `cloudflared` helper automatically.

## Watch from your laptop (any network)

```bash
python viewer.py
```
It reads the mailbox, finds the server, and opens the feed in your browser. Log
in with the username/password you set on the host.

If you changed the **topic** on the host, point the listener at the same one
(no code edits): set the `ROOMCAM_TOPIC` env var, or drop a matching
`roomcam_config.ini` beside `viewer.py`.

## Local-only mode

Set `ENABLE_TUNNEL = False` in `webcam_server.py` to skip the tunnel and serve
on your LAN only (`http://<your-ip>:5000`).

## ⚠️ Security — read this

This puts your webcam on the **public internet** behind a single password.

- **Set a real password** at the first-run prompt (or in `roomcam_config.ini`,
  or via the `ROOMCAM_PASSWORD` env var). The default `1337` is a public demo
  value. The password is **no longer compiled into the exe** — it lives in the
  config file, so you change it without rebuilding.
- The **ntfy topic is public** — anyone who knows it can read the current tunnel
  URL. That's fine *because* the password gates the feed. Don't rely on the
  topic being secret.
- Cloudflare quick tunnels are **free/best-effort** (great for a personal cam,
  not a uptime guarantee). The public URL changes each run — the mailbox handles
  that automatically.
- Stop the host when you're done; that drops the public tunnel.

## Files

| File | Role |
|------|------|
| `webcam_server.py` | Host: tunnel + ntfy publish + stream. No tokens. |
| `viewer.py` | Laptop: reads ntfy, opens the feed. Stdlib only. |
| `roomcam_config.ini` | Auto-created on first run; holds your password + topic. **Not committed.** |
| `requirements.txt` | `flask`, `opencv-python`, `pycloudflared`. |
