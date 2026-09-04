"""
Room Cam Web — LISTENER (runs on your laptop).

Fully automatic and tokenless: it reads the public ntfy.sh mailbox to find the
server's current public URL, then opens the live feed in your browser. No IPs,
no tokens, no copying URLs.

    python viewer.py          (uses only the Python standard library)

Log in with the username / password you set on the host (default admin / 1337).

The ntfy topic must match the host. You don't edit code to change it: set the
ROOMCAM_TOPIC env var, or drop a roomcam_config.ini next to this file with a
[roomcam] section and a topic = ... line (same file the host writes).
"""

import configparser
import json
import os
import sys
import urllib.request
import webbrowser

# ---- Topic resolution: env var -> roomcam_config.ini -> default ------------
DEFAULT_TOPIC = "roomcam-relay-3f9k2m7qx4"
CONFIG_FILENAME = "roomcam_config.ini"
CONFIG_SECTION = "roomcam"


def load_topic():
    """Find the ntfy rendezvous topic without any code edits: environment
    variable first, then a roomcam_config.ini beside this file, else default."""
    env = os.environ.get("ROOMCAM_TOPIC")
    if env:
        return env
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(base, CONFIG_FILENAME)
    if os.path.exists(path):
        cfg = configparser.ConfigParser()
        cfg.read(path)
        if cfg.has_option(CONFIG_SECTION, "topic") and cfg.get(CONFIG_SECTION, "topic"):
            return cfg.get(CONFIG_SECTION, "topic")
    return DEFAULT_TOPIC


NTFY_TOPIC = load_topic()
# ---------------------------------------------------------------------------


def fetch_url_from_mailbox():
    """Read the most recent public URL the server posted to the ntfy topic.

    ntfy's ?poll=1 returns cached messages as newline-delimited JSON; we take
    the newest 'message' event that looks like a URL. No token needed.
    """
    req = urllib.request.Request(
        f"https://ntfy.sh/{NTFY_TOPIC}/json?poll=1&since=12h",
        headers={"User-Agent": "room-cam-web"},
    )
    latest = None
    latest_time = -1
    with urllib.request.urlopen(req, timeout=15) as resp:
        for line in resp.read().decode().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if obj.get("event") != "message":
                continue
            msg = (obj.get("message") or "").strip()
            when = obj.get("time", 0)
            if msg.startswith("http") and when >= latest_time:
                latest_time = when
                latest = msg
    return latest


def main():
    print("Reading the rendezvous mailbox (ntfy)...")
    try:
        url = fetch_url_from_mailbox()
    except Exception as exc:  # noqa: BLE001
        print(f"Couldn't read the mailbox: {exc}")
        return

    if not url:
        print("No URL in the mailbox yet.")
        print("Start webcam_server.py on the host, wait a few seconds, re-run this.")
        return

    print(f"Server is live at: {url}")
    print("Opening it in your browser.")
    print("Log in with the username/password you set on the host "
          "(default admin / 1337).")
    print("(Cloudflare may show a brief connecting page first; that's normal.)")
    webbrowser.open(url)


if __name__ == "__main__":
    main()
