"""Free push channels: ntfy.sh (phone app + Windows companion) and Telegram."""
import os

import requests


def ntfy_configured() -> bool:
    return bool(os.environ.get("NTFY_TOPIC"))


def send_ntfy(title: str, message: str, url: str | None, priority: int) -> None:
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    payload = {"topic": os.environ["NTFY_TOPIC"], "title": title, "message": message,
               "priority": priority, "tags": ["rotating_light" if priority >= 5 else "movie_camera"]}
    if url:
        payload["click"] = url
        payload["actions"] = [{"action": "view", "label": "Open booking", "url": url}]
    r = requests.post(server, json=payload, timeout=20)
    r.raise_for_status()


def telegram_configured() -> bool:
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))


def send_telegram(title: str, message: str, url: str | None) -> None:
    token, chat = os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
    text = f"{title}\n\n{message}" + (f"\n\n{url}" if url else "")
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat, "text": text}, timeout=20)
    r.raise_for_status()
