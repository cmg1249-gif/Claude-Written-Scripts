"""
Room Cam Web — LISTENER (runs on your laptop).

Fully automatic: it reads the rendezvous mailbox (a GitHub gist) to find the
server's current public URL, then opens the live feed in your browser. No IP
addresses, no copying URLs — the server publishes where it is, this finds it.

    python viewer.py          (uses only the Python standard library)

Log in with admin / 1337 when the browser asks.
"""

import json
import time
import urllib.request
import webbrowser

# ---- Must match webcam_server.py -------------------------------------------
GIST_ID = "51afc120ecb7715badd3c0ac391d8bdc"
GIST_FILENAME = "roomcam_url.txt"
USERNAME = "admin"
PASSWORD = "1337"
# ---------------------------------------------------------------------------


def fetch_url_from_gist():
    """Read the current public URL the server published into the gist mailbox.

    Uses the public GitHub API by gist ID — no token needed, even for a secret
    gist, and it isn't CDN-cached so we always get the latest URL.
    """
    # The ?_=<ms> cache-buster gives the CDN a unique URL each time, so we get
    # the freshly published value instead of a cached one.
    req = urllib.request.Request(
        f"https://api.github.com/gists/{GIST_ID}?_={int(time.time() * 1000)}",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "room-cam-web",
            "Cache-Control": "no-cache",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode())
    files = data.get("files", {})
    if GIST_FILENAME not in files:
        return None
    return (files[GIST_FILENAME]["content"] or "").strip()


def main():
    print("Reading the rendezvous mailbox...")
    try:
        url = fetch_url_from_gist()
    except Exception as exc:  # noqa: BLE001
        print(f"Couldn't read the mailbox: {exc}")
        return

    if not url or url == "pending":
        print("The server hasn't published a URL yet.")
        print("Start webcam_server.py on the host, wait a few seconds, re-run this.")
        return

    print(f"Server is live at: {url}")
    print(f"Opening it in your browser — log in with {USERNAME} / {PASSWORD}.")
    print("(ngrok's free tier shows a 'Visit Site' page first; click through once.)")
    webbrowser.open(url)


if __name__ == "__main__":
    main()
