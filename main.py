"""VOX Avengers: Doomsday monitor - command line entry point.

Commands:
  python main.py run-once [--dry-run]            one full check (used by GitHub Actions)
  python main.py loop [--duration SEC] [--dry-run]   keep checking with the adaptive schedule
  python main.py status                          beginner-friendly status report
  python main.py check-sources                   test every configured source
  python main.py test-alert [--channel NAME]     send a TEST alert (email/ntfy/telegram/whatsapp)
"""
import argparse
import logging
import os
import sys
import time
import uuid
from dataclasses import asdict
from datetime import timedelta

import requests
import yaml

from monitor import health, social_sources, vox_booking
from monitor.database import Database, iso, utcnow
from monitor.detection import Alert, BookingState, Evidence, advance
from monitor.http_client import PoliteClient
from monitor.logging_config import setup_logging
from monitor.notifications import channel_registry, dispatch
from monitor.scheduler import next_interval

log = logging.getLogger("main")


def load_env_file(path: str = ".env") -> None:
    """Tiny .env loader for local runs (GitHub uses real secrets instead)."""
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_config(path: str = "config.yaml") -> dict:
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    cfg = dict(raw)
    cfg.update({k: v for k, v in raw["vox"].items()})  # flatten vox.* for the client/monitor
    return cfg


def _state_from_db(db: Database) -> BookingState:
    d = db.get_kv("booking_state")
    return BookingState(**d) if d else BookingState()


def run_cycle(cfg: dict, db: Database, client: PoliteClient, dry_run: bool = False, owner: str = "worker") -> dict:
    """One full monitoring pass. Returns a small summary dict (also used by tests)."""
    now = utcnow()
    if not db.get_kv("monitor_first_run"):
        db.set_kv("monitor_first_run", iso(now))
    if not db.try_lock(owner, ttl_seconds=600, now=now):
        log.warning("another worker holds the lock - skipping this cycle")
        return {"skipped": "locked"}
    try:
        # 1) social / announcements ------------------------------------------------
        hint = False
        for src in social_sources.registry(cfg):
            for item in social_sources.poll_source(src, client, db, now):
                v = item["verdict"]
                if not item["trusted"]:
                    hint = True
                    continue
                log.info("social signal %s from %s (%s)", v.kind, src["id"], item["post"].url)
                if v.kind == "OPEN_NOW" and item["change"] in ("new", "edited") and not item["repost"]:
                    pass  # state machine below turns this into a preliminary alert (dedup by opening_id)
        if hint:
            db.set_kv("discovery_hint_until", iso(now + timedelta(hours=6)))
        social, social_url, social_src = social_sources.current_social_signal(db, cfg, now)

        # 2) the official booking pages --------------------------------------------
        prev = _state_from_db(db)
        poll_n = (db.get_kv("poll_counter") or 0) + 1
        db.set_kv("poll_counter", poll_n)
        check_cs = prev.state != "CONFIRMED_OPEN" and (poll_n % 3 == 1 or prev.state in ("UNKNOWN", "ERROR", "ANNOUNCED"))
        ev, extras = vox_booking.check_booking(client, cfg, check_coming_soon=check_cs)
        if social:
            ev = Evidence(**{**asdict(ev), "social": social, "social_url": social_url, "social_source": social_src})
        det = cfg.get("detection", {})
        new, alert = advance(prev, ev, det.get("miss_threshold", 3), det.get("realert_on_reopen", True), iso(now))
        if new.state != prev.state:
            log.info("STATE %s -> %s (%s)", prev.state, new.state, new.evidence_summary)
            db.log_event("state", f"{prev.state}->{new.state}: {new.evidence_summary}", now)
        db.set_kv("booking_state", asdict(new))

        # 3) branch discovery ---------------------------------------------------------
        cinemas = extras.get("cinemas") or []
        known = db.get_kv("cinemas")
        if cinemas:
            if known is not None and set(cinemas) - set(known):
                added = sorted(set(cinemas) - set(known))
                dispatch(db, Alert("info", f"new-cinema:{'|'.join(added)}", "VOX Egypt added a new cinema",
                                   f"New branch(es) listed: {', '.join(added)}", cfg['base_url'] + "/movies/whatson"), cfg, dry_run)
            db.set_kv("cinemas", cinemas)

        # 4) alerts ----------------------------------------------------------------
        results = {}
        if alert:
            results = dispatch(db, alert, cfg, dry_run)
        elif new.state in ("CONFIRMED_OPEN", "SUSPECTED_OPEN"):
            # retry channels that failed earlier for the same opening (never duplicates a delivered one)
            key = f"{'critical' if new.state == 'CONFIRMED_OPEN' else 'preliminary'}:{new.opening_id}"
            if any((db.notification_status(key, n) or {}).get("status") == "failed" for n, ok, _ in channel_registry(cfg) if ok):
                title = "AVENGERS: DOOMSDAY BOOKING IS OPEN at VOX Egypt (CONFIRMED)" if new.state == "CONFIRMED_OPEN" else \
                        "Avengers: Doomsday booking MAY have opened at VOX Egypt (preliminary)"
                results = dispatch(db, Alert("critical" if new.state == "CONFIRMED_OPEN" else "preliminary", key, title,
                                             new.evidence_summary, new.booking_url), cfg, dry_run)

        # 5) coverage / watchdog -----------------------------------------------------
        bad = health.degraded_sources(db, cfg, now)
        was_bad = db.get_kv("degraded_flag", False)
        if bad and not was_bad:
            ep = db.get_kv("degraded_episode", 0) + 1
            db.set_kv("degraded_episode", ep)
            dispatch(db, Alert("degraded", f"degraded:{ep}", "Monitor coverage DEGRADED (VOX page not readable)",
                               f"Not read successfully for {cfg.get('degraded_after_minutes', 45)}+ minutes: {', '.join(bad)}. "
                               "The monitor is NOT healthy - check manually: https://egy.voxcinemas.com/movies/comingsoon",
                               cfg['base_url'] + "/movies/comingsoon"), cfg, dry_run)
        elif was_bad and not bad:
            dispatch(db, Alert("recovered", f"recovered:{db.get_kv('degraded_episode', 0)}", "Monitor coverage recovered",
                               "VOX pages are being read successfully again."), cfg, dry_run)
        db.set_kv("degraded_flag", bool(bad))

        db.set_kv("heartbeat", iso(now))
        mode, secs = next_interval(cfg, new.state, social or ("FUTURE" if (db.get_kv("discovery_hint_until") or "") > iso(now) else None))
        db.set_kv("polling_mode", mode)
        db.set_kv("polling_seconds", int(secs))
        ping = os.environ.get("HEALTHCHECK_PING_URL")
        if ping and not bad and not dry_run:
            try:
                requests.get(ping, timeout=10)
            except requests.RequestException:
                log.warning("healthcheck ping failed")
        return {"state": new.state, "alert": alert.key if alert else None, "results": results, "next_seconds": secs, "mode": mode}
    finally:
        db.release_lock(owner)


