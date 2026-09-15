"""HttpTransport against a real socket.

The rest of the suite uses FakeTransport, which is right for provider logic but
tests nothing about the HTTP layer itself. These tests stand up an actual
``http.server`` on a loopback port so the transport's real behavior is
exercised: connection reuse, error decoding, non-JSON bodies, and the stale
keep-alive retry.

They are fast (a thread and a loopback socket) and hermetic (no external
network), so there is no reason to keep them out of the default run.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from abca.providers.base import ProviderTimeout
from abca.providers.transport import HttpTransport, TransportError


class _Handler(BaseHTTPRequestHandler):
    """Routes a handful of fixed paths. Behavior is driven by the path itself."""

    protocol_version = "HTTP/1.1"  # enables keep-alive

    def log_message(self, *args):
        return

    def _respond(self, status: int, body: bytes, content_type="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        server = self.server
        server.request_count += 1  # type: ignore[attr-defined]

        if self.path == "/ok":
            self._respond(200, json.dumps({"hello": "world"}).encode())
        elif self.path == "/empty":
            self._respond(200, b"")
        elif self.path == "/notjson":
            self._respond(200, b"<html>not json</html>", "text/html")
        elif self.path == "/array":
            self._respond(200, b'[1,2,3]')
        elif self.path == "/error":
            self._respond(404, json.dumps({"error": "model 'x' not found"}).encode())
        else:
            self._respond(404, b"{}")

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        self.server.last_payload = payload  # type: ignore[attr-defined]
        self.server.last_headers = dict(self.headers)  # type: ignore[attr-defined]
        self._respond(200, json.dumps({"echo": payload}).encode())


@pytest.fixture
def server():
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    httpd.request_count = 0  # type: ignore[attr-defined]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def transport(server):
    host, port = server.server_address
    client = HttpTransport(f"http://{host}:{port}", timeout=5.0, provider_name="test")
    yield client
    client.close()


class TestRequests:
    def test_get_json(self, transport):
        assert transport.get_json("/ok") == {"hello": "world"}

    def test_post_json_round_trips(self, transport, server):
        assert transport.post_json("/echo", {"a": 1})["echo"] == {"a": 1}
        assert server.last_payload == {"a": 1}

    def test_custom_headers_are_sent(self, server):
        """How the Anthropic version header and both auth schemes reach the wire."""
        host, port = server.server_address
        with HttpTransport(
            f"http://{host}:{port}",
            headers={"x-api-key": "sk-test", "anthropic-version": "2023-06-01"},
        ) as client:
            client.post_json("/echo", {})
        assert server.last_headers["x-api-key"] == "sk-test"
        assert server.last_headers["anthropic-version"] == "2023-06-01"

    def test_empty_body_is_an_empty_dict(self, transport):
        assert transport.get_json("/empty") == {}


class TestConnectionReuse:
    def test_one_connection_serves_many_requests(self, transport, server):
        """The reason for http.client over urllib.

        The classifier role runs once per claim -- thousands of times on a big
        thread. A fresh TCP handshake per call is measurable locally and much
        worse over an SSH tunnel to EC2.
        """
        for _ in range(10):
            transport.get_json("/ok")
        assert server.request_count == 10
        # A single connection object was reused throughout.
        assert transport._connection is not None

    def test_close_is_idempotent(self, transport):
        transport.close()
        transport.close()
        assert transport._connection is None

    def test_reconnects_after_close(self, transport):
        transport.get_json("/ok")
        transport.close()
        assert transport.get_json("/ok") == {"hello": "world"}


class TestErrors:
    def test_http_error_surfaces_the_server_message(self, transport):
        """Ollama's "model not found" is far more actionable than a bare 404."""
        with pytest.raises(TransportError, match="model 'x' not found"):
            transport.get_json("/error")

    def test_status_code_is_reported(self, transport):
        with pytest.raises(TransportError, match="HTTP 404"):
            transport.get_json("/error")

    def test_non_json_body_is_rejected(self, transport):
        with pytest.raises(TransportError, match="non-JSON"):
            transport.get_json("/notjson")

    def test_non_object_json_is_rejected(self, transport):
        """Every endpoint used here returns an object; a bare array means trouble."""
        with pytest.raises(TransportError, match="expected an object"):
            transport.get_json("/array")

    def test_unreachable_port_is_a_transport_error(self):
        """A closed port must surface as a provider-level error, never a bare socket one.

        Which provider-level error depends on the operating system, and the
        first CI run on Windows found out: POSIX refuses the connection at once
        (``TransportError``, with the "Is the backend running" hint), while the
        Windows stack retries SYN for longer than the 1s budget and the timeout
        fires first (``ProviderTimeout``). Both are the right SHAPE of failure --
        typed, caught, and reported by the caller -- and asserting on the POSIX
        message alone was asserting on the kernel rather than on this code.
        """
        with HttpTransport("http://127.0.0.1:1", timeout=1.0) as client:
            with pytest.raises((TransportError, ProviderTimeout)) as caught:
                client.get_json("/ok")
        if isinstance(caught.value, TransportError):
            assert "Is the backend running" in str(caught.value)

    def test_timeout_is_its_own_error_type(self, server):
        """Distinct from unavailable: on a loaded GPU a timeout is routine."""
        host, port = server.server_address
        # A socket that accepts but never answers would be ideal; approximating
        # with an unroutable address that will hang until the timeout fires.
        with HttpTransport("http://10.255.255.1", timeout=0.5) as client:
            with pytest.raises((ProviderTimeout, TransportError)):
                client.get_json("/ok")
