from __future__ import annotations

import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from eox_imagery_cache_proxy.server import Config, ProxyServer
from eox_imagery_cache_proxy.cache import Cache

from fake_upstream import FakeUpstream

TILE_0_0_0 = "/0/0/0.jpg"
TILE_5_3_2 = "/5/3/2.jpg"
TILE_RANGE_MAX_BYTES = 1 << 20  # matches server.MAX_TILE_BYTES
TILE_RANGE = "bytes=0-1048575"


def _get(port: int, path: str, range_header: str | None) -> tuple[int, bytes]:
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="GET")
    if range_header is not None:
        req.add_header("Range", range_header)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _stats(port: int) -> dict:
    import json

    with urllib.request.urlopen(f"http://127.0.0.1:{port}/_internal/stats", timeout=5) as resp:
        return json.loads(resp.read())


def _delete(port: int, path: str) -> tuple[int, dict]:
    import json

    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _health(port: int) -> tuple[int, dict]:
    import json

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/__health", timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _get_with_headers(port: int, path: str, range_header: str | None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="GET")
    if range_header is not None:
        req.add_header("Range", range_header)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


def _health_head(port: int) -> int:
    req = urllib.request.Request(f"http://127.0.0.1:{port}/__health", method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


class ProxyTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.sqlite_path = os.path.join(self.tmpdir.name, "cache.sqlite3")
        self.tile_bytes = bytes(range(256)) * 4  # 1024 bytes, deterministic, well under 1 MiB
        self.upstream = FakeUpstream({TILE_0_0_0: self.tile_bytes, TILE_5_3_2: self.tile_bytes}).start()
        self.config = Config(
            bind_host="127.0.0.1",
            bind_port=0,
            sqlite_path=self.sqlite_path,
            upstream_base=self.upstream.base_url,
        )

    def _start_server(self, config: Config | None = None) -> ProxyServer:
        config = config or self.config
        cache = Cache(config.sqlite_path, max_bytes=config.max_cache_bytes)
        server = ProxyServer(config, cache)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        return server

    def tearDown(self) -> None:
        self.upstream.stop()
        self.tmpdir.cleanup()

    def test_start_empty(self) -> None:
        server = self._start_server()
        stats = _stats(server.server_address[1])
        self.assertEqual(stats["cold_misses"], 0)
        self.assertEqual(stats["warm_hits"], 0)
        self.assertEqual(stats["cached_entries"], 0)
        self.assertIn("EOxCloudless", stats["attribution"])

    def test_cold_then_warm(self) -> None:
        server = self._start_server()
        port = server.server_address[1]
        status, body, headers = _get_with_headers(port, TILE_5_3_2, TILE_RANGE)
        self.assertEqual(status, 206)
        self.assertEqual(body, self.tile_bytes)
        # A 206 must carry a real Content-Range, matching the exact range
        # requested — bug found live via the Zippy demo (MINECRAFT-579
        # comment 55976): a 206 with no/mismatched Content-Range is
        # malformed per RFC 7233 and the real client (RangeFiles) rejects
        # it as corrupt rather than accepting a bare partial body.
        self.assertEqual(headers.get("Content-Range"), f"bytes 0-1023/{len(self.tile_bytes)}")
        self.assertEqual(self.upstream.call_count, 1)

        status2, body2, headers2 = _get_with_headers(port, TILE_5_3_2, TILE_RANGE)
        self.assertEqual(status2, 206)
        self.assertEqual(body2, self.tile_bytes)
        self.assertEqual(headers2.get("Content-Range"), f"bytes 0-1023/{len(self.tile_bytes)}")
        # Served from the SQLite cache: no second upstream send.
        self.assertEqual(self.upstream.call_count, 1)

        stats = _stats(port)
        self.assertEqual(stats["cold_misses"], 1)
        self.assertEqual(stats["warm_hits"], 1)

    def test_malformed_206_missing_content_range_is_rejected_and_not_cached(self) -> None:
        # Reproduces the live Zippy-demo bug (MINECRAFT-579 comment 55976):
        # an upstream 206 with no Content-Range header at all must never be
        # forwarded to the client as a bare 206 (that is itself malformed
        # per RFC 7233, and the real client rejects it), and must never be
        # cached as a successful read — it is retryable, same as any other
        # incomplete response.
        self.upstream.omit_content_range = True
        server = self._start_server()
        port = server.server_address[1]
        status, body = _get(port, TILE_5_3_2, TILE_RANGE)
        self.assertEqual(status, 502)
        self.assertEqual(self.upstream.call_count, 1)
        self.assertEqual(_stats(port)["cached_entries"], 0)

        # Not cached: a repeat request goes upstream again, not served
        # (warm) as a bogus success.
        self.upstream.omit_content_range = False
        status2, body2, headers2 = _get_with_headers(port, TILE_5_3_2, TILE_RANGE)
        self.assertEqual(status2, 206)
        self.assertEqual(body2, self.tile_bytes)
        self.assertIsNotNone(headers2.get("Content-Range"))
        self.assertEqual(self.upstream.call_count, 2)
        self.assertEqual(_stats(port)["warm_hits"], 0)

    def test_legitimate_short_whole_file_200_jpeg_needs_no_content_range(self) -> None:
        # A complete, valid whole-file 200 response (the upstream ignoring
        # Range, or a client-side plain GET) carries no Content-Range at
        # all by design -- 200 means "here is the whole thing," not a
        # partial -- and must still be served and cached correctly.
        self.upstream.ignore_range = True
        server = self._start_server()
        port = server.server_address[1]
        status, body, headers = _get_with_headers(port, TILE_5_3_2, TILE_RANGE)
        self.assertEqual(status, 200)
        self.assertEqual(body, self.tile_bytes)
        self.assertIsNone(headers.get("Content-Range"))
        self.assertEqual(_stats(port)["cached_entries"], 1)

        calls_before = self.upstream.call_count
        status2, body2, headers2 = _get_with_headers(port, TILE_5_3_2, TILE_RANGE)
        self.assertEqual(status2, 200)
        self.assertEqual(body2, self.tile_bytes)
        self.assertIsNone(headers2.get("Content-Range"))
        self.assertEqual(self.upstream.call_count, calls_before)

    def test_restart_keeps_warm_cache(self) -> None:
        server = self._start_server()
        port = server.server_address[1]
        _get(port, TILE_5_3_2, TILE_RANGE)
        server.shutdown()
        server.server_close()

        # A fresh process against the same SQLite file would see this; here
        # a fresh ProxyServer/Cache object simulates the restart without an
        # actual process relaunch.
        second = self._start_server()
        port2 = second.server_address[1]
        status, body = _get(port2, TILE_5_3_2, TILE_RANGE)
        self.assertEqual(status, 206)
        self.assertEqual(body, self.tile_bytes)
        # Still just the one call from before the "restart".
        self.assertEqual(self.upstream.call_count, 1)
        self.assertEqual(_stats(port2)["warm_hits"], 1)

    def test_error_response_is_never_cached(self) -> None:
        server = self._start_server()
        port = server.server_address[1]
        self.upstream.fail_next = True
        status, _ = _get(port, TILE_5_3_2, TILE_RANGE)
        # The real upstream status (503) is forwarded through unmodified,
        # not remapped — only genuinely unreachable (no response at all)
        # becomes this proxy's own 502.
        self.assertEqual(status, 503)
        self.assertEqual(self.upstream.call_count, 1)

        # Retried: not served from a bogus cached failure, goes upstream again.
        status2, body2 = _get(port, TILE_5_3_2, TILE_RANGE)
        self.assertEqual(status2, 206)
        self.assertEqual(body2, self.tile_bytes)
        self.assertEqual(self.upstream.call_count, 2)
        self.assertEqual(_stats(port)["warm_hits"], 0)

    def test_genuinely_missing_tile_is_not_cached_as_success(self) -> None:
        # Not requested from the fake upstream's tile set: a real 404, never
        # cached as success, and the client's own missing-vs-transient
        # distinction is preserved.
        server = self._start_server()
        port = server.server_address[1]
        status, _ = _get(port, "/9/1/1.jpg", TILE_RANGE)
        self.assertEqual(status, 404)
        self.assertEqual(self.upstream.call_count, 1)
        self.assertEqual(_stats(port)["cached_entries"], 0)

    def test_path_outside_the_allowlisted_shape_is_rejected_before_upstream(self) -> None:
        server = self._start_server()
        port = server.server_address[1]
        for bad_path in ("/../../etc/passwd", "/5/3/2.png", "/99/0/0.jpg", "/5/32/2.jpg"):
            status, _ = _get(port, bad_path, TILE_RANGE)
            self.assertEqual(status, 404, bad_path)
        self.assertEqual(self.upstream.call_count, 0)

    def test_concurrent_miss_is_deduplicated(self) -> None:
        server = self._start_server()
        port = server.server_address[1]
        results = []

        def fetch():
            results.append(_get(port, TILE_5_3_2, TILE_RANGE))

        threads = [threading.Thread(target=fetch) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        self.assertEqual(len(results), 8)
        for status, body in results:
            self.assertEqual(status, 206)
            self.assertEqual(body, self.tile_bytes)
        # Eight concurrent identical misses collapse into one upstream send.
        self.assertEqual(self.upstream.call_count, 1)

    def test_health_endpoint_is_live_and_never_touches_the_tile_cache(self) -> None:
        server = self._start_server()
        port = server.server_address[1]

        status, body = _health(port)
        self.assertEqual(status, 200)
        self.assertEqual(body["upstream"], "reachable")
        self.assertEqual(self.upstream.call_count, 1)
        self.assertEqual(_stats(port)["probe_sends"], 1)
        # A probe must never populate, or be satisfied by, the tile cache.
        self.assertEqual(_stats(port)["cached_entries"], 0)

        # A second probe still goes live (TTL 0 — never infinite freshness).
        status2, _ = _health(port)
        self.assertEqual(status2, 200)
        self.assertEqual(self.upstream.call_count, 2)

        # An ordinary tile read for the same tile is cached separately and
        # does not affect the probe counter.
        status3, _ = _get(port, TILE_0_0_0, TILE_RANGE)
        self.assertEqual(status3, 206)
        self.assertEqual(self.upstream.call_count, 3)
        self.assertEqual(_stats(port)["cold_misses"], 1)
        self.assertEqual(_stats(port)["probe_sends"], 2)

    def test_health_endpoint_reports_failure_live_while_tile_cache_stays_warm(self) -> None:
        server = self._start_server()
        port = server.server_address[1]
        # Warm the ordinary tile cache first.
        status, _ = _get(port, TILE_0_0_0, TILE_RANGE)
        self.assertEqual(status, 206)

        self.upstream.down = True
        health_status, health_body = _health(port)
        self.assertEqual(health_status, 502)
        self.assertEqual(health_body["upstream"], "unreachable")

        # The already-warmed tile is unaffected: still served from SQLite,
        # no new upstream call for it.
        calls_before = self.upstream.call_count
        status2, body2 = _get(port, TILE_0_0_0, TILE_RANGE)
        self.assertEqual(status2, 206)
        self.assertEqual(body2, self.tile_bytes)
        self.assertEqual(self.upstream.call_count, calls_before)

        self.upstream.down = False
        health_status2, health_body2 = _health(port)
        self.assertEqual(health_status2, 200)
        self.assertEqual(health_body2["upstream"], "reachable")


    def test_good_hit_survives_restart_and_large_clock_advance(self) -> None:
        import time as time_mod

        server = self._start_server()
        port = server.server_address[1]
        status, body = _get(port, TILE_5_3_2, TILE_RANGE)
        self.assertEqual(status, 206)
        self.assertEqual(body, self.tile_bytes)
        calls_after_warm = self.upstream.call_count

        # TTL -1: no time-based expiration. A huge forward clock jump must
        # not invalidate the entry.
        real_time = time_mod.time
        time_mod.time = lambda: real_time() + 10_000_000
        try:
            status2, body2 = _get(port, TILE_5_3_2, TILE_RANGE)
        finally:
            time_mod.time = real_time
        self.assertEqual(status2, 206)
        self.assertEqual(body2, self.tile_bytes)
        self.assertEqual(self.upstream.call_count, calls_after_warm)

        # And a restart (fresh Cache/ProxyServer against the same file).
        server.shutdown()
        server.server_close()
        second = self._start_server()
        port2 = second.server_address[1]
        status3, body3 = _get(port2, TILE_5_3_2, TILE_RANGE)
        self.assertEqual(status3, 206)
        self.assertEqual(body3, self.tile_bytes)
        self.assertEqual(self.upstream.call_count, calls_after_warm)

    def test_explicit_invalidation_forces_a_fresh_upstream_read(self) -> None:
        server = self._start_server()
        port = server.server_address[1]
        _get(port, TILE_5_3_2, TILE_RANGE)
        self.assertEqual(_stats(port)["cached_entries"], 1)

        status, body = _delete(port, TILE_5_3_2)
        self.assertEqual(status, 200)
        self.assertEqual(body["invalidated"], 1)
        self.assertEqual(_stats(port)["cached_entries"], 0)

        # No TTL, no eviction triggered it — only the explicit DELETE did.
        # The next identical read is a genuine cold miss again.
        calls_before = self.upstream.call_count
        status2, body2 = _get(port, TILE_5_3_2, TILE_RANGE)
        self.assertEqual(status2, 206)
        self.assertEqual(body2, self.tile_bytes)
        self.assertEqual(self.upstream.call_count, calls_before + 1)

    def test_health_endpoint_also_answers_head_with_status_only(self) -> None:
        # The real client (EarthStatus.reach()) probes with HEAD, expecting
        # exactly this proxy's own status and no body.
        server = self._start_server()
        port = server.server_address[1]

        status = _health_head(port)
        self.assertEqual(status, 200)
        self.assertEqual(self.upstream.call_count, 1)
        self.assertEqual(_stats(port)["probe_sends"], 1)

        self.upstream.down = True
        status2 = _health_head(port)
        self.assertEqual(status2, 502)

    def test_range_ignoring_oversized_response_is_bounded_and_not_cached(self) -> None:
        # An origin that ignores Range and returns far more than the
        # requested/expected size must never be buffered in full, and must
        # never be cached as a successful read.
        oversized_path = "/9/1/1.jpg"
        huge = b"x" * (TILE_RANGE_MAX_BYTES + 4096)
        self.upstream.tiles[oversized_path] = huge
        self.upstream.ignore_range = True

        server = self._start_server()
        port = server.server_address[1]
        status, body = _get(port, oversized_path, TILE_RANGE)
        self.assertEqual(status, 200)
        # Bounded, not the full oversized body.
        self.assertLessEqual(len(body), TILE_RANGE_MAX_BYTES)
        # Never cached: a repeat identical request goes upstream again.
        calls_before = self.upstream.call_count
        self.assertEqual(_stats(port)["cached_entries"], 0)
        status2, _ = _get(port, oversized_path, TILE_RANGE)
        self.assertEqual(status2, 200)
        self.assertGreater(self.upstream.call_count, calls_before)

    def test_legacy_pre_migration_headerless_206_row_is_invalidated_not_replayed(self) -> None:
        # Reproduces John's follow-up on the live fix (MINECRAFT-579 comment
        # 55979): a Zippy SQLite predating the content_range column has
        # existing 206 rows with no header at all. The migration adds the
        # column as nullable, so Cache.get() must treat that legacy shape
        # as invalid and transparently re-fetch -- never hand a status-206,
        # content_range=None CachedRange to the response writer (which
        # asserts a real header is present on any 206 it serves).
        import sqlite3

        # Build the pre-migration schema by hand: exactly what main looked
        # like before this column existed, with one legacy headerless 206
        # row (the exact bug shape) and one ordinary legacy 200 row (never
        # needed a Content-Range, must survive untouched).
        legacy_db = sqlite3.connect(self.sqlite_path)
        legacy_db.execute(
            "CREATE TABLE entries ("
            " key TEXT PRIMARY KEY, status INTEGER NOT NULL, body BLOB NOT NULL,"
            " size INTEGER NOT NULL, last_access REAL NOT NULL)"
        )
        legacy_db.execute(
            "CREATE TABLE counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL)"
        )
        for name in Cache.COUNTER_NAMES:
            legacy_db.execute("INSERT INTO counters (name, value) VALUES (?, 0)", (name,))
        legacy_key = Cache.key(TILE_5_3_2, 0, 1048575)
        legacy_db.execute(
            "INSERT INTO entries (key, status, body, size, last_access) VALUES (?, ?, ?, ?, ?)",
            (legacy_key, 206, self.tile_bytes, len(self.tile_bytes), 1.0),
        )
        legit_200_key = Cache.key(TILE_0_0_0, 0, 1048575)
        legacy_db.execute(
            "INSERT INTO entries (key, status, body, size, last_access) VALUES (?, ?, ?, ?, ?)",
            (legit_200_key, 200, self.tile_bytes, len(self.tile_bytes), 1.0),
        )
        legacy_db.commit()
        legacy_db.close()

        server = self._start_server()  # opens the legacy file; migrates the column
        port = server.server_address[1]

        # The legacy headerless 206 must not be replayed (no 500, no bare
        # 206) -- it is invalidated and re-fetched exactly like a genuine
        # cold miss, landing a correct, fully-headered 206 this time.
        status, body, headers = _get_with_headers(port, TILE_5_3_2, TILE_RANGE)
        self.assertEqual(status, 206)
        self.assertEqual(body, self.tile_bytes)
        self.assertIsNotNone(headers.get("Content-Range"))
        self.assertEqual(self.upstream.call_count, 1)

        # A repeat is now warm from the freshly-written, correct entry.
        status2, body2, headers2 = _get_with_headers(port, TILE_5_3_2, TILE_RANGE)
        self.assertEqual(status2, 206)
        self.assertIsNotNone(headers2.get("Content-Range"))
        self.assertEqual(self.upstream.call_count, 1)

        # The untouched legacy 200 entry is still served warm, with zero
        # upstream calls for it -- the migration/invalidation logic only
        # ever touches the specific malformed-206 shape, nothing else.
        status3, body3 = _get(port, TILE_0_0_0, TILE_RANGE)
        self.assertEqual(status3, 200)
        self.assertEqual(body3, self.tile_bytes)
        self.assertEqual(self.upstream.call_count, 1)

    def test_clear_all_invalidates_every_entry(self) -> None:
        server = self._start_server()
        port = server.server_address[1]
        _get(port, TILE_5_3_2, TILE_RANGE)
        _get(port, TILE_0_0_0, TILE_RANGE)
        self.assertEqual(_stats(port)["cached_entries"], 2)

        status, body = _delete(port, "/_internal/cache")
        self.assertEqual(status, 200)
        self.assertEqual(body["invalidated"], 2)
        self.assertEqual(_stats(port)["cached_entries"], 0)


if __name__ == "__main__":
    unittest.main()