def cmd_loop(cfg, db, client, dry_run, duration):
    owner = f"loop-{uuid.uuid4().hex[:8]}"
    end = time.time() + duration if duration else None
    if os.environ.get("PORT"):
        health.serve_health(db.path, cfg, int(os.environ["PORT"]))
    stop = False
    while not stop:
        try:
            out = run_cycle(cfg, db, client, dry_run, owner)
            wait = out.get("next_seconds", 300)
        except Exception:  # noqa: BLE001 - the loop must survive anything
            log.exception("cycle crashed; retrying after backoff")
            wait = 120
        if end and time.time() + wait > end:
            break
        try:
            time.sleep(wait)
        except KeyboardInterrupt:
            log.info("stopped by user (Ctrl+C)")
            stop = True


def cmd_check_sources(cfg, db, client):
    print("Source inventory (what is REALLY monitored):\n")
    base = cfg["base_url"]
    for name, url in (("vox_whatson", f"{base}/movies/whatson"), ("vox_comingsoon", f"{base}/movies/comingsoon")):
        r = client.get(name, url, conditional=False)
        print(f"  [{'OK ' if r.ok else 'FAIL'}] {name:<22} automated  {url}  {'' if r.ok else r.error}")
    for s in social_sources.registry(cfg):
        active = s.get("method") == "rss" and s.get("enabled") and s.get("feed_url")
        status = "AUTOMATED" if active else ("DISABLED " if s.get("method") == "rss" else "MANUAL   ")
        extra = ""
        if active:
            r = client.get(s["id"], s["feed_url"], conditional=False)
            extra = "reachable" if r.ok else f"UNREACHABLE: {r.error}"
        print(f"  [{status}] {s['id']:<26} {s.get('profile_url') or '(no url)'}  {extra}\n              {s.get('limitation', '')}")
    print("\nLast successful checks:")
    for row in db.all_sources():
        print(f"  {row['source']:<26} {row['last_ok_utc'] or 'never'}")


def cmd_test_alert(cfg, db, channel, dry_run):
    key = f"test:{uuid.uuid4().hex[:8]}"
    alert = Alert("test", key, "TEST - Avengers: Doomsday monitor", "This is only a test. Nothing has opened.",
                  "https://egy.voxcinemas.com/movies/avengers-doomsday")
    if channel:
        cfg = {**cfg, "notifications": {**cfg["notifications"], **{c: (c == channel) for c in ("email", "ntfy", "telegram", "whatsapp")}}}
    print("Results:", dispatch(db, alert, cfg, dry_run))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["run-once", "loop", "status", "check-sources", "test-alert"])
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--db", default=os.environ.get("MONITOR_DB", "monitor.db"))
    ap.add_argument("--dry-run", action="store_true", help="check everything, send nothing")
    ap.add_argument("--duration", type=int, default=0, help="loop: stop after this many seconds")
    ap.add_argument("--channel", choices=["email", "ntfy", "telegram", "whatsapp"])
    args = ap.parse_args(argv)
    load_env_file()
    setup_logging(os.environ.get("LOG_LEVEL", "INFO"))
    cfg = load_config(args.config)
    dry = args.dry_run or os.environ.get("DRY_RUN") == "1"
    db = Database(args.db)
    client = PoliteClient(cfg, db)
    try:
        if args.command == "status":
            print(health.build_status(db, cfg))
        elif args.command == "check-sources":
            cmd_check_sources(cfg, db, client)
        elif args.command == "test-alert":
            cmd_test_alert(cfg, db, args.channel, dry)
        elif args.command == "run-once":
            print(run_cycle(cfg, db, client, dry, f"once-{uuid.uuid4().hex[:8]}"))
        else:
            cmd_loop(cfg, db, client, dry, args.duration)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
