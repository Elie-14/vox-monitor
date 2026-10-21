"""Alert dispatcher: per-channel retries, persistent de-duplication, dry-run."""
import logging
import time
from zoneinfo import ZoneInfo

from . import email_alerts, push_alerts, whatsapp_alerts
from .database import Database, utcnow
from .detection import Alert

log = logging.getLogger(__name__)
CAIRO = ZoneInfo("Africa/Cairo")


def cairo_now_str() -> str:
    return utcnow().astimezone(CAIRO).strftime("%A %d %B %Y, %H:%M:%S (Africa/Cairo, UTC%z)")


def channel_registry(cfg: dict):
    """(name, configured?, sender(alert)->None). WhatsApp only if enabled in config."""
    n = cfg.get("notifications", {})
    prio = lambda a: 5 if a.kind in ("critical", "test") else (4 if a.kind == "preliminary" else 3)
    chans = [
        ("email", n.get("email", True) and email_alerts.configured(),
         lambda a: email_alerts.send(a.title, format_email(a))),
        ("ntfy", n.get("ntfy", True) and push_alerts.ntfy_configured(),
         lambda a: push_alerts.send_ntfy(a.title, short_body(a), a.url, prio(a))),
        ("telegram", n.get("telegram", False) and push_alerts.telegram_configured(),
         lambda a: push_alerts.send_telegram(a.title, short_body(a), a.url)),
        ("whatsapp", n.get("whatsapp", False) and whatsapp_alerts.configured(),
         lambda a: whatsapp_alerts.send(a.title, short_body(a), a.url)),
    ]
    return chans


def short_body(a: Alert) -> str:
    return f"{a.body}\n{cairo_now_str()}"


def format_email(a: Alert) -> str:
    status = {"critical": "CONFIRMED", "preliminary": "PRELIMINARY (not yet confirmed)"}.get(a.kind, a.kind.upper())
    return (f"{a.title}\n\nStatus: {status}\nTime: {cairo_now_str()}\n\nWhy the monitor thinks so:\n{a.body}\n\n"
            f"Booking link (official VOX Egypt):\n{a.url or 'https://egy.voxcinemas.com/movies/whatson'}\n\n"
            "The monitor never buys tickets or logs in for you - open the link and book yourself.\n")


def dispatch(db: Database, alert: Alert, cfg: dict, dry_run: bool = False, sleep=time.sleep) -> dict[str, str]:
    """Send to every configured channel that has not already delivered this alert. Returns {channel: result}."""
    results: dict[str, str] = {}
    max_attempts = cfg.get("notifications", {}).get("max_attempts_per_channel", 10)
    for name, ok_to_use, sender in channel_registry(cfg):
        if not ok_to_use:
            results[name] = "not-configured"
            continue
        st = db.notification_status(alert.key, name)
        if st and st["status"] == "sent":
            results[name] = "already-sent"
            continue
        if st and st["attempts"] >= max_attempts:
            results[name] = "gave-up"
            continue
        if dry_run:
            log.info("[dry-run] would send %s alert '%s' via %s", alert.kind, alert.title, name)
            results[name] = "dry-run"
            continue
        last_err = None
        for attempt in range(3):
            try:
                sender(alert)
                db.record_notification(alert.key, name, True)
                results[name] = "sent"
                log.info("alert %s sent via %s", alert.key, name)
                break
            except Exception as exc:  # noqa: BLE001 - never let one channel block the others
                last_err = f"{type(exc).__name__}: {str(exc)[:150]}"
                sleep(min(2 ** attempt, 4))
        else:
            db.record_notification(alert.key, name, False, last_err)
            results[name] = f"failed ({last_err})"
            log.error("alert %s failed via %s: %s", alert.key, name, last_err)
    return results
