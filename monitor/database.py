"""SQLite persistence: state, source health, posts, notification history, lock."""
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS source_status (
  source TEXT PRIMARY KEY, last_ok_utc TEXT, last_attempt_utc TEXT,
  consecutive_failures INTEGER NOT NULL DEFAULT 0, total_failures INTEGER NOT NULL DEFAULT 0,
  total_checks INTEGER NOT NULL DEFAULT 0, last_error TEXT,
  etag TEXT, last_modified TEXT, content_hash TEXT, next_allowed_utc TEXT);
CREATE TABLE IF NOT EXISTS posts (
  source TEXT NOT NULL, post_id TEXT NOT NULL, url TEXT, published_utc TEXT,
  first_seen_utc TEXT NOT NULL, text TEXT, text_hash TEXT, classification TEXT,
  PRIMARY KEY (source, post_id));
CREATE TABLE IF NOT EXISTS notifications (
  alert_key TEXT NOT NULL, channel TEXT NOT NULL, status TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, created_utc TEXT NOT NULL, sent_utc TEXT,
  PRIMARY KEY (alert_key, channel));
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts_utc TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


class Database:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # ---- key/value -------------------------------------------------
    def get_kv(self, key: str, default=None):
        with self._lock:
            row = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_kv(self, key: str, value) -> None:
        with self._lock:
            self.conn.execute("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                              (key, json.dumps(value, ensure_ascii=False)))

    def log_event(self, kind: str, detail: str = "", now: datetime | None = None) -> None:
        with self._lock:
            self.conn.execute("INSERT INTO events(ts_utc,kind,detail) VALUES(?,?,?)", (iso(now or utcnow()), kind, detail[:500]))
            self.conn.execute("DELETE FROM events WHERE id < (SELECT MAX(id)-2000 FROM events)")

    # ---- lock (protects against duplicate concurrent workers) ------
    def try_lock(self, owner: str, ttl_seconds: int = 600, now: datetime | None = None) -> bool:
        now = now or utcnow()
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                cur = self.get_kv("worker_lock")
                if cur and cur["owner"] != owner and parse_iso(cur["expires"]) > now:
                    self.conn.execute("ROLLBACK")
                    return False
                self.set_kv("worker_lock", {"owner": owner, "expires": iso(now + timedelta(seconds=ttl_seconds))})
                self.conn.execute("COMMIT")
                return True
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    def release_lock(self, owner: str) -> None:
        cur = self.get_kv("worker_lock")
        if cur and cur["owner"] == owner:
            self.set_kv("worker_lock", {"owner": owner, "expires": iso(utcnow() - timedelta(seconds=1))})

    # ---- source health ---------------------------------------------
    def get_source(self, source: str) -> dict:
        with self._lock:
            row = self.conn.execute("SELECT * FROM source_status WHERE source=?", (source,)).fetchone()
        return dict(row) if row else {"source": source, "last_ok_utc": None, "last_attempt_utc": None,
                                      "consecutive_failures": 0, "total_failures": 0, "total_checks": 0,
                                      "last_error": None, "etag": None, "last_modified": None,
                                      "content_hash": None, "next_allowed_utc": None}

    def _save_source(self, d: dict) -> None:
        cols = ["source", "last_ok_utc", "last_attempt_utc", "consecutive_failures", "total_failures", "total_checks",
                "last_error", "etag", "last_modified", "content_hash", "next_allowed_utc"]
        with self._lock:
            self.conn.execute(f"INSERT OR REPLACE INTO source_status({','.join(cols)}) VALUES({','.join('?'*len(cols))})",
                              [d.get(c) for c in cols])

    def record_check(self, source: str, ok: bool, error: str | None = None, now: datetime | None = None) -> None:
        now = now or utcnow()
        d = self.get_source(source)
        d["last_attempt_utc"] = iso(now)
        d["total_checks"] += 1
        if ok:
            d["last_ok_utc"] = iso(now)
            d["consecutive_failures"] = 0
            d["last_error"] = None
        else:
            d["consecutive_failures"] += 1
            d["total_failures"] += 1
            d["last_error"] = (error or "unknown error")[:300]
        self._save_source(d)

    def set_validators(self, source: str, etag=None, last_modified=None, content_hash=None) -> None:
        d = self.get_source(source)
        if etag is not None:
            d["etag"] = etag
        if last_modified is not None:
            d["last_modified"] = last_modified
        if content_hash is not None:
            d["content_hash"] = content_hash
        self._save_source(d)

    def set_next_allowed(self, source: str, when: datetime | None) -> None:
        d = self.get_source(source)
        d["next_allowed_utc"] = iso(when) if when else None
        self._save_source(d)

    def all_sources(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.conn.execute("SELECT * FROM source_status ORDER BY source")]

    # ---- posts -----------------------------------------------------
    def upsert_post(self, source, post_id, url, published_utc, text, text_hash, classification, now=None) -> str:
        """Returns 'new', 'edited' or 'same'."""
        now = now or utcnow()
        with self._lock:
            row = self.conn.execute("SELECT text_hash FROM posts WHERE source=? AND post_id=?", (source, post_id)).fetchone()
            if row is None:
                self.conn.execute("INSERT INTO posts VALUES(?,?,?,?,?,?,?,?)",
                                  (source, post_id, url, published_utc, iso(now), text, text_hash, classification))
                return "new"
            if row["text_hash"] != text_hash:
                self.conn.execute("UPDATE posts SET text=?, text_hash=?, classification=?, url=? WHERE source=? AND post_id=?",
                                  (text, text_hash, classification, url, source, post_id))
                return "edited"
            return "same"

    def text_hash_seen_elsewhere(self, text_hash: str, source: str, post_id: str) -> bool:
        with self._lock:
            row = self.conn.execute("SELECT 1 FROM posts WHERE text_hash=? AND NOT (source=? AND post_id=?) LIMIT 1",
                                    (text_hash, source, post_id)).fetchone()
        return row is not None

    def recent_posts(self, since: datetime, classifications: tuple[str, ...], trusted_sources: set[str]) -> list[dict]:
        q = ",".join("?" * len(classifications))
        with self._lock:
            rows = self.conn.execute(
                f"SELECT * FROM posts WHERE first_seen_utc>=? AND classification IN ({q}) ORDER BY first_seen_utc DESC",
                (iso(since), *classifications)).fetchall()
        return [dict(r) for r in rows if r["source"] in trusted_sources]

    # ---- notifications ---------------------------------------------
    def notification_status(self, alert_key: str, channel: str) -> dict | None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM notifications WHERE alert_key=? AND channel=?", (alert_key, channel)).fetchone()
        return dict(row) if row else None

    def record_notification(self, alert_key: str, channel: str, ok: bool, error: str | None = None, now=None) -> None:
        now = now or utcnow()
        cur = self.notification_status(alert_key, channel)
        attempts = (cur["attempts"] if cur else 0) + 1
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO notifications(alert_key,channel,status,attempts,last_error,created_utc,sent_utc) VALUES(?,?,?,?,?,?,?)",
                (alert_key, channel, "sent" if ok else "failed", attempts, None if ok else (error or "")[:300],
                 cur["created_utc"] if cur else iso(now), iso(now) if ok else (cur or {}).get("sent_utc")))

    def last_alert(self) -> dict | None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM notifications WHERE status='sent' ORDER BY sent_utc DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def channel_health(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        with self._lock:
            for r in self.conn.execute("SELECT channel, status, COUNT(*) n FROM notifications GROUP BY channel, status"):
                out.setdefault(r["channel"], {})[r["status"]] = r["n"]
        return out
