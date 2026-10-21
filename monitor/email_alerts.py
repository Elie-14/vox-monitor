"""Email via SMTP (Gmail App Password works on the free tier)."""
import os
import smtplib
import ssl
from email.message import EmailMessage


def configured() -> bool:
    return all(os.environ.get(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "ALERT_EMAIL_TO"))


def send(subject: str, body: str) -> None:
    host, port = os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", "465"))
    user, pwd, to = os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"], os.environ["ALERT_EMAIL_TO"]
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, os.environ.get("ALERT_EMAIL_FROM", user), to
    msg.set_content(body)
    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
            s.login(user, pwd)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls(context=ctx)
            s.login(user, pwd)
            s.send_message(msg)
