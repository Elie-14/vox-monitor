"""Beginner-friendly status report + tiny /health HTTP endpoint (for hosts that need one)."""
import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from zoneinfo import ZoneInfo

from .database import Database, parse_iso, utcnow
from .notifications import channel_registry

CAIRO = ZoneInfo("Africa/Cairo")


def _fmt(ts: str | None) -> str:
    dt = parse_iso(ts)
    return dt.astimezone(CAIRO).strftime("%Y-%m-%d %H:%M:%S") + " Cairo" if dt else "never"


def degraded_sources(db: Database, cfg: dict, now: datetime | None = None) -> list[str]:
    now = now or utcnow()
    limit = timedelta(minutes=cfg.get("degraded_after_minutes", 45))
    bad = []
    for src in cfg.get("critical_sources", ["vox_whatson"]):
        last = parse_iso(db.get_source(src)["last_ok_utc"])
        first = parse_iso(db.get_kv("monitor_first_run"))
        ref = last or first
        if ref is None or now - ref > limit:
            bad.append(src)
    return bad


def build_status(db: Database, cfg: dict) -> str:
    now = utcnow()
    st = db.get_kv("booking_state", {})
    bad = degraded_sources(db, cfg, now)
    hb = parse_iso(db.get_kv("heartbeat"))
    lines = ["=" * 60, "VOX AVENGERS: DOOMSDAY MONITOR - STATUS", "=" * 60,
             f"Overall monitor : {'DEGRADED - ' + ', '.join(bad) + ' not checked recently' if bad else 'HEALTHY'}",
             f"Booking state   : {st.get('state', 'UNKNOWN')}   (why: {st.get('evidence_summary') or '-'})",
             f"Last valid link : {st.get('booking_url') or 'none yet'}",
             f"Last heartbeat  : {_fmt(db.get_kv('heartbeat'))}" + (f"  ({int((now - hb).total_seconds() // 60)} min ago)" if hb else ""),
             f"Polling mode    : {db.get_kv('polling_mode', '-')} (every ~{db.get_kv('polling_seconds', '-')} s)",
             "", "Sources:"]
    for s in db.all_sources():
        lines.append(f"  - {s['source']:<22} last OK: {_fmt(s['last_ok_utc']):<28} fails in a row: {s['consecutive_failures']}"
                     f"  total fails: {s['total_failures']}" + (f"  last error: {s['last_error']}" if s["last_error"] else ""))
    lines += ["", "Notification channels (configured?):"]
    health = db.channel_health()
    for name, ok, _ in channel_registry(cfg):
        h = health.get(name, {})
        lines.append(f"  - {name:<9} {'configured' if ok else 'not configured':<15} sent={h.get('sent', 0)} failed={h.get('failed', 0)}")
    la = db.last_alert()
    lines += ["", f"Last alert sent : {_fmt(la['sent_utc']) + ' (' + la['alert_key'] + ' via ' + la['channel'] + ')' if la else 'none yet'}",
              f"Known cinemas   : {', '.join(db.get_kv('cinemas', [])) or 'not read yet'}", "=" * 60]
    return "\n".join(lines)


def serve_health(db_path: str, cfg: dict, port: int) -> HTTPServer:
    from .database import Database as DB

    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            db = DB(db_path)
            bad = degraded_sources(db, cfg)
            body = json.dumps({"ok": not bad, "degraded": bad, "state": db.get_kv("booking_state", {}).get("state"),
                               "heartbeat": db.get_kv("heartbeat")}).encode()
            db.close()
            self.send_response(200 if not bad else 503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):  # silence
            pass

    srv = HTTPServer(("0.0.0.0", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
