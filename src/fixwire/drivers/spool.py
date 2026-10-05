"""The offline spool (opt-in): encoded requests (already redacted and
compressed: a /v1 path, a content type and a body) are kept in SQLite until
the server accepts them, so events survive network outages and restarts.
For desktop apps, CLIs, devices and anything that runs offline.

Bounded: 1,000 requests, 50 MB and 72 hours, oldest dropped first. Several
processes can share one spool (gunicorn workers): each claims the rows it
loads, and rows of a process that stopped are reclaimed after 10 minutes.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time

from fixwire._core.delivery import Outbound

MAX_ITEMS = 1000
MAX_BYTES = 50 << 20
TTL_SECONDS = 72 * 3600
RECLAIM_AFTER = 600

_SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created REAL NOT NULL,
  path TEXT NOT NULL,
  content_type TEXT NOT NULL,
  body BLOB NOT NULL,
  category TEXT NOT NULL,
  owner INTEGER,
  claimed REAL
)
"""


def default_path(dsn: str) -> str:
    import hashlib

    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "fixwire", hashlib.sha256(dsn.encode()).hexdigest()[:16], "spool.sqlite3")


class Spool:
    def __init__(
        self, path: str, max_items: int = MAX_ITEMS, max_bytes: int = MAX_BYTES, ttl: float = TTL_SECONDS
    ) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.path = path
        self.max_items, self.max_bytes, self.ttl = max_items, max_bytes, ttl
        self._lock = threading.Lock()
        self._db = self._open()

    def _open(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5, check_same_thread=False, isolation_level=None)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        db.execute(_SCHEMA)
        return db

    def reopen(self) -> None:
        """After fork: the child gets its own connection."""
        self._lock = threading.Lock()
        self._db = self._open()

    def put(self, out: Outbound) -> int:
        now = time.time()
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO requests (created, path, content_type, body, category, owner, claimed)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (now, out.path, out.content_type, out.body, out.category, os.getpid(), now),
            )
            self._trim(now)
            return int(cur.lastrowid or 0)

    def delete(self, row_id: int) -> None:
        with self._lock:
            self._db.execute("DELETE FROM requests WHERE id = ?", (row_id,))

    def claim(self, limit: int = 100) -> list[Outbound]:
        """Rows no live process holds, oldest first, now owned by this one."""
        now = time.time()
        pid = os.getpid()
        with self._lock:
            self._trim(now)
            self._db.execute("BEGIN IMMEDIATE")
            try:
                rows = self._db.execute(
                    "SELECT id, path, content_type, body, category FROM requests"
                    " WHERE owner IS NULL OR owner = ? OR claimed < ? ORDER BY id LIMIT ?",
                    (pid, now - RECLAIM_AFTER, limit),
                ).fetchall()
                if rows:
                    marks = ",".join("?" * len(rows))
                    self._db.execute(
                        "UPDATE requests SET owner = ?, claimed = ? WHERE id IN (%s)" % marks,
                        (pid, now, *[r[0] for r in rows]),
                    )
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
        return [Outbound(str(r[1]), str(r[2]), bytes(r[3]), str(r[4]), spool_id=int(r[0])) for r in rows]

    def touch(self) -> None:
        """Keeps this process's claims fresh while it runs."""
        with self._lock:
            self._db.execute("UPDATE requests SET claimed = ? WHERE owner = ?", (time.time(), os.getpid()))

    def count(self) -> int:
        with self._lock:
            return int(self._db.execute("SELECT count(*) FROM requests").fetchone()[0])

    def _trim(self, now: float) -> None:
        self._db.execute("DELETE FROM requests WHERE created < ?", (now - self.ttl,))
        self._db.execute(
            "DELETE FROM requests WHERE id NOT IN (SELECT id FROM requests ORDER BY id DESC LIMIT ?)",
            (self.max_items,),
        )
        total = self._db.execute("SELECT coalesce(sum(length(body)), 0) FROM requests").fetchone()[0]
        while total > self.max_bytes:
            row = self._db.execute("SELECT id, length(body) FROM requests ORDER BY id LIMIT 1").fetchone()
            if row is None:
                break
            self._db.execute("DELETE FROM requests WHERE id = ?", (row[0],))
            total -= row[1]

    def close(self) -> None:
        with self._lock:
            self._db.close()


def open_spool(offline: object, dsn: str) -> Spool | None:
    """Option value → spool: False/None (off), True (default path) or a path."""
    if not offline:
        return None
    return Spool(default_path(dsn) if offline is True else str(offline))
