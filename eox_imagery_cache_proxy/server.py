"""HTTP server: routes EOX Sentinel-2 cloudless imagery tile reads through
the SQLite cache, forwarding only cache misses to the fixed upstream.

Native request contract reproduced from the real client (Earth Platform
alpha.73 / combined-73, commit 88b13c33954550c6ac660dceb743a4288a9aafac,
``io.earthplatform.geography.globe.ImagerySource`` + ``RangeFiles``):

- Dataset ``s2cloudless-2016-v1``: the 2016 EOX Sentinel-2 cloudless vintage
  specifically, the one vintage under CC BY 4.0 (later vintages are
  non-commercial-only CC BY-NC-SA and are never requested by this client —
  ``ImagerySource.EOX_S2_CLOUDLESS_2016`` is the only base URI the mod
  constructs). Caching and redistribution is permitted under CC BY 4.0
  attribution; there is no terms blocker. Required attribution text is
  reproduced verbatim from the mod's own ``ImagerySource.ATTRIBUTION``
  constant and never stripped: see ``ATTRIBUTION`` below.
- Fixed origin: ``https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless_3857/default/g/``.
- ``GET <zoom>/<row>/<col>.jpg`` (WMTS TileMatrix/TileRow/TileCol order,
  i.e. ``z/y/x.jpg`` — not ``z/x/y``), zoom 0-14 (``ImagerySource.MAX_ZOOM``),
  row/col bounded to ``[0, 2**zoom)``. There is no query string.
- Every read sends an exact ``Range: bytes=0-1048575`` header (offset zero,
  length ``MAX_TILE_BYTES`` = 1 MiB — ``RangeFiles.fetch(uri, 0,
  MAX_TILE_BYTES)``), never a whole-file GET and never any other range
  shape. No conditional request (If-None-Match / If-Modified-Since) is ever
  sent by this client, so this proxy does not implement 304 handling.
- The client treats HTTP 404/403 as "no imagery here" (never retried as
  transient) and 200/206 as success — including the upstream's own tiny
  placeholder image for open-sea tiles, which is a genuine 2xx body and is
  cached like any other successful tile. This proxy forwards status and
  body through unmodified and lets the client keep making that distinction.
- Cached tile data has TTL -1 (no time-based expiration) per the
  cross-lane cache-lifetime direction (MINECRAFT-571 comment 55740): a
  good tile survives a restart and an arbitrary clock advance with zero
  further upstream calls. This is bounded by the byte-cap eviction below,
  not by time, and remains explicitly invalidatable (``DELETE <tile
  path>``, removing every cached range for that path) — never by a
  background expiry. Failures, errors, and truncated reads are already
  excluded from the cache entirely (never written in the first place), so
  there is nothing time-based to expire there either.
- A dedicated upstream health-check endpoint (``GET``/``HEAD /__health``)
  is exposed per the cross-lane direction (MINECRAFT-571 comments
  55734/55737/55789/55808/55816): TTL 0, always a live round trip,
  bypassing the tile cache entirely (never read from or written to it),
  counted separately (``probe_sends``) so it never inflates or deflates
  the ordinary cold/warm tile counters. It issues a bounded ``Range:
  bytes=0-0`` read of a fixed representative tile (``0/0/0.jpg``, always
  present at the minimum zoom) against the real upstream, treating either
  ``200`` or ``206`` from THAT read as healthy (both are genuine
  successful answers to a Range request). The real client
  (``EarthStatus.reach()``) probes with a plain, no-Range ``HEAD`` against
  THIS endpoint and requires exactly this proxy's own ``200`` to count as
  healthy — ``HEAD`` therefore answers with just that status and no body;
  ``GET`` answers the same status with a JSON body. The game never calls
  ``GET /__health`` itself.
"""

from __future__ import annotations

import http.client
import os
import re
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, urlunsplit

from .cache import Cache, CachedRange

MAX_ZOOM = 14
MAX_TILE_BYTES = 1 << 20

_TILE_PATH = re.compile(r"^/(\d+)/(\d+)/(\d+)\.jpg$")

ATTRIBUTION = "EOxCloudless - https://cloudless.eox.at by EOX IT Services GmbH (Contains modified Copernicus Sentinel data 2016)"

_HEALTH_PROBE_PATH = "/0/0/0.jpg"
_HEALTH_PROBE_RANGE = "bytes=0-0"


class RangeNotSatisfiable(Exception):
    pass


def _parse_range(header: str | None) -> tuple[int, int]:
    """Parses ``bytes=<start>-<end>`` (end inclusive), requiring the exact
    shape this client always sends: bounded, starting at offset zero.
    """
    if not header or not header.startswith("bytes="):
        raise RangeNotSatisfiable(header)
    spec = header[len("bytes="):]
    if "," in spec:
        raise RangeNotSatisfiable(header)
    start_s, _, end_s = spec.partition("-")
    if not start_s or not end_s:
        raise RangeNotSatisfiable(header)
    try:
        start, end = int(start_s), int(end_s)
    except ValueError as exc:
        raise RangeNotSatisfiable(header) from exc
    if start != 0 or end < start:
        raise RangeNotSatisfiable(header)
    return start, end


