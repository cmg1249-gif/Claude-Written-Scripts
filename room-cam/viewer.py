"""
Room Cam — VIEWER (runs on any machine on the same network).

Fully automatic: it finds the host by UDP broadcast (no IP to enter), turns the
host camera on, shows the live feed, and mirrors the host's log lines here. It
keeps retrying discovery until the host appears, so you can launch the viewer
before the host and it will connect the moment the host comes online. When you
quit you can also turn the host camera off.

    pip install opencv-python
    python viewer.py

Keys (with the video window focused):
    q  = quit AND turn the host camera OFF
    l  = quit but LEAVE the host camera running
"""

import base64
import json
import socket
import threading
import time
import urllib.error
import urllib.request

import cv2

# ---- Must match camera_server.py -------------------------------------------
USERNAME = "admin"
PASSWORD = "1337"
DISCOVERY_PORT = 50505
DISCOVERY_REQUEST = b"ROOMCAM_DISCOVERY_V1"
DISCOVERY_REPLY_PREFIX = "ROOMCAM_HERE"
# ---------------------------------------------------------------------------

_AUTH_HEADER = "Basic " + base64.b64encode(
    f"{USERNAME}:{PASSWORD}".encode()
).decode()


def discover_host(timeout=5):
    """Broadcast a discovery ping and wait for the host to answer with its IP
    and stream port. Returns (ip, port) or None if nobody answered in time."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(timeout)
    try:
        sock.sendto(DISCOVERY_REQUEST, ("255.255.255.255", DISCOVERY_PORT))
        while True:
            data, addr = sock.recvfrom(1024)
            text = data.decode(errors="ignore").strip()
            if text.startswith(DISCOVERY_REPLY_PREFIX):
                parts = text.split(":")
                ip = parts[1] if len(parts) > 1 and parts[1] else addr[0]
                port = parts[2] if len(parts) > 2 else "5000"
                return ip, port
    except (socket.timeout, OSError):
        return None
    finally:
        sock.close()


def discover_host_retry(attempt_timeout=5):
    """Keep broadcasting until a host answers, then return (ip, port).

    Each attempt waits up to attempt_timeout seconds for a reply; if none
    arrives we just try again, indefinitely. This means the viewer can be
    started before the host -- it will connect the moment the host appears.
    Raises KeyboardInterrupt if the user gives up with Ctrl+C.
    """
    attempt = 0
    while True:
        attempt += 1
        found = discover_host(timeout=attempt_timeout)
        if found:
            return found
        # Don't spam: announce the first miss, then every ~30s of waiting.
        if attempt == 1 or attempt % 6 == 0:
            print("Still searching for the camera host... (Ctrl+C to stop)")
        time.sleep(1.0)


def api(base, path):
    """Call a host control endpoint (/start, /stop, /status, /logs)."""
    req = urllib.request.Request(
        f"{base}{path}", headers={"Authorization": _AUTH_HEADER}
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode())
    except Exception as exc:  # noqa: BLE001
        print(f"[viewer] control call {path} failed: {exc}")
        return None


def stream_host_logs(base, stop_event):
    """Poll the host's /logs in the background and print only NEW lines here."""
    shown = 0
    while not stop_event.is_set():
        data = api(base, "/logs")
        if data and "lines" in data:
            for line in data["lines"][shown:]:
                print(f"[host] {line}")
            shown = len(data["lines"])
        time.sleep(1.0)


def main():
    print("Searching the network for the camera host...")
    try:
        ip, port = discover_host_retry()
    except KeyboardInterrupt:
        print("\nStopped searching.")
        return

    base = f"http://{ip}:{port}"
    stream_url = f"http://{USERNAME}:{PASSWORD}@{ip}:{port}/video"
    print(f"Found the host at {ip}:{port}")

    status = api(base, "/status")
    if status is None:
        print("Found the host but couldn't reach its control API.")
        return
    if status.get("active"):
        print("Host camera is already ON.")
    else:
        print("Host camera is OFF -> turning it ON...")
        api(base, "/start")

    stop_event = threading.Event()
    threading.Thread(
        target=stream_host_logs, args=(base, stop_event), daemon=True
    ).start()

    stream = cv2.VideoCapture(stream_url)
    if not stream.isOpened():
        print("Couldn't open the video stream.")
        stop_event.set()
        return

    print("Live. Keys:  q = quit + shut camera off   |   l = quit, leave it on")
    shut_down = False
    while True:
        ok, frame = stream.read()
        if not ok:
            print("Stream ended.")
            break
        cv2.imshow("Room Cam (auto-discovered)", frame)
        key = cv2.waitKey(1) & 0xFF
        if key == ord("q"):
            shut_down = True
            break
        if key == ord("l"):
            break

    stop_event.set()
    stream.release()
    cv2.destroyAllWindows()
    if shut_down:
        print("Turning host camera OFF...")
        api(base, "/stop")
    print("Viewer closed.")


if __name__ == "__main__":
    main()
