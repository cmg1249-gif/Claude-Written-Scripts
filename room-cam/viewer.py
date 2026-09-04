"""
Room Cam — VIEWER (runs on your laptop).

Connects to the always-on camera_server on the host. On launch it turns the
host camera ON if it's off, shows the live feed in a window here, and streams
the host's log lines to your laptop (prefixed [host]). When you quit you can
also turn the host camera OFF.

Setup:
    1. Leave camera_server.py running on the host machine.
    2. Put its IP into HOST_IP below (and matching USERNAME / PASSWORD).
    3. On your laptop:  pip install opencv-python
    4. Run this file:   python viewer.py

Keys (with the video window focused):
    q  = quit AND turn the host camera OFF (shut it down)
    l  = quit but LEAVE the host camera running (for other viewers)
"""

import base64
import json
import threading
import time
import urllib.error
import urllib.request

import cv2

# ---- Change these to match camera_server.py --------------------------------
HOST_IP = "192.168.1.42"        # the IP camera_server.py printed
PORT = 5000
USERNAME = "admin"              # must match USERNAME in camera_server.py
PASSWORD = "1337"              # must match PASSWORD in camera_server.py
# ---------------------------------------------------------------------------

BASE = f"http://{HOST_IP}:{PORT}"
# The username:password@ form sends the login along with the video request.
STREAM_URL = f"http://{USERNAME}:{PASSWORD}@{HOST_IP}:{PORT}/video"
_AUTH_HEADER = "Basic " + base64.b64encode(
    f"{USERNAME}:{PASSWORD}".encode()
).decode()


def api(path):
    """Call a host control endpoint (/start, /stop, /status, /logs).

    Returns the parsed JSON dict, or None if the host couldn't be reached.
    """
    req = urllib.request.Request(
        f"{BASE}{path}", headers={"Authorization": _AUTH_HEADER}
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode())
    except Exception as exc:  # noqa: BLE001 - any failure means "host not reachable"
        print(f"[viewer] control call {path} failed: {exc}")
        return None


def stream_host_logs(stop_event):
    """Poll the host's /logs in the background and print only NEW lines here."""
    shown = 0
    while not stop_event.is_set():
        data = api("/logs")
        if data and "lines" in data:
            lines = data["lines"]
            for line in lines[shown:]:
                print(f"[host] {line}")
            shown = len(lines)
        time.sleep(1.0)


def main():
    print(f"Connecting to {BASE} ...")

    status = api("/status")
    if status is None:
        print("Could not reach the host. Is camera_server.py running, and do")
        print("HOST_IP / USERNAME / PASSWORD match? Same Wi-Fi network?")
        return

    if status.get("active"):
        print("Host camera is already ON.")
    else:
        print("Host camera is OFF -> turning it ON...")
        api("/start")

    # Background thread: mirror the host's log lines onto this laptop.
    stop_event = threading.Event()
    log_thread = threading.Thread(
        target=stream_host_logs, args=(stop_event,), daemon=True
    )
    log_thread.start()

    stream = cv2.VideoCapture(STREAM_URL)
    if not stream.isOpened():
        print("Reached the host but couldn't open the video stream.")
        stop_event.set()
        return

    print("Live. Keys:  q = quit + shut camera off   |   l = quit, leave it on")

    shut_down_on_exit = False
    while True:
        success, frame = stream.read()
        if not success:
            print("Stream ended (camera turned off, or host stopped).")
            break

        cv2.imshow("Room Cam (laptop viewer)", frame)

        key = cv2.waitKey(1) & 0xFF
        if key == ord("q"):
            shut_down_on_exit = True
            break
        if key == ord("l"):
            break

    # Clean up the viewer side.
    stop_event.set()
    stream.release()
    cv2.destroyAllWindows()

    if shut_down_on_exit:
        print("Turning host camera OFF...")
        api("/stop")

    print("Viewer closed.")


if __name__ == "__main__":
    main()
