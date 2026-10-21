"""Structured logging with automatic secret redaction."""
import logging
import os
import sys
import time

SECRET_ENV_NAMES = [
    "SMTP_PASSWORD", "NTFY_TOPIC", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
    "WHATSAPP_TOKEN", "HEALTHCHECK_PING_URL", "ALERT_EMAIL_TO", "WHATSAPP_TO",
]


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        for name in SECRET_ENV_NAMES:
            val = os.environ.get(name, "")
            if val and len(val) >= 4 and val in msg:
                msg = msg.replace(val, "***")
        record.msg = msg
        record.args = ()
        return True


def setup_logging(level: str = "INFO", log_file: str | None = None) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    fmt = logging.Formatter("%(asctime)sZ %(levelname)s %(name)s: %(message)s", "%Y-%m-%dT%H:%M:%S")
    fmt.converter = time.gmtime  # log in UTC
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    for h in handlers:
        h.setFormatter(fmt)
        h.addFilter(RedactFilter())
        root.addHandler(h)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
