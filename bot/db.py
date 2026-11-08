"""Stockage SQLite (watchlist, historique, alertes)."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS products(
  id INTEGER PRIMARY KEY, url TEXT UNIQUE NOT NULL, name TEXT, store TEXT,
  threshold REAL, max_price REAL, active INTEGER DEFAULT 1,
  added_at TEXT, next_check TEXT, last_price REAL, currency TEXT,
  fail_count INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS prices(
  id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL, price REAL NOT NULL,
  currency TEXT, anomaly INTEGER DEFAULT 0, ts TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_prices ON prices(product_id, id);
CREATE TABLE IF NOT EXISTS alerts(
  id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL, price REAL NOT NULL, ts TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sources(
  id INTEGER PRIMARY KEY, url TEXT UNIQUE NOT NULL, store TEXT, active INTEGER DEFAULT 1,
  added_at TEXT, next_check TEXT, last_items INTEGER, fail_count INTEGER DEFAULT 0,
  last_found TEXT, auto INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS listing_prices(
  id INTEGER PRIMARY KEY, url TEXT NOT NULL, price REAL NOT NULL, old_price REAL,
  anomaly INTEGER DEFAULT 0, ts TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_listing ON listing_prices(url, id);
CREATE TABLE IF NOT EXISTS discovery_alerts(
  id INTEGER PRIMARY KEY, url TEXT NOT NULL, price REAL NOT NULL, ts TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_disc ON discovery_alerts(url, id);
CREATE TABLE IF NOT EXISTS source_candidates(
  url TEXT PRIMARY KEY, store TEXT, status TEXT, items INTEGER, ts TEXT NOT NULL);
"""


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


@dataclass
class Product:
    id: int
    url: str
    name: str | None
    store: str | None
    threshold: float | None
    max_price: float | None
    active: bool
    last_price: float | None
    currency: str | None
    fail_count: int


@dataclass
class Source:
    id: int
    url: str
    store: str | None
    active: bool
    last_items: int | None
    fail_count: int
    last_found: datetime | None = None
    auto: bool = False


