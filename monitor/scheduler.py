"""Adaptive polling interval."""
import random

from .detection import ANNOUNCED, CONFIRMED_OPEN, SUSPECTED_OPEN


def choose_mode(cfg: dict, state: str, social: str | None) -> tuple[str, int]:
    s = cfg["schedule"]
    if state == CONFIRMED_OPEN:
        return "post_confirmation", s["post_confirmation_seconds"]
    if state == SUSPECTED_OPEN or social == "OPEN_NOW":
        return "high", s["high_seconds"]
    if state == ANNOUNCED or social == "FUTURE":
        return "elevated", s["elevated_seconds"]
    return "normal", s["normal_seconds"]


def next_interval(cfg: dict, state: str, social: str | None, rng: random.Random | None = None) -> tuple[str, float]:
    """Interval with +-10% jitter, never below the configured floor (site-friendly safety limit)."""
    rng = rng or random
    mode, secs = choose_mode(cfg, state, social)
    secs = max(secs, cfg["schedule"]["min_seconds"])
    return mode, secs * rng.uniform(0.9, 1.1)
