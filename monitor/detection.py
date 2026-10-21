"""Text classification (English + Arabic) and the booking-availability state machine.

Design rules
------------
* A failed request, CAPTCHA, empty page or changed HTML is NEVER evidence that tickets opened.
* Only the VOX site itself can move the state to CONFIRMED_OPEN.
* Social posts / news can at most produce SUSPECTED_OPEN (a *preliminary* alert) or ANNOUNCED.
"""
import re
import unicodedata
from dataclasses import dataclass, replace

UNKNOWN = "UNKNOWN"
NOT_LISTED = "NOT_LISTED"
COMING_SOON = "COMING_SOON"
ANNOUNCED = "ANNOUNCED"
SUSPECTED_OPEN = "SUSPECTED_OPEN"
CONFIRMED_OPEN = "CONFIRMED_OPEN"
TEMPORARILY_UNAVAILABLE = "TEMPORARILY_UNAVAILABLE"
ERROR = "ERROR"
OPEN_STATES = (SUSPECTED_OPEN, CONFIRMED_OPEN)

# ---------------------------------------------------------------- text helpers
_AR_MARKS = re.compile(r"[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED\u0640]")
_AR_MAP = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ى": "ي", "ة": "ه", "ؤ": "و", "ئ": "ي"})


def normalize(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "")
    t = _AR_MARKS.sub("", t).translate(_AR_MAP).lower()
    t = t.replace("’", "'").replace("‘", "'")
    return re.sub(r"\s+", " ", t).strip()


_TITLE_FULL = [
    re.compile(r"avengers?\s*[:\-–—_]?\s*doom'?s?\s*day"),
    re.compile(r"(?:افنجرز|الافنجرز|الانتقاميون)\s*[:\-–—]?\s*(?:دومزداي|دومسداي|دوومزداي|دوم\s*زداي|يوم\s*القياماه|doomsday)"),
]
_TITLE_ALONE = re.compile(r"\bdoomsday\b|دومزداي|دومسداي")
_TITLE_CONTEXT = re.compile(r"avengers|marvel|افنجرز|الانتقاميون|مارفل")


def mentions_target(text: str) -> bool:
    t = normalize(text)
    if any(p.search(t) for p in _TITLE_FULL):
        return True
    return bool(_TITLE_ALONE.search(t) and _TITLE_CONTEXT.search(t))


_OPEN = [
    r"tickets?\s+(?:are\s+)?(?:now\s+)?(?:on\s+sale|available|live)",
    r"(?:on\s+sale|available|booking|bookings)\s+(?:now|today)",
    r"book(?:ing|ings)?\s+(?:is\s+|are\s+)?(?:now\s+)?(?:open|live)",
    r"now\s+(?:booking|open\s+for\s+booking|on\s+sale)",
    r"book\s+(?:your\s+)?(?:tickets?|seats?)\s+now",
    r"(?:grab|get)\s+your\s+tickets?\s+now",
    r"الحجز\s+(?:متاح|مفتوح|بدا)",
    r"(?:بدا|بدء|فتح)\s+(?:ال)?حجز",
    r"(?:ال)?تذاكر\s+متاحه",
    r"احجز\s+(?:تذكرتك|تذاكرك|مقعدك)",
    r"متاحه?\s+(?:الان|للحجز)",
]
_FUTURE = [
    r"coming\s+soon", r"stay\s+tuned", r"get\s+ready", r"\bsoon\b", r"\btomorrow\b", r"countdown",
    r"(?:will|to)\s+(?:be\s+)?(?:available|on\s+sale|open|start|begin)",
    r"go(?:es)?\s+on\s+sale(?!\s+(?:now|today))",
    r"(?:available|on\s+sale|opens?|opening|starts?|starting|begins?|beginning)\s+(?:from|on|next|in\s+\d+|this\s+\w+day)",
    r"\bnext\s+(?:week|month|\w+day)",
    r"قريبا", r"ترقبوا", r"استعدوا", r"غدا",
    r"الاسبوع\s+(?:القادم|الجاي)", r"ابتداء\s+من",
    r"(?:هي|هت|سي|ست)(?:بدا|تاح|فتح|طرح)",
]
_ADVANCE = [r"advance\s+(?:booking|tickets?)", r"pre-?\s?sale", r"pre-?\s?booking", r"حجز\s+مسبق"]
_OPEN_RE = [re.compile(p) for p in _OPEN]
_FUTURE_RE = [re.compile(p) for p in _FUTURE]
_ADVANCE_RE = [re.compile(p) for p in _ADVANCE]


@dataclass(frozen=True)
class TextVerdict:
    kind: str            # OPEN_NOW | FUTURE | MENTION | NONE
    open_hits: tuple = ()
    future_hits: tuple = ()
    mixed: bool = False


def classify_text(text: str) -> TextVerdict:
    """Context-aware classification of an announcement."""
    if not mentions_target(text):
        return TextVerdict("NONE")
    t = normalize(text)
    open_hits = tuple(p.pattern for p in _OPEN_RE if p.search(t))
    future_hits = tuple(p.pattern for p in _FUTURE_RE if p.search(t))
    advance = any(p.search(t) for p in _ADVANCE_RE)
    if open_hits and not future_hits:
        return TextVerdict("OPEN_NOW", open_hits, future_hits)
    if future_hits:
        return TextVerdict("FUTURE", open_hits, future_hits, mixed=bool(open_hits))
    if advance:
        return TextVerdict("FUTURE", open_hits, ("advance-mention",))
    return TextVerdict("MENTION")


