# eox-imagery-cache-proxy

A small, separately-deployable local SQLite tile cache proxy for EOX's
Sentinel-2 cloudless WMTS imagery. It sits in front of the real EOX upstream
and caches successful, complete tile reads so repeat Earth Platform world
generation (MINECRAFT-579 under the shared MINECRAFT-571 delivery) doesn't
re-fetch the same tiles from the provider.

Status: **first slice**, offline-verified only. No live call has been made
against the real `tiles.maps.eox.at` upstream; see "Provider terms" below.
The target repository `brooswit-intel/eox-imagery-cache-proxy` does not
exist yet (confirmed by Wroosbit, MINECRAFT-571 comment 55730) — this is
local, unpushed implementation per MINECRAFT-579's "continue local
implementation while repo setup is pending" direction.

This source is **optional**: Earth Platform's `SATELLITE_IMAGERY` config
flag (`EarthConfig.java`) is off by default, and this proxy has no feature
flag of its own to match — it serves tiles whenever the mod is configured
to point at it, and is simply unused when the flag stays off. No live
provider request happens before that opt-in regardless of this proxy
running.

## Why

- Earth Platform's `ImagerySource` (alpha.73 / combined-73, commit
  `88b13c33954550c6ac660dceb743a4288a9aafac`) reads EOX's public WMTS tile
  server directly with a bounded HTTP range request on every cold tile. A
  local proxy gives one process a persistent, restart-proof cache of those
  exact tiles, so regeneration reads the same bytes offline.
- The dataset actually requested — `s2cloudless-2016-v1`, the 2016 vintage
  (`ImagerySource.EOX_S2_CLOUDLESS_2016`) — is released under **CC BY 4.0**:
  attribution-only, no non-commercial restriction, no terms blocker on
  caching or republishing. (Later EOX Sentinel-2 cloudless vintages,
  2018-2024, are CC BY-NC-SA 4.0 and non-commercial-only — but the mod never
  requests those; `ImagerySource` hardcodes the 2016 URI at its one call
  site in `CubicSettings`.)

## Native request contract (reproduced from the real client)

Read directly from `io.earthplatform.geography.globe.ImagerySource` +
`RangeFiles` at the pinned alpha.73 commit, cross-checked against the live
EOX WMTS capabilities document:

- Fixed origin: `https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless_3857/default/g/`.
- `GET <zoom>/<row>/<col>.jpg` — WMTS TileMatrix/TileRow/TileCol order,
  i.e. `z/y/x.jpg`, **not** `z/x/y`. Zoom `0`-`14` (`ImagerySource.MAX_ZOOM`),
  row/col bounded to `[0, 2**zoom)`. No query string, no other method.
- Every read sends an exact `Range: bytes=0-1048575` header — offset zero,
  length `MAX_TILE_BYTES` = 1 MiB (`RangeFiles.fetch(uri, 0,
  MAX_TILE_BYTES)`) — never a whole-file GET. No conditional request
  (`If-None-Match`/`If-Modified-Since`) is ever sent by this client, so this
  proxy does not implement `304` handling.
- `404`/`403` mean "no imagery here" to the real client and are never
  retried as transient. `200`/`206` mean success — **including** the
  upstream's own tiny placeholder image returned for open-sea tiles, which
  is a genuine `2xx` body and is cached like any other successful tile; the
  client, not this proxy, decides that a non-JPEG body means "no imagery."
  This proxy forwards status and body through unmodified.
- A dedicated upstream health-check endpoint, `GET`/`HEAD /__health`, per
  the cross-lane direction (MINECRAFT-571 comments 55734/55737/55789/
  55808/55816): TTL 0, always a live round trip, **never** read from or
  written to the tile cache, counted separately (`probe_sends`) so it
  never inflates or deflates the ordinary tile `cold_misses`/`warm_hits`.
  It issues a bounded `Range: bytes=0-0` read of a fixed representative
  tile (`0/0/0.jpg`, always present at the minimum zoom) against the real
  upstream, treating either `200` or `206` as healthy (both are genuine
  successful answers to that exact Range request — unlike the plain,
  no-Range `HEAD` the real client sends to this endpoint itself, where
  only its own `200` counts). `HEAD /__health` answers with just this
  proxy's own status and no body, matching the real client
  (`EarthStatus.reach()`), which probes with `HEAD` and requires exactly
  `200`; `GET /__health` answers the same status with a JSON body for
  humans/tooling. The game itself never calls `GET /__health`; only
  `HEAD /__health` is on the real client's path, via whatever configured
  health-monitoring the integration lane (MINECRAFT-580) points at it.

## Running it

```sh
python3 -m eox_imagery_cache_proxy
```

Config is via environment variables, all optional:

| Var | Default | Meaning |
|---|---|---|
| `PROXY_BIND_HOST` | `127.0.0.1` | Bind address. Loopback-only by default. |
| `PROXY_BIND_PORT` | `8836` | Listen port. |
| `PROXY_SQLITE_PATH` | `./eox-imagery-cache.sqlite3` | Cache database file. A missing file starts empty. |
| `EOX_UPSTREAM_BASE` | `https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless_3857/default/g` | Fixed, allowlisted HTTPS upstream origin. Point this at a fake/local upstream for any testing. |
| `PROXY_MAX_CACHE_BYTES` | `2147483648` (2 GiB) | Bound on total cached bytes; oldest-touched entries are evicted first once exceeded. |
| `PROXY_UPSTREAM_TIMEOUT_SECONDS` | `30` | Per-request timeout talking to the upstream. |