class DB:
    def __init__(self, path: str):
        self.con = sqlite3.connect(path)
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.executescript(SCHEMA)
        for col in ("last_found TEXT", "auto INTEGER DEFAULT 0"):   # migration des anciennes bases
            try:
                self.con.execute(f"ALTER TABLE sources ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass

    @staticmethod
    def _p(r: sqlite3.Row) -> Product:
        return Product(r["id"], r["url"], r["name"], r["store"], r["threshold"],
                       r["max_price"], bool(r["active"]), r["last_price"], r["currency"],
                       r["fail_count"])

    # --- watchlist ---
    def add_product(self, url, store, threshold=None, max_price=None) -> bool:
        """Retourne True si le produit est nouveau (sinon mise à jour des réglages)."""
        exists = self.con.execute("SELECT 1 FROM products WHERE url=?", (url,)).fetchone()
        self.con.execute(
            """INSERT INTO products(url, store, threshold, max_price, added_at)
               VALUES(?,?,?,?,?)
               ON CONFLICT(url) DO UPDATE SET
                 threshold=COALESCE(excluded.threshold, threshold),
                 max_price=COALESCE(excluded.max_price, max_price),
                 active=1, fail_count=0, next_check=NULL""",
            (url, store, threshold, max_price, iso(now())))
        self.con.commit()
        return not exists

    def remove_product(self, url) -> bool:
        r = self.con.execute("SELECT id FROM products WHERE url=?", (url,)).fetchone()
        if not r:
            return False
        pid = r["id"]
        self.con.execute("DELETE FROM prices WHERE product_id=?", (pid,))
        self.con.execute("DELETE FROM alerts WHERE product_id=?", (pid,))
        self.con.execute("DELETE FROM products WHERE id=?", (pid,))
        self.con.commit()
        return True

    def list_products(self) -> list[Product]:
        return [self._p(r) for r in self.con.execute("SELECT * FROM products ORDER BY id")]

    def due(self, limit: int = 50) -> list[Product]:
        rows = self.con.execute(
            """SELECT * FROM products WHERE active=1
               AND (next_check IS NULL OR next_check <= ?)
               ORDER BY next_check IS NOT NULL, next_check ASC LIMIT ?""",
            (iso(now()), limit))
        return [self._p(r) for r in rows]

    # --- historique ---
    def reference_prices(self, pid: int, n: int = 20) -> list[float]:
        """Prix 'normaux' uniquement : les anomalies ne polluent pas la référence."""
        rows = self.con.execute(
            "SELECT price FROM prices WHERE product_id=? AND anomaly=0 ORDER BY id DESC LIMIT ?",
            (pid, n))
        return [r[0] for r in rows]

    def record_price(self, pid, price, currency, anomaly=False):
        self.con.execute(
            "INSERT INTO prices(product_id, price, currency, anomaly, ts) VALUES(?,?,?,?,?)",
            (pid, price, currency, int(anomaly), iso(now())))
        self.con.commit()

    def anomaly_streak(self, pid: int, n: int = 20) -> int:
        rows = self.con.execute(
            "SELECT anomaly FROM prices WHERE product_id=? ORDER BY id DESC LIMIT ?", (pid, n))
        streak = 0
        for (a,) in rows:
            if not a:
                break
            streak += 1
        return streak

    def accept_new_baseline(self, pid: int):
        self.con.execute("UPDATE prices SET anomaly=0 WHERE product_id=?", (pid,))
        self.con.commit()

    # --- état produit ---
    def touch_success(self, pid, name, price, currency, next_check):
        self.con.execute(
            """UPDATE products SET name=COALESCE(?, name), last_price=?,
               currency=COALESCE(?, currency), fail_count=0, next_check=? WHERE id=?""",
            (name or None, price, currency, iso(next_check), pid))
        self.con.commit()

    def touch_failure(self, pid, next_check, count=True) -> int:
        self.con.execute(
            "UPDATE products SET fail_count=fail_count+?, next_check=? WHERE id=?",
            (1 if count else 0, iso(next_check), pid))
        self.con.commit()
        return self.con.execute("SELECT fail_count FROM products WHERE id=?",
                                (pid,)).fetchone()[0]

    def deactivate(self, pid):
        self.con.execute("UPDATE products SET active=0 WHERE id=?", (pid,))
        self.con.commit()

    # --- alertes ---
    def last_alert(self, pid):
        r = self.con.execute(
            "SELECT price, ts FROM alerts WHERE product_id=? ORDER BY id DESC LIMIT 1",
            (pid,)).fetchone()
        return (r["price"], datetime.fromisoformat(r["ts"])) if r else None

    def record_alert(self, pid, price):
        self.con.execute("INSERT INTO alerts(product_id, price, ts) VALUES(?,?,?)",
                         (pid, price, iso(now())))
        self.con.commit()

    def stats(self) -> dict:
        q = self.con.execute
        since = iso(now().replace(hour=0, minute=0, second=0))
        return {
            "active": q("SELECT COUNT(*) FROM products WHERE active=1").fetchone()[0],
            "disabled": q("SELECT COUNT(*) FROM products WHERE active=0").fetchone()[0],
            "prices": q("SELECT COUNT(*) FROM prices").fetchone()[0],
            "alerts_today": q("SELECT COUNT(*) FROM alerts WHERE ts>=?", (since,)).fetchone()[0],
            "sources": q("SELECT COUNT(*) FROM sources WHERE active=1").fetchone()[0],
            "discoveries_today": q("SELECT COUNT(*) FROM discovery_alerts WHERE ts>=?",
                                   (since,)).fetchone()[0],
        }

    def prune(self, days: int = 90):
        self.con.execute("DELETE FROM prices WHERE ts < ?", (iso(now() - timedelta(days=days)),))
        self.con.execute("DELETE FROM listing_prices WHERE ts < ?",
                         (iso(now() - timedelta(days=7)),))
        self.con.execute("DELETE FROM source_candidates WHERE ts < ?",
                         (iso(now() - timedelta(days=30)),))
        self.con.execute("DELETE FROM discovery_alerts WHERE ts < ?",
                         (iso(now() - timedelta(days=30)),))
        self.con.commit()

    # --- sources (pages liste) ---
    @staticmethod
    def _s(r: sqlite3.Row) -> Source:
        found = datetime.fromisoformat(r["last_found"]) if r["last_found"] else None
        return Source(r["id"], r["url"], r["store"], bool(r["active"]), r["last_items"],
                      r["fail_count"], found, bool(r["auto"]))

    def add_source(self, url, store, auto=False) -> bool:
        exists = self.con.execute("SELECT 1 FROM sources WHERE url=?", (url,)).fetchone()
        self.con.execute(
            """INSERT INTO sources(url, store, added_at, auto) VALUES(?,?,?,?)
               ON CONFLICT(url) DO UPDATE SET active=1, fail_count=0, next_check=NULL,
                 auto=CASE WHEN excluded.auto=0 THEN 0 ELSE auto END""",
            (url, store, iso(now()), int(auto)))
        self.con.commit()
        return not exists

    def remove_source(self, url) -> bool:
        cur = self.con.execute("DELETE FROM sources WHERE url=?", (url,))
        self.con.commit()
        return cur.rowcount > 0

    def list_sources(self) -> list[Source]:
        return [self._s(r) for r in self.con.execute("SELECT * FROM sources ORDER BY id")]

    def due_sources(self) -> list[Source]:
        rows = self.con.execute(
            """SELECT * FROM sources WHERE active=1 AND (next_check IS NULL OR next_check <= ?)
               ORDER BY next_check IS NOT NULL, next_check ASC""", (iso(now()),))
        return [self._s(r) for r in rows]

    def source_done(self, sid, next_check, fail=False, items=None) -> int:
        """Planifie le prochain scan. fail=True incrémente le compteur d'échecs."""
        self.con.execute(
            """UPDATE sources SET next_check=?, last_items=COALESCE(?, last_items),
               fail_count=CASE WHEN ?=1 THEN fail_count+1 WHEN ? IS NOT NULL THEN 0
                               ELSE fail_count END WHERE id=?""",
            (iso(next_check), items, int(fail), items, sid))
        self.con.commit()
        return self.con.execute("SELECT fail_count FROM sources WHERE id=?",
                                (sid,)).fetchone()[0]

    def source_mark_found(self, sid):
        self.con.execute("UPDATE sources SET last_found=? WHERE id=?", (iso(now()), sid))
        self.con.commit()

    def deactivate_source(self, sid):
        self.con.execute("UPDATE sources SET active=0 WHERE id=?", (sid,))
        self.con.commit()

    # --- historique des prix vus sur les pages liste ---
    def listing_history(self, url: str, n: int = 10) -> list[float]:
        rows = self.con.execute(
            "SELECT price FROM listing_prices WHERE url=? AND anomaly=0 ORDER BY id DESC LIMIT ?",
            (url, n))
        return [r[0] for r in rows]

    def record_listing(self, url, price, old_price, anomaly=False):
        self.con.execute(
            "INSERT INTO listing_prices(url, price, old_price, anomaly, ts) VALUES(?,?,?,?,?)",
            (url, price, old_price, int(anomaly), iso(now())))

    def listing_streak(self, url: str, n: int = 20) -> int:
        rows = self.con.execute(
            "SELECT anomaly FROM listing_prices WHERE url=? ORDER BY id DESC LIMIT ?", (url, n))
        streak = 0
        for (a,) in rows:
            if not a:
                break
            streak += 1
        return streak

    def listing_accept(self, url: str):
        self.con.execute("UPDATE listing_prices SET anomaly=0 WHERE url=?", (url,))

    def discovery_recent(self, url: str, seconds: int) -> bool:
        r = self.con.execute(
            "SELECT ts FROM discovery_alerts WHERE url=? ORDER BY id DESC LIMIT 1",
            (url,)).fetchone()
        return bool(r) and (now() - datetime.fromisoformat(r["ts"])).total_seconds() < seconds

    def record_discovery(self, url, price):
        self.con.execute("INSERT INTO discovery_alerts(url, price, ts) VALUES(?,?,?)",
                         (url, price, iso(now())))
        self.con.commit()

    def commit(self):
        self.con.commit()

    # --- découverte automatique ---
    def has_source(self, url: str) -> bool:
        return bool(self.con.execute("SELECT 1 FROM sources WHERE url=?", (url,)).fetchone())

    def count_auto_sources(self, store: str) -> int:
        return self.con.execute(
            "SELECT COUNT(*) FROM sources WHERE auto=1 AND active=1 AND store=?",
            (store,)).fetchone()[0]

    def candidate_recent(self, url: str, days: int = 7) -> bool:
        r = self.con.execute("SELECT ts FROM source_candidates WHERE url=?", (url,)).fetchone()
        return bool(r) and (now() - datetime.fromisoformat(r["ts"])).days < days

    def candidate_set(self, url, store, status, items):
        self.con.execute(
            """INSERT INTO source_candidates(url, store, status, items, ts) VALUES(?,?,?,?,?)
               ON CONFLICT(url) DO UPDATE SET status=excluded.status, items=excluded.items,
                 ts=excluded.ts""", (url, store, status, items, iso(now())))
        self.con.commit()

    def deactivate_weak_auto(self, min_items: int) -> int:
        cur = self.con.execute(
            "UPDATE sources SET active=0 WHERE auto=1 AND active=1 "
            "AND last_items IS NOT NULL AND last_items < ?", (min_items,))
        self.con.commit()
        return cur.rowcount