def _validate_tile_path(path: str) -> tuple[int, int, int] | None:
    """Returns (zoom, row, col) for a well-formed, in-bounds tile path, or
    None — this proxy never forwards an arbitrary path to the upstream.
    """
    match = _TILE_PATH.match(path)
    if match is None:
        return None
    zoom, row, col = (int(g) for g in match.groups())
    if zoom < 0 or zoom > MAX_ZOOM:
        return None
    n = 1 << zoom
    if not (0 <= row < n) or not (0 <= col < n):
        return None
    return zoom, row, col


class Config:
    def __init__(
        self,
        bind_host: str = "127.0.0.1",
        bind_port: int = 8836,
        sqlite_path: str = "./eox-imagery-cache.sqlite3",
        upstream_base: str = "https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless_3857/default/g",
        max_cache_bytes: int = 2 << 30,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.bind_host = bind_host
        self.bind_port = bind_port
        self.sqlite_path = sqlite_path
        self.upstream_base = upstream_base.rstrip("/")
        self.max_cache_bytes = max_cache_bytes
        self.timeout_seconds = timeout_seconds

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            bind_host=os.environ.get("PROXY_BIND_HOST", "127.0.0.1"),
            bind_port=int(os.environ.get("PROXY_BIND_PORT", "8836")),
            sqlite_path=os.environ.get("PROXY_SQLITE_PATH", "./eox-imagery-cache.sqlite3"),
            upstream_base=os.environ.get(
                "EOX_UPSTREAM_BASE",
                "https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless_3857/default/g",
            ),
            max_cache_bytes=int(os.environ.get("PROXY_MAX_CACHE_BYTES", str(2 << 30))),
            timeout_seconds=float(os.environ.get("PROXY_UPSTREAM_TIMEOUT_SECONDS", "30")),
        )


_HEALTH_PROBE_MAX_BYTES = 1 << 16  # 64 KiB: generous for a 1-byte range read even if the
# origin ignores Range and returns a small whole tile; still bounds memory if it doesn't.


