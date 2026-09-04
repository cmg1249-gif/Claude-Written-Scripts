"""
Room Cam Web — optional desktop viewer.

For the web version, the easiest client is just a BROWSER: open the public
https URL the server prints, log in with admin / 1337, done. This script is a
bonus if you'd rather have a dedicated window.

Point PUBLIC_URL at the ngrok URL the server printed (or a http://IP:5000 on
the same network), then run:  python viewer.py

NOTE on ngrok's free tier: the first visit to an *.ngrok-free.app URL shows an
"interstitial" warning page a browser clicks through. OpenCV can't click it, so
this script may fail against a free-tier tunnel. If the feed won't open, use a
browser instead (open the URL, log in, watch) — that's the reliable path.
"""

import cv2

# ---- Set this to what the server printed ------------------------------------
PUBLIC_URL = "https://REPLACE-ME.ngrok-free.app"   # or http://192.168.x.x:5000
USERNAME = "admin"
PASSWORD = "1337"
# ---------------------------------------------------------------------------

# Build an authenticated stream URL:  https://user:pass@host/video
_scheme, _rest = PUBLIC_URL.split("://", 1)
STREAM_URL = f"{_scheme}://{USERNAME}:{PASSWORD}@{_rest.rstrip('/')}/video"


def main():
    print(f"Connecting to {PUBLIC_URL} ...")

    stream = cv2.VideoCapture(STREAM_URL)

    if not stream.isOpened():
        print("Couldn't open the stream. Check PUBLIC_URL / login, or just use")
        print("a browser: open the URL, log in, and watch there.")
        return

    print("Connected. Press 'q' in the window to quit.")
    while True:
        ok, frame = stream.read()
        if not ok:
            print("Stream ended or blocked (ngrok interstitial?). Try a browser.")
            break
        cv2.imshow("Room Cam Web", frame)
        if cv2.waitKey(1) & 0xFF == ord("q"):
            break

    stream.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
