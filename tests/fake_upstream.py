"""A loopback-only, credential-free fake EOX-shaped upstream for tests.

Nothing here ever reaches the real EOX tile server (``tiles.maps.eox.at``);
it serves ``/<z>/<y>/<x>.jpg`` tiles with range-request semantics close
enough to the real WMTS server to exercise this proxy's caching,
concurrency, and health-probe behavior without any live provider call.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


class FakeUpstream:
    def __init__(self, tiles: dict[str, bytes]) -> None:
        """``tiles`` maps a tile path (e.g. ``/0/0/0.jpg``) to its full byte
        content.
        """
        self.tiles = tiles
        self.call_count = 0
        self.range_calls: list[tuple[str, str | None]] = []
        self.fail_next = False
        self.down = False
        self.ignore_range = False
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args) -> None:  # noqa: D401
                pass

            def do_GET(self) -> None:
                outer.call_count += 1
                range_header = self.headers.get("Range")
                outer.range_calls.append((self.path, range_header))

                if outer.down:
                    self.connection.close()
                    return

                if outer.fail_next:
                    outer.fail_next = False
                    payload = b"simulated upstream failure"
                    self.send_response(503)
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return

                content = outer.tiles.get(self.path)
                if content is None:
                    payload = b"not found"
                    self.send_response(404)
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return

                if outer.ignore_range or not range_header or not range_header.startswith("bytes="):
                    self.send_response(200)
                    self.send_header("Content-Type", "image/jpeg")
                    self.send_header("Content-Length", str(len(content)))
                    self.end_headers()
                    self.wfile.write(content)
                    return

                start_s, _, end_s = range_header[len("bytes="):].partition("-")
                start, end = int(start_s), int(end_s)
                body = content[start:end + 1]
                self.send_response(206)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(content)}")
                self.end_headers()
                self.wfile.write(body)

        self._httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def start(self) -> "FakeUpstream":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"
