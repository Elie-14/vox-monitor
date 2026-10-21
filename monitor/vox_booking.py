"""VOX Cinemas Egypt booking-site monitor.

Verified (18 Oct 2026) by reading the public pages:
  * Movies that can be booked appear on  /movies/whatson  as a card with a "Showtimes" link
    (/movies/<slug>#showtimes) AND in the "Select Your Movie(s)" selector.
  * Movies that are not bookable yet (e.g. Avengers: Doomsday, release 16 Dec 2026) appear only on /movies/comingsoon.
  * A movie page's <title> already says "Now Showing | Book Tickets" even when it is only Coming Soon -> the title
    is NOT used as evidence.
NOT verified: the exact HTML of the per-cinema session list (no tickets are on sale for the target yet).
"""
import logging
import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

from .detection import Evidence, mentions_target, normalize
from .http_client import PoliteClient

log = logging.getLogger(__name__)
_SLUG = re.compile(r"/movies/([a-z0-9][a-z0-9\-]*)(?:[/?#]|$)", re.I)
_NOT_MOVIE = {"whatson", "comingsoon"}


@dataclass
class Listing:
    cards: dict = field(default_factory=dict)        # slug -> {"title":..., "showtimes": bool}
    selector_movies: list = field(default_factory=list)
    cinemas: list = field(default_factory=list)


def _slug_of(href: str) -> str | None:
    m = _SLUG.search(href or "")
    if not m or m.group(1).lower() in _NOT_MOVIE:
        return None
    return m.group(1).lower()


def _between(text: str, start: str, ends: tuple[str, ...]) -> list[str]:
    i = text.find(start)
    if i < 0:
        return []
    seg = text[i + len(start):]
    cut = len(seg)
    for e in ends:
        j = seg.find(e)
        if j >= 0:
            cut = min(cut, j)
    lines = [re.sub(r"^\[[ x]?\]\s*", "", ln.strip()) for ln in seg[:cut].splitlines()]
    return [ln for ln in lines if ln and ln.lower() not in ("any movie", "clear all", "done", "close")]


def parse_listing(html: str) -> Listing:
    soup = BeautifulSoup(html, "html.parser")
    out = Listing()
    for a in soup.find_all("a", href=True):
        slug = _slug_of(a["href"])
        if not slug:
            continue
        entry = out.cards.setdefault(slug, {"title": "", "showtimes": False})
        txt = a.get_text(" ", strip=True)
        if txt.lower() == "showtimes" or a["href"].lower().endswith("#showtimes"):
            entry["showtimes"] = True
        elif txt and not entry["title"] and a.find_parent(["h2", "h3", "h4"]):
            entry["title"] = re.sub(r"^(?:G|PG|12\+|15\+|16\+|18\+|18TC|PG15)\s+", "", txt)
    page_text = soup.get_text("\n")
    out.selector_movies = _between(page_text, "Select Your Movie(s)", ("Find Times and Book", "Clear All\nDone\nFind"))
    # selector block ends at the first "Find Times and Book"; strip trailing control words
    out.cinemas = _between(page_text, "Select Your Cinema(s)", ("Clear All", "Select Your Movie(s)"))
    return out


def target_slugs_in(listing: Listing, cfg: dict, require_showtimes: bool) -> list[str]:
    wanted = {s.lower() for s in cfg.get("target_slugs", [])}
    hits = []
    for slug, e in listing.cards.items():
        if slug in wanted or mentions_target(e["title"]):
            if (e["showtimes"] or not require_showtimes):
                hits.append(slug)
    return hits


def target_in_selector(listing: Listing, cfg: dict) -> bool:
    return any(mentions_target(t) for t in listing.selector_movies)


def verify_movie_page(html: str) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    h1 = soup.find("h1")
    og = soup.find("meta", attrs={"property": "og:title"})
    heading = (h1.get_text(" ", strip=True) if h1 else "") + " " + (og.get("content", "") if og else "")
    return mentions_target(heading)


def check_booking(client: PoliteClient, cfg: dict, check_coming_soon: bool = True) -> tuple[Evidence, dict]:
    """Returns (evidence, extras). extras['cinemas'] lists the branches the site currently shows."""
    base = cfg["base_url"].rstrip("/")
    extras: dict = {}
    res = client.get("vox_whatson", f"{base}/movies/whatson", conditional=False)
    if not res.ok:
        return Evidence(False, error=res.error or "vox_whatson failed"), extras
    listing = parse_listing(res.text)
    if not listing.cards:  # page loaded but looks wrong -> inconclusive, NOT evidence of anything
        client.db.record_check("vox_whatson", False, "page parsed but no movie cards found (layout changed?)")
        return Evidence(False, error="what's-on page had no movie cards (layout changed?)"), extras
    extras["cinemas"] = listing.cinemas

    open_slugs = target_slugs_in(listing, cfg, require_showtimes=True)
    in_selector = target_in_selector(listing, cfg)
    in_cs = False
    if check_coming_soon:
        cs = client.get("vox_comingsoon", f"{base}/movies/comingsoon", conditional=False)
        if cs.ok:
            cs_listing = parse_listing(cs.text)
            in_cs = bool(target_slugs_in(cs_listing, cfg, require_showtimes=False))
    elif (client.db.get_kv("last_in_coming_soon") or False):
        in_cs = True
    if check_coming_soon:
        client.db.set_kv("last_in_coming_soon", in_cs)

    page_verified, booking_url = False, None
    candidates = open_slugs or ([s for s in cfg.get("target_slugs", [])] if in_selector else [])
    for slug in candidates:
        url = f"{base}/movies/{slug}"
        pg = client.get(f"vox_movie_{slug}", url, conditional=False)
        if pg.ok and verify_movie_page(pg.text):
            page_verified, booking_url = True, url + "#showtimes"
            break
    if open_slugs and not booking_url:
        booking_url = f"{base}/movies/{open_slugs[0]}#showtimes"
    return Evidence(True, on_whatson=bool(open_slugs), in_selector=in_selector, in_coming_soon=in_cs,
                    page_verified=page_verified, booking_url=booking_url), extras
