"""Windows companion core: listens to the free ntfy.sh topic and plays a loud sound + toast on critical alerts."""
import json
import logging
import os
import time
import webbrowser
from urllib.parse import urlparse

import requests

log = logging.getLogger("laptop")
ALLOWED_HOSTS = ("egy.voxcinemas.com", "voxcinemas.com", "linktr.ee")
STATE_FILE = "companion_state.json"


def safe_url(url: str | None) -> str | None:
    """Only open official https VOX links."""
    if not url:
        return None
    p = urlparse(url)
    return url if p.scheme == "https" and (p.hostname or "").endswith(ALLOWED_HOSTS) else None


def load_state(path: str = STATE_FILE) -> dict:
    try:
        return json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        return {"since": int(time.time()), "played": []}


def save_state(st: dict, path: str = STATE_FILE) -> None:
    st["played"] = st["played"][-200:]
    json.dump(st, open(path, "w", encoding="utf-8"))


def should_play(msg: dict, st: dict, min_priority: int = 4) -> bool:
    """Play only real messages with high priority that were not played before (no repeats)."""
    if msg.get("event") != "message" or msg.get("priority", 3) < min_priority:
        return False
    return msg.get("id") not in st["played"]


def play_sound(wav_path: str | None = None, seconds: int = 10) -> None:
    try:
        import winsound
    except ImportError:
        print("\a[sound only works on Windows]")
        return
    end = time.time() + seconds
    while time.time() < end:
        if wav_path and os.path.exists(wav_path):
            winsound.PlaySound(wav_path, winsound.SND_FILENAME)
        else:
            for f in (880, 1175, 880, 1175):
                winsound.Beep(f, 250)
        time.sleep(0.3)


def toast(title: str, message: str, url: str | None) -> None:
    try:
        from winotify import Notification, audio
        n = Notification(app_id="VOX Monitor", title=title, msg=message, duration="long")
        n.set_audio(audio.LoopingAlarm, loop=False)
        if url:
            n.add_actions(label="Open booking", launch=url)
        n.show()
    except Exception as exc:  # noqa: BLE001
        log.warning("desktop toast unavailable (%s); sound still plays", exc)


def handle(msg: dict, wav: str | None, open_browser: bool) -> None:
    title, body = msg.get("title", "VOX alert"), msg.get("message", "")
    url = safe_url(msg.get("click"))
    log.info("ALERT: %s", title)
    toast(title, f"{body}\n{url or ''}"[:250], url)
    if open_browser and url:
        webbrowser.open(url)
    play_sound(wav)


def listen_forever(server: str, topic: str, wav: str | None = None, open_browser: bool = False,
                   state_path: str = STATE_FILE, max_events: int | None = None) -> None:
    st = load_state(state_path)
    backoff, seen = 2, 0
    while True:
        try:
            with requests.get(f"{server.rstrip('/')}/{topic}/json", params={"since": st["since"]}, stream=True, timeout=(10, 120)) as r:
                r.raise_for_status()
                backoff = 2
                for line in r.iter_lines():
                    if not line:
                        continue
                    msg = json.loads(line)
                    if msg.get("time"):
                        st["since"] = max(st["since"], int(msg["time"]))
                    if should_play(msg, st):
                        st["played"].append(msg["id"])
                        handle(msg, wav, open_browser)
                        seen += 1
                    save_state(st, state_path)
                    if max_events and seen >= max_events:
                        return
        except (requests.RequestException, ValueError) as exc:
            log.warning("connection lost (%s); retrying in %ss", type(exc).__name__, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 120)
