"""Local fixture server: serves the GHOST test pages over HTTP.

Used by the demo, the benchmark, and the tests. It binds to 127.0.0.1 on
an ephemeral port and serves only the ``fixtures/pages`` directory, so
nothing outside the repo is ever exposed. The production worker must never
reach localhost; the benchmark opts into this server explicitly via the
job's ``allow_local_fixtures`` flag.
"""

from __future__ import annotations

import functools
import http.server
import os
import socketserver
import threading
from typing import Optional

PAGES_DIR = os.path.join(os.path.dirname(__file__), "pages")


class FixtureServer:
    def __init__(self, host: str = "127.0.0.1", port: int = 0):
        handler = functools.partial(
            http.server.SimpleHTTPRequestHandler, directory=PAGES_DIR
        )
        self._httpd = socketserver.TCPServer((host, port), handler)
        self._httpd.allow_reuse_address = True
        self.host = host
        self.port = self._httpd.server_address[1]
        self._thread: Optional[threading.Thread] = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def __enter__(self) -> "FixtureServer":
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._httpd.shutdown()
        self._httpd.server_close()
        return False
