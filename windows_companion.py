"""Run on your Windows laptop: plays a loud sound + desktop notification when a critical alert arrives.
   python windows_companion.py            -> run quietly in the background (use pythonw.exe for no window)
   python windows_companion.py --test     -> play the sound and toast once
"""
import argparse
import logging
import os

from monitor.laptop_alerts import handle, listen_forever


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--wav", default=os.environ.get("ALERT_WAV"), help="optional .wav file for the alarm")
    ap.add_argument("--open-browser", action="store_true", help="also open the booking link")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    if a.test:
        handle({"title": "TEST: Avengers: Doomsday", "message": "Windows sound test", "click": "https://egy.voxcinemas.com/movies/avengers-doomsday"},
               a.wav, False)
        return
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        raise SystemExit("Set NTFY_TOPIC first (same value as on GitHub). See README step 11.")
    listen_forever(os.environ.get("NTFY_SERVER", "https://ntfy.sh"), topic, a.wav, a.open_browser)


if __name__ == "__main__":
    main()