def _forward(config: Config, path: str, range_header: str, max_body_bytes: int = MAX_TILE_BYTES):
    """Makes one actual network send to the fixed upstream for one exact byte
    range of one validated tile path (or the fixed health-probe path).

    Always returns a 3-tuple and never raises for a network/transport
    failure — every way the send can go wrong is folded into the return
    value so the caller has exactly one place to decide whether anything is
    cacheable.

    Reads are capped at ``max_body_bytes + 1``: an origin that ignores the
    Range header and returns far more than requested (or an error page
    larger than expected) is detected and never buffered in full — status
    and whatever was read up to the cap are still returned, but ``complete``
    is False, so an oversized response is treated the same as a truncated
    one and never cached as success.

    Returns (status, body, complete):
    - ``status`` is 0 if no response was ever received at all.
    - ``complete`` is False whenever the response must not be cached as
      success: a non-200/206 status, a declared length that the actually
      delivered body didn't match (a genuine mid-transfer truncation), or a
      body that exceeded ``max_body_bytes``.
    """
    base = urlsplit(config.upstream_base)
    url = urlunsplit((base.scheme, base.netloc, base.path + path, "", ""))
    req = urllib.request.Request(
        url,
        headers={"Range": range_header, "User-Agent": "eox-imagery-cache-proxy/0.1"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=config.timeout_seconds) as resp:
            expected_length = resp.headers.get("Content-Length")
            try:
                raw = resp.read(max_body_bytes + 1)
            except (http.client.HTTPException, OSError):
                return resp.status, b"", False
            if len(raw) > max_body_bytes:
                return resp.status, raw[:max_body_bytes], False
            complete = expected_length is None or len(raw) == int(expected_length)
            return resp.status, raw, complete
    except urllib.error.HTTPError as exc:
        # 404/403 land here too: a real, complete answer from the upstream,
        # just not a 2xx one. Never cached as success; the status is
        # forwarded through unmodified so the client keeps making its own
        # missing-vs-transient distinction. Still bounded — an oversized
        # error body is truncated to the cap rather than read in full.
        body = exc.read(max_body_bytes + 1)
        return exc.code, body[:max_body_bytes], False
    except (urllib.error.URLError, OSError):
        # Covers both "never connected at all" (URLError: refused/DNS/
        # timeout before any bytes) and "connected but the peer dropped us
        # before a status line arrived" (e.g. http.client.RemoteDisconnected,
        # an OSError subclass) — neither is a real HTTP response.
        return 0, b"", False


def make_handler(cache: Cache, config: Config):
    locks_guard = threading.Lock()
    inflight: dict[str, threading.Lock] = {}

    def singleflight_fetch(key: str, path: str, range_header: str):
        with locks_guard:
            lock = inflight.setdefault(key, threading.Lock())
        with lock:
            cached = cache.get(key)
            if cached is not None:
                return "warm", cached.status, cached.body
            status, body, complete = _forward(config, path, range_header)
            if status in (200, 206) and complete:
                cache.put(key, CachedRange(status=status, body=body))
                return "cold", status, body
            return "uncached", status, body
        # Lock is released; stale per-path lock objects are intentionally
        # left in the dict (bounded by distinct tile keys actually
        # requested, not by request volume) rather than torn down under a
        # race.

    class Handler(BaseHTTPRequestHandler):
        server_version = "eox-imagery-cache-proxy/0.1"

        def log_message(self, fmt, *args):  # noqa: D401 - silence default stderr logging
            pass

        def _check_health(self) -> tuple[int, dict]:
            # TTL 0, always live: never read from or written to the tile
            # cache, and counted separately from ordinary tile cold/warm
            # reads. Health here means exactly the health of the fixed
            # upstream for the probe request actually sent (a bounded Range
            # read) — 200 or 206 are both genuine successful answers to
            # that request; anything else (404/403/5xx/no response) is not.
            status, _body, _complete = _forward(
                config, _HEALTH_PROBE_PATH, _HEALTH_PROBE_RANGE, max_body_bytes=_HEALTH_PROBE_MAX_BYTES
            )
            cache.record_probe_send()
            if status == 0:
                return 502, {"upstream": "unreachable"}
            if status in (200, 206):
                return 200, {"upstream": "reachable", "status": status}
            return 502, {"upstream": "unhealthy", "status": status}

        def do_HEAD(self) -> None:
            # The real client (EarthStatus.reach()) probes the dedicated
            # health endpoint with HEAD, expecting exactly this proxy's own
            # HTTP status (200 healthy, anything else not) and no body.
            parsed = urlsplit(self.path)
            if parsed.path == "/__health":
                status, _body = self._check_health()
                self.send_response(status)
                self.end_headers()
                return
            self.send_response(404)
            self.end_headers()

        def do_GET(self) -> None:
            parsed = urlsplit(self.path)

            if parsed.path == "/_internal/stats":
                self._respond_json(200, cache.stats() | {"attribution": ATTRIBUTION})
                return

            if parsed.path == "/__health":
                status, body = self._check_health()
                self._respond_json(status, body)
                return

            if _validate_tile_path(parsed.path) is None:
                self._respond_plain(404, b"not a valid EOX tile path")
                return

            range_header = self.headers.get("Range")
            try:
                start, end = _parse_range(range_header)
            except RangeNotSatisfiable:
                self._respond_plain(416, b"unsupported or missing Range header")
                return

            key = Cache.key(parsed.path, start, end)
            outcome, status, body = singleflight_fetch(key, parsed.path, range_header)
            if outcome == "warm":
                cache.record_warm_hit()
                self._respond_bytes(status, body)
                return
            if outcome == "cold":
                cache.record_cold_miss()
                self._respond_bytes(status, body)
                return
            # uncached: error, transient failure, or truncated — never
            # stored, always retryable on the next request.
            cache.record_cold_miss()
            if status == 0:
                self._respond_plain(502, b"upstream unreachable")
                return
            self._respond_bytes(status, body)

        def do_DELETE(self) -> None:
            parsed = urlsplit(self.path)
            if parsed.path == "/_internal/cache":
                removed = cache.clear()
                self._respond_json(200, {"invalidated": removed})
                return
            if _validate_tile_path(parsed.path) is None:
                self._respond_plain(404, b"not a valid EOX tile path")
                return
            removed = cache.delete_prefix(parsed.path)
            self._respond_json(200, {"invalidated": removed})

        def _respond_bytes(self, status: int, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _respond_plain(self, status: int, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _respond_json(self, status: int, obj) -> None:
            import json

            body = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


class ProxyServer(ThreadingHTTPServer):
    """A ThreadingHTTPServer that also owns (and closes) the cache's sqlite
    connection, so callers have one object to shut down cleanly.
    """

    daemon_threads = True

    def __init__(self, config: Config, cache: Cache) -> None:
        super().__init__((config.bind_host, config.bind_port), make_handler(cache, config))
        self.proxy_cache = cache

    def server_close(self) -> None:
        super().server_close()
        self.proxy_cache.close()


def run(config: Config | None = None) -> ProxyServer:
    config = config or Config.from_env()
    cache = Cache(config.sqlite_path, max_bytes=config.max_cache_bytes)
    return ProxyServer(config, cache)


def main() -> None:
    server = run()
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
