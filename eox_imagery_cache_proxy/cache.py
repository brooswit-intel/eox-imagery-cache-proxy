"""SQLite-backed byte-range cache and persisted cold/warm/probe counters.

A fresh ``Cache(path)`` against a path that doesn't exist yet starts with
zero cached entries and zero counters ("empty-start"). Everything here is
local disk I/O; nothing in this module makes a network call.

Entries are keyed by the exact (tile path, byte range) the real client
requests. Size is bounded: the oldest-touched entries (by ``last_access``)
are evicted once the total cached byte count would exceed ``max_bytes``, so
a long-running proxy can't grow its SQLite file without limit.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class CachedRange:
    status: int
    body: bytes


class Cache:
    """Thread-safe SQLite cache of complete, successful upstream range reads.

    Counters are persisted in the same database so a before/after delta of
    :meth:`stats` is valid across a proxy restart, not just within one
    process's lifetime.
    """

    COUNTER_NAMES = ("cold_misses", "warm_hits", "probe_sends")

    def __init__(self, path: str, max_bytes: int = 2 << 30) -> None:
        self._lock = threading.Lock()
        self._max_bytes = max_bytes
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS entries ("
            " key TEXT PRIMARY KEY,"
            " status INTEGER NOT NULL,"
            " body BLOB NOT NULL,"
            " size INTEGER NOT NULL,"
            " last_access REAL NOT NULL"
            ")"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS counters ("
            " name TEXT PRIMARY KEY,"
            " value INTEGER NOT NULL"
            ")"
        )
        for name in self.COUNTER_NAMES:
            self._conn.execute(
                "INSERT OR IGNORE INTO counters (name, value) VALUES (?, 0)",
                (name,),
            )
        self._conn.commit()

    @staticmethod
    def key(path: str, start: int, end_inclusive: int) -> str:
        return f"{path}:{start}-{end_inclusive}"

    def get(self, key: str) -> CachedRange | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT status, body FROM entries WHERE key = ?", (key,)
            ).fetchone()
            if row is not None:
                self._conn.execute(
                    "UPDATE entries SET last_access = ? WHERE key = ?",
                    (time.time(), key),
                )
                self._conn.commit()
        if row is None:
            return None
        status, body = row
        return CachedRange(status=status, body=bytes(body))

    def put(self, key: str, response: CachedRange) -> None:
        """Store a complete, successful (200/206) range read. Callers must
        never call this for an error, transient, negative, or truncated
        response — this module does not itself filter on status.
        """
        size = len(response.body)
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO entries (key, status, body, size, last_access)"
                " VALUES (?, ?, ?, ?, ?)",
                (key, response.status, response.body, size, time.time()),
            )
            self._evict_locked()
            self._conn.commit()

    def _evict_locked(self) -> None:
        total = self._conn.execute("SELECT COALESCE(SUM(size), 0) FROM entries").fetchone()[0]
        if total <= self._max_bytes:
            return
        rows = self._conn.execute("SELECT key, size FROM entries ORDER BY last_access ASC").fetchall()
        for key, size in rows:
            if total <= self._max_bytes:
                break
            self._conn.execute("DELETE FROM entries WHERE key = ?", (key,))
            total -= size

    def record_cold_miss(self) -> None:
        self._incr("cold_misses")

    def record_warm_hit(self) -> None:
        self._incr("warm_hits")

    def record_probe_send(self) -> None:
        self._incr("probe_sends")

    def _incr(self, name: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE counters SET value = value + 1 WHERE name = ?", (name,)
            )
            self._conn.commit()

    def stats(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute("SELECT name, value FROM counters").fetchall()
            cached_bytes = self._conn.execute(
                "SELECT COALESCE(SUM(size), 0) FROM entries"
            ).fetchone()[0]
            cached_entries = self._conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
        out = {name: value for name, value in rows}
        out["cached_bytes"] = cached_bytes
        out["cached_entries"] = cached_entries
        return out

    def close(self) -> None:
        with self._lock:
            self._conn.close()
