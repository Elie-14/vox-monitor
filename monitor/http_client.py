"""Polite HTTP client: timeouts, conditional requests, robots.txt, backoff, block detection."""
import hashlib
import logging
import random
import time
import urllib.robotparser
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlparse

import requests

from .database import Database, utcnow

log = logging.getLogger(__name__)
BLOCK_MARKERS = ("captcha", "cf-challenge", "just a moment", "access denied", "are you a robot", "unusual traffic")


@dataclass
class FetchResult:
    ok: bool
    status: int | None = None
    text: str = ""
    not_modified: bool = False
    blocked: bool = False
    error: str | None = None
    skipped: bool = False


def backoff_seconds(failures: int, base: int = 30, cap: int = 3600, rng: random.Random | None = None) -> float:
    """Exponential backoff with full jitter."""
    rng = rng or random
    return rng.uniform(base / 2, min(cap, base * (2 ** max(0, failures - 1))))


class RateLimiter:
    """Hard cap on requests per rolling hour (safe-fallback protection)."""
    def __init__(self, max_per_hour: int):
        self.max = max_per_hour
        self.stamps: list[float] = []

    def allow(self, now: float | None = None) -> bool:
        now = now or time.time()
        self.stamps = [s for s in self.stamps if now - s < 3600]
        if len(self.stamps) >= self.max:
            return False
        self.stamps.append(now)
        return True


class PoliteClient:
    def __init__(self, cfg: dict, db: Database, session: requests.Session | None = None):
        self.cfg = cfg
        self.db = db
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": cfg.get("user_agent", "vox-avengers-monitor/1.0 (personal ticket alert)"),
                                     "Accept-Language": "en,ar;q=0.8"})
        self.timeout = cfg.get("request_timeout_seconds", 15)
        self.limiter = RateLimiter(cfg.get("max_requests_per_hour", 150))
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    def robots_allows(self, url: str) -> bool:
        if not self.cfg.get("respect_robots", True):
            return True
        p = urlparse(url)
        origin = f"{p.scheme}://{p.netloc}"
        if origin not in self._robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                r = self.session.get(origin + "/robots.txt", timeout=self.timeout)
                if r.status_code == 200:
                    rp.parse(r.text.splitlines())
                    self._robots[origin] = rp
                else:
                    self._robots[origin] = None  # no usable robots.txt -> nothing disallowed
            except requests.RequestException:
                self._robots[origin] = None
        rp = self._robots[origin]
        return True if rp is None else rp.can_fetch(self.session.headers["User-Agent"], url)

    def get(self, source: str, url: str, conditional: bool = True) -> FetchResult:
        now = utcnow()
        st = self.db.get_source(source)
        nxt = st.get("next_allowed_utc")
        if nxt and nxt > now.strftime("%Y-%m-%dT%H:%M:%SZ"):
            return FetchResult(False, skipped=True, error=f"backing off until {nxt}")
        if not self.robots_allows(url):
            self.db.record_check(source, False, "blocked by robots.txt")
            return FetchResult(False, blocked=True, error="robots.txt disallows this URL")
        if not self.limiter.allow():
            return FetchResult(False, skipped=True, error="local request-rate cap reached")
        headers = {}
        if conditional and st.get("etag"):
            headers["If-None-Match"] = st["etag"]
        if conditional and st.get("last_modified"):
            headers["If-Modified-Since"] = st["last_modified"]
        try:
            r = self.session.get(url, headers=headers, timeout=self.timeout)
        except requests.RequestException as exc:
            return self._fail(source, f"network error: {type(exc).__name__}")
        if r.status_code == 304:
            self.db.record_check(source, True)
            self.db.set_next_allowed(source, None)
            return FetchResult(True, 304, not_modified=True)
        if r.status_code in (403, 429, 503) or any(m in r.text[:5000].lower() for m in BLOCK_MARKERS):
            extra = float(r.headers.get("Retry-After", 0)) if r.headers.get("Retry-After", "").isdigit() else 0
            res = self._fail(source, f"blocked or rate-limited (HTTP {r.status_code})", extra_wait=extra)
            res.blocked, res.status = True, r.status_code
            return res
        if r.status_code >= 400:
            res = self._fail(source, f"HTTP {r.status_code}")
            res.status = r.status_code
            return res
        if not r.text.strip():
            return self._fail(source, "empty response")
        self.db.record_check(source, True)
        self.db.set_next_allowed(source, None)
        self.db.set_validators(source, etag=r.headers.get("ETag"), last_modified=r.headers.get("Last-Modified"),
                               content_hash=hashlib.sha256(r.content).hexdigest())
        return FetchResult(True, r.status_code, r.text)

    def _fail(self, source: str, msg: str, extra_wait: float = 0) -> FetchResult:
        self.db.record_check(source, False, msg)
        n = self.db.get_source(source)["consecutive_failures"]
        wait = max(extra_wait, backoff_seconds(n, self.cfg.get("backoff_base_seconds", 30), self.cfg.get("backoff_cap_seconds", 3600)))
        self.db.set_next_allowed(source, utcnow() + timedelta(seconds=wait))
        log.warning("source=%s failure #%d: %s (backing off %.0fs)", source, n, msg, wait)
        return FetchResult(False, error=msg)
