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
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


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
        status, body = _get(port, TILE_5_3_2, TILE_RANGE)
        self.assertEqual(status, 206)
        self.assertEqual(body, self.tile_bytes)
        self.assertEqual(self.upstream.call_count, 1)

        status2, body2 = _get(port, TILE_5_3_2, TILE_RANGE)
        self.assertEqual(status2, 206)
        self.assertEqual(body2, self.tile_bytes)
        # Served from the SQLite cache: no second upstream send.
        self.assertEqual(self.upstream.call_count, 1)

        stats = _stats(port)
        self.assertEqual(stats["cold_misses"], 1)
        self.assertEqual(stats["warm_hits"], 1)

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

    def test_healthz_is_live_and_never_touches_the_tile_cache(self) -> None:
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

    def test_healthz_reports_failure_live_while_tile_cache_stays_warm(self) -> None:
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
