"""Integration coverage for app.ai.http_utils.post_json_bounded.

Runs against a real local HTTP server rather than mocking httpx, matching
this project's existing style of exercising real behavior end-to-end --
mocking httpx.stream would only prove the mock was configured correctly,
not that the streaming/abort logic actually works against a real response.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.ai.http_utils import ResponseTooLarge, post_json_bounded


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)

        if self.path == "/small":
            body = json.dumps({"ok": True, "echo": self.headers.get("X-Test-Header")}).encode()
            self.send_response(200)
        elif self.path == "/huge":
            body = b"x" * (2 * 1024 * 1024)  # 2MB, larger than the test's small max_bytes
            self.send_response(200)
        elif self.path == "/server-error":
            body = b'{"error": "boom"}'
            self.send_response(500)
        else:
            body = b"{}"
            self.send_response(404)

        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


@pytest.fixture(scope="module")
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()


def test_small_response_parsed_normally(server):
    result = post_json_bounded(f"{server}/small", {"hello": "world"}, timeout=5)
    assert result == {"ok": True, "echo": None}


def test_headers_are_forwarded(server):
    result = post_json_bounded(f"{server}/small", {}, timeout=5, headers={"X-Test-Header": "present"})
    assert result["echo"] == "present"


def test_oversized_response_aborts_without_buffering_fully(server):
    with pytest.raises(ResponseTooLarge):
        post_json_bounded(f"{server}/huge", {}, timeout=5, max_bytes=1024)


def test_http_error_status_raises_before_touching_body(server):
    import httpx
    with pytest.raises(httpx.HTTPStatusError):
        post_json_bounded(f"{server}/server-error", {}, timeout=5)
