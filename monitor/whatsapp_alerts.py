"""WhatsApp Business Cloud API (official). DISABLED by default.

Cost note (verified 18 Oct 2026): since 1 Oct 2026 Meta charges per delivered message even inside the 24-hour
customer-service window, and messaging you first needs an approved template. It needs a Meta Business account,
a phone number registered to the API (not your personal WhatsApp number) and a template. Enable only with approval.
"""
import os

import requests

GRAPH = "https://graph.facebook.com/v21.0"


def configured() -> bool:
    return all(os.environ.get(k) for k in ("WHATSAPP_TOKEN", "WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_TO"))


def send(title: str, message: str, url: str | None) -> None:
    token, phone_id, to = os.environ["WHATSAPP_TOKEN"], os.environ["WHATSAPP_PHONE_NUMBER_ID"], os.environ["WHATSAPP_TO"]
    template = os.environ.get("WHATSAPP_TEMPLATE")
    if template:  # approved template with one body parameter
        body = {"messaging_product": "whatsapp", "to": to, "type": "template",
                "template": {"name": template, "language": {"code": "en"},
                             "components": [{"type": "body", "parameters": [{"type": "text", "text": f"{title} {url or ''}"[:900]}]}]}}
    else:  # free-form text only works inside an open 24h customer-service window
        body = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": f"{title}\n{message}\n{url or ''}"}}
    r = requests.post(f"{GRAPH}/{phone_id}/messages", json=body, headers={"Authorization": f"Bearer {token}"}, timeout=20)
    r.raise_for_status()