# ---------------------------------------------------------------- state machine
@dataclass(frozen=True)
class Evidence:
    fetch_ok: bool
    error: str | None = None
    on_whatson: bool = False        # target card with a "Showtimes" link on /movies/whatson
    in_selector: bool = False       # target listed in the site's bookable-movie selector
    in_coming_soon: bool = False
    page_verified: bool = False     # movie page really is Avengers: Doomsday
    booking_url: str | None = None
    social: str | None = None       # OPEN_NOW | FUTURE | None  (from trusted official sources only)
    social_url: str | None = None
    social_source: str | None = None


@dataclass(frozen=True)
class BookingState:
    state: str = UNKNOWN
    opening_id: int = 0
    miss_count: int = 0
    evidence_summary: str = ""
    booking_url: str | None = None
    changed_at: str | None = None


@dataclass(frozen=True)
class Alert:
    kind: str       # critical | preliminary | degraded | recovered | info | test
    key: str
    title: str
    body: str
    url: str | None = None


def site_verdict(ev: Evidence) -> str | None:
    """'CONFIRMED', 'SUSPECTED', 'CLOSED' or None when the site could not be read."""
    if not ev.fetch_ok:
        return None
    if ev.on_whatson and ev.in_selector and ev.page_verified:
        return "CONFIRMED"
    if ev.on_whatson or ev.in_selector:
        return "SUSPECTED"
    return "CLOSED"


def _why(ev: Evidence, verdict: str | None) -> str:
    parts = []
    if ev.on_whatson:
        parts.append("listed on VOX What's On with a Showtimes link")
    if ev.in_selector:
        parts.append("present in VOX bookable-movie selector")
    if ev.page_verified:
        parts.append("movie page verified as Avengers: Doomsday")
    if ev.social == "OPEN_NOW":
        parts.append(f"official post says booking is open ({ev.social_source})")
    return "; ".join(parts) or (ev.error or "no evidence")


def advance(prev: BookingState, ev: Evidence, miss_threshold: int = 3, realert_on_reopen: bool = True,
            now_iso: str | None = None) -> tuple[BookingState, Alert | None]:
    """Pure function: previous state + new evidence -> new state (+ optional alert)."""
    verdict = site_verdict(ev)
    new_state = prev.state
    miss = prev.miss_count
    url = ev.booking_url or prev.booking_url

    if verdict == "CONFIRMED":
        new_state, miss = CONFIRMED_OPEN, 0
    elif verdict == "SUSPECTED":
        new_state, miss = (prev.state if prev.state == CONFIRMED_OPEN else SUSPECTED_OPEN), 0
    elif ev.social == "OPEN_NOW":
        # Official social says "open" but the site cannot confirm (closed or unreadable): preliminary only.
        new_state = prev.state if prev.state == CONFIRMED_OPEN else SUSPECTED_OPEN
        miss = 0
    elif verdict is None:
        # Unreadable site: never flip away from an open state, never claim open.
        if prev.state in OPEN_STATES:
            new_state = prev.state
        elif ev.social == "FUTURE":
            new_state = ANNOUNCED
        else:
            new_state = ERROR if prev.state in (UNKNOWN, ERROR) else prev.state
    else:  # site readable, target not bookable
        if prev.state in OPEN_STATES and miss + 1 < miss_threshold:
            new_state, miss = prev.state, miss + 1
        elif prev.state in OPEN_STATES:
            new_state, miss = TEMPORARILY_UNAVAILABLE, 0
        elif prev.state == TEMPORARILY_UNAVAILABLE:
            new_state = TEMPORARILY_UNAVAILABLE
        elif ev.social == "FUTURE":
            new_state = ANNOUNCED
        elif ev.in_coming_soon:
            new_state = COMING_SOON
        else:
            new_state = NOT_LISTED

    opening_id = prev.opening_id
    entering_open = new_state in OPEN_STATES and prev.state not in OPEN_STATES
    if entering_open:
        reopened = prev.opening_id > 0
        opening_id = prev.opening_id + 1 if (not reopened or realert_on_reopen) else prev.opening_id

    alert = None
    if new_state == CONFIRMED_OPEN and prev.state != CONFIRMED_OPEN:
        alert = Alert("critical", f"critical:{opening_id}",
                      "AVENGERS: DOOMSDAY BOOKING IS OPEN at VOX Egypt (CONFIRMED)",
                      f"Confirmed: {_why(ev, verdict)}.", url)
    elif new_state == SUSPECTED_OPEN and prev.state not in OPEN_STATES:
        alert = Alert("preliminary", f"preliminary:{opening_id}",
                      "Avengers: Doomsday booking MAY have opened at VOX Egypt (preliminary)",
                      f"Not yet confirmed by the booking pages: {_why(ev, verdict)}.", url or ev.social_url)

    summary = _why(ev, verdict) if new_state != prev.state or not prev.evidence_summary else prev.evidence_summary
    state = replace(prev, state=new_state, opening_id=opening_id, miss_count=miss, evidence_summary=summary,
                    booking_url=url, changed_at=now_iso if new_state != prev.state else prev.changed_at)
    return state, alert
