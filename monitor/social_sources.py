"""Social / announcement sources.

Only legitimately automatable sources are polled (RSS/Atom feeds). Platforms whose public pages sit behind logins
or paid/approved APIs (Facebook, Instagram, TikTok) are registered as 'manual' and are NEVER scraped.
"""
import hashlib
import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from .database import Database, iso, utcnow
from .detection import classify_text, normalize
from .http_client import PoliteClient

log = logging.getLogger(__name__)
ATOM = "{http://www.w3.org/2005/Atom}"


@dataclass
class Post:
    post_id: str
    url: str
    published_utc: str | None
    text: str


def _txt(el, *names) -> str:
    for n in names:
        found = el.find(n)
        if found is not None and (found.text or "").strip():
            return found.text.strip()
    return ""


def parse_feed(xml_text: str) -> list[Post]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ValueError(f"feed is not valid XML: {exc}") from exc
    posts: list[Post] = []
    for it in root.iter("item"):  # RSS 2.0
        title, desc = _txt(it, "title"), _txt(it, "description")
        link, guid, pub = _txt(it, "link"), _txt(it, "guid"), _txt(it, "pubDate")
        published = None
        if pub:
            try:
                published = iso(parsedate_to_datetime(pub).astimezone(timezone.utc))
            except (TypeError, ValueError):
                pass
        posts.append(Post(guid or link or title, link, published, f"{title}\n{desc}".strip()))
    for en in root.iter(f"{ATOM}entry"):  # Atom (YouTube etc.)
        link_el = en.find(f"{ATOM}link")
        link = link_el.get("href", "") if link_el is not None else ""
        pid = _txt(en, f"{ATOM}id") or link
        pub = _txt(en, f"{ATOM}published", f"{ATOM}updated")
        summary = _txt(en, f"{ATOM}summary", f"{ATOM}content")
        media = en.find("{http://search.yahoo.com/mrss/}group")
        if media is not None:
            summary = summary or _txt(media, "{http://search.yahoo.com/mrss/}description")
        published = pub.replace("+00:00", "Z")[:19] + "Z" if pub else None
        posts.append(Post(pid, link, published, f"{_txt(en, f'{ATOM}title')}\n{summary}".strip()))
    return posts


def registry(cfg: dict) -> list[dict]:
    return cfg.get("sources", [])


def poll_source(src: dict, client: PoliteClient, db: Database, now: datetime | None = None) -> list[dict]:
    """Fetch a feed source; returns list of NEW/EDITED relevant posts [{post, verdict, trusted, change}]."""
    now = now or utcnow()
    name = src["id"]
    if src.get("method") != "rss" or not src.get("enabled", False) or not src.get("feed_url"):
        return []
    last = db.get_source(name).get("last_attempt_utc")
    interval = src.get("interval_seconds", 600)
    if last and (now - datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)).total_seconds() < interval * 0.9:
        return []
    res = client.get(name, src["feed_url"], conditional=True)
    if not res.ok or res.not_modified:
        return []
    try:
        posts = parse_feed(res.text)
    except ValueError as exc:
        db.record_check(name, False, str(exc))
        return []
    relevant = []
    for p in posts:
        verdict = classify_text(p.text)
        h = hashlib.sha256(normalize(p.text).encode()).hexdigest()
        change = db.upsert_post(name, p.post_id, p.url, p.published_utc, p.text[:2000], h, verdict.kind, now)
        if change == "same" or verdict.kind in ("NONE", "MENTION"):
            continue
        repost = db.text_hash_seen_elsewhere(h, name, p.post_id)
        relevant.append({"post": p, "verdict": verdict, "trusted": bool(src.get("trusted", False)),
                         "change": change, "repost": repost, "source": name})
    return relevant


def current_social_signal(db: Database, cfg: dict, now: datetime | None = None) -> tuple[str | None, str | None, str | None]:
    """Most relevant trusted-source signal in the last N hours -> (signal, url, source)."""
    now = now or utcnow()
    hours = cfg.get("social_signal_window_hours", 48)
    trusted = {s["id"] for s in registry(cfg) if s.get("trusted")}
    posts = db.recent_posts(now - timedelta(hours=hours), ("OPEN_NOW", "FUTURE"), trusted)
    for kind in ("OPEN_NOW", "FUTURE"):
        for p in posts:
            if p["classification"] == kind:
                return kind, p["url"], p["source"]
    return None, None, None