## Caching rules

- Only a well-formed, in-bounds tile path (`<zoom 0-14>/<row>/<col>.jpg`
  with `row`/`col` inside `[0, 2**zoom)`) is served at all; anything else,
  including any path-traversal attempt, is rejected with `404` before any
  upstream send.
- Only a **complete, successful (`200`/`206`)** range read is cached.
  Errors, transient failures, and truncated reads (a declared
  `Content-Length` the delivered body didn't match) are never cached and
  remain retryable on the next request. Reads are also bounded (per
  MINECRAFT-571 comment 55839's cross-lane finding on raster proxies that
  buffered an origin's response in full): an origin that ignores `Range`
  and returns far more than requested is never read past the expected
  size, and the resulting oversized response is treated the same as a
  truncated one — never cached.
- Cache key is the exact `(tile path, byte range)` the client asked for.
- Cached tile data is **TTL -1** (no time-based expiration) per the
  cross-lane direction (MINECRAFT-571 comment 55740): a good hit survives
  a restart and an arbitrary clock advance with zero further upstream
  calls. It is still bounded by the byte cap below, and explicitly
  invalidatable:
  - `DELETE <zoom>/<row>/<col>.jpg` removes every cached range for that
    one tile.
  - `DELETE /_internal/cache` clears the entire cache.
- `/__health` is carved out of ordinary caching entirely (see above) — TTL
  0, always live.
- Concurrent identical-tile misses are deduplicated (single-flight): N
  simultaneous requests for the same uncached tile produce exactly one
  upstream send, and every waiter gets that one result.
- The cache is bounded (`PROXY_MAX_CACHE_BYTES`); exceeding it evicts the
  least-recently-accessed entries, not a blanket flush.

## Metrics

`GET /_internal/stats` returns:

```json
{"cold_misses": 0, "warm_hits": 0, "probe_sends": 0, "cached_bytes": 0, "cached_entries": 0, "attribution": "..."}
```

All three counters are cumulative since the SQLite file was created (they
survive a process restart, unlike pure in-memory counters):

- `warm_hits` — a tile served entirely from SQLite, no network send.
- `cold_misses` — a tile read that went out over the network (success,
  error, or truncated — every attempted tile send is counted here exactly
  once, regardless of outcome).
- `probe_sends` — a `/__health` reachability probe, counted separately so it
  never inflates or deflates the tile counters above.

`attribution` echoes `ImagerySource.ATTRIBUTION` verbatim so a consumer
surfacing it (e.g. in-game credits) stays correct without re-deriving it,
and is never to be stripped.

## Provider terms

The requested dataset, `s2cloudless-2016-v1`, is released under **CC BY 4.0**
(per the live EOX WMTS capabilities document and `cloudless.eox.at`):
attribution-only, commercial use permitted, no restriction on caching or
redistributing tile bytes. Required attribution text, reproduced verbatim
from `ImagerySource.ATTRIBUTION`:

> EOxCloudless - https://cloudless.eox.at by EOX IT Services GmbH (Contains
> modified Copernicus Sentinel data 2016)

What is **not** yet confirmed: the real upstream's exact behavior on a
malformed/out-of-range request, and its documented rate-limiting under
sustained load (`cloudless.eox.at` mentions rate limiting exists for the
public endpoint) — this slice's retry/backoff behavior has only been
exercised against the local `tests/fake_upstream.py`, never the live tile
server.

## Security notes

- Binds to loopback by default. There is no authentication in front of
  this proxy — anything that can reach `PROXY_BIND_HOST:PROXY_BIND_PORT`
  can use it. Don't bind it to a non-loopback address without adding that.
- No credentials are involved at all (EOX's public WMTS endpoint needs
  none), so there is nothing upstream-secret for this proxy to leak.

## Testing

```sh
python3 -m unittest discover -s tests -v
```

Tests spin up a local fake EOX-shaped upstream on loopback (no network
access, no live provider) and exercise:

- start-empty (fresh SQLite file has zero cached entries),
- cold-then-warm (a repeat identical tile is served from cache with zero
  additional upstream sends),
- restart (a fresh `Cache`/`ProxyServer` object against the same SQLite
  file still serves the warm entry — simulating a process restart),
- error-not-cached (an upstream error is forwarded through unmodified and
  is never cached; the identical request is retried live next time),
- genuinely-missing-tile (a tile the fake upstream doesn't have is a real
  `404`, never cached as success),
- path-shape-rejected (a path outside the allowlisted
  `<zoom>/<row>/<col>.jpg` shape, including path traversal and out-of-bounds
  row/col/zoom, is rejected with `404` before any upstream send),
- concurrent-miss-dedup (eight simultaneous identical-tile misses collapse
  into exactly one upstream send),
- health-endpoint-live (`/__health` always goes live, is never cached, and is
  counted separately from ordinary tile reads),
- health-endpoint-failure-vs-warm-tile (a down upstream fails `/__health` live
  through the proxy while an already-warmed tile keeps serving from SQLite
  unaffected, then `/__health` recovers once the upstream comes back),
- health-endpoint-head (`HEAD /__health` answers with just this proxy's
  own status and no body, matching the real client's probe shape),
- bounded-oversized-read (an origin that ignores `Range` and returns far
  more than requested is read only up to the expected size and never
  cached).
- ttl-minus-one (a warm tile survives both a restart and a simulated huge
  forward clock jump with zero additional upstream calls),
- explicit-invalidation (`DELETE` on one tile path, and `DELETE
  /_internal/cache` for everything, each force the next read to go
  upstream again).
