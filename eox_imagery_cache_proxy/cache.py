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

import re
import sqlite3
import threading
import time
from dataclasses import dataclass

_CONTENT_RANGE = re.compile(r"^bytes (\d+)-(\d+)/(\d+|\*)$")


def content_range_matches_body(content_range: str | None, body_len: int) -> bool:
    """Validates that a 206's ``Content-Range`` is both syntactically
    well-formed (RFC 7233 ``bytes <start>-<end>/<total>``) and actually
    describes the body bytes received or stored — not merely present.

    The real client only ever sends ``Range: bytes=0-1048575`` (offset
    zero), so a genuine, trustworthy answer must start at 0 and its
    advertised length (``end - start + 1``) must equal the body length
    exactly; anything else — missing entirely, a mismatched offset, a
    declared length that doesn't match the actual bytes, an unparseable
    header — is malformed and must never be served or replayed as though
    it were a real partial response. Nothing here guesses or synthesizes a
    byte range; it only validates one that's already present.
    """
    if content_range is None:
        return False
    match = _CONTENT_RANGE.match(content_range)
    if match is None:
        return False
    start, end, total = match.groups()
    start, end = int(start), int(end)
    if start != 0 or end < start:
        return False
    if end - start + 1 != body_len:
        return False
    if total != "*" and int(total) < body_len:
        return False
    return True


@dataclass(frozen=True)
class CachedRange:
    status: int
    body: bytes
    content_range: str | None = None


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
        existing_columns = {row[1] for row in self._conn.execute("PRAGMA table_info(entries)")}
        if "content_range" not in existing_columns:
            self._conn.execute("ALTER TABLE entries ADD COLUMN content_range TEXT")
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
                "SELECT status, body, content_range FROM entries WHERE key = ?", (key,)
            ).fetchone()
            if row is None:
                return None
            status, body, content_range = row
            if status == 206 and not content_range_matches_body(content_range, len(body)):
                # Either a row written before the content_range column
                # existed (backfilled NULL), or — in principle, from any
                # bug that let a present-but-mismatched header slip past a
                # write-time check — a cached entry whose Content-Range
                # doesn't actually describe its own stored body. Either way
                # this is exactly the malformed-206 shape the proxy must
                # never serve. Never replay it — invalidate it here and
                # report a miss so the caller re-fetches it properly from
                # upstream, the same path a genuine cold miss takes. No
                # byte range is guessed; the stale row is simply discarded.
                self._conn.execute("DELETE FROM entries WHERE key = ?", (key,))
                self._conn.commit()
                return None
            self._conn.execute(
                "UPDATE entries SET last_access = ? WHERE key = ?",
                (time.time(), key),
            )
            self._conn.commit()
        return CachedRange(status=status, body=bytes(body), content_range=content_range)

    def put(self, key: str, response: CachedRange) -> None:
        """Store a complete, successful (200/206) range read. Callers must
        never call this for an error, transient, negative, or truncated
        response — this module does not itself filter on status.
        """
        size = len(response.body)
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO entries (key, status, body, size, last_access, content_range)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (key, response.status, response.body, size, time.time(), response.content_range),
            )
            self._evict_locked()
            self._conn.commit()

    def delete_prefix(self, path_prefix: str) -> int:
        """Explicitly invalidates every cached entry whose key starts with
        ``path_prefix`` (a tile path, e.g. ``/5/3/2.jpg``). Returns the
        number of entries removed. Data has no time-based expiration
        (TTL -1), so this is the only way a stale entry ever leaves the
        cache short of eviction.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT key FROM entries WHERE key LIKE ? ESCAPE '\\'",
                (path_prefix.replace("%", "\\%").replace("_", "\\_") + "%",),
            ).fetchall()
            for (key,) in rows:
                self._conn.execute("DELETE FROM entries WHERE key = ?", (key,))
            self._conn.commit()
        return len(rows)

    def clear(self) -> int:
        """Explicitly invalidates every cached entry. Returns the number removed."""
        with self._lock:
            count = self._conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
            self._conn.execute("DELETE FROM entries")
            self._conn.commit()
        return count

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
