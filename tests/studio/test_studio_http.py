"""End-to-end tests over a real loopback socket."""

from __future__ import annotations

import http.client
import io
import json
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from studio_samples import BASELINE_ID

from traceon.studio import server as server_mod
from traceon.studio.server import SECURITY_HEADERS, StudioError, StudioServer, make_server, serve

EXPECTED_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'"
)


@pytest.fixture
def live(fixture_dir: Path) -> Iterator[StudioServer]:
    srv = make_server([fixture_dir], port=0)
    thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


def request(
    srv: StudioServer, method: str, path: str, host: str | None = None
) -> tuple[int, dict[str, str], bytes]:
    port = int(srv.server_address[1])
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        headers = {"Host": host if host is not None else f"127.0.0.1:{port}"}
        conn.request(method, path, headers=headers)
        resp = conn.getresponse()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
    finally:
        conn.close()


def assert_security_headers(headers: dict[str, str]) -> None:
    csp = headers["content-security-policy"]
    assert csp.startswith(EXPECTED_CSP)
    assert "frame-ancestors 'none'" in csp
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "no-referrer"
    assert headers["x-frame-options"] == "DENY"


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/static/app.js",
        "/api/fixtures",
        f"/api/fixtures/{BASELINE_ID}",
        "/missing",
        "/static/../server.py",
        "/api/diff",
    ],
)
def test_security_headers_on_every_response(live: StudioServer, path: str) -> None:
    _status, headers, _ = request(live, "GET", path)
    assert_security_headers(headers)


def test_get_list_and_fixture(live: StudioServer) -> None:
    status, headers, data = request(live, "GET", "/api/fixtures")
    assert status == 200
    assert headers["content-type"] == "application/json; charset=utf-8"
    ids = [f["id"] for f in json.loads(data)["fixtures"]]
    assert BASELINE_ID in ids
    status, _, data = request(live, "GET", f"/api/fixtures/{BASELINE_ID}")
    assert status == 200 and json.loads(data)["fixture_id"] == BASELINE_ID


def test_head_has_no_body(live: StudioServer) -> None:
    status, headers, data = request(live, "HEAD", "/")
    assert status == 200
    assert int(headers["content-length"]) > 0
    assert data == b""


def test_post_rejected(live: StudioServer) -> None:
    status, headers, _ = request(live, "POST", "/api/fixtures")
    assert status == 405
    assert headers["allow"] == "GET, HEAD"


def test_raw_traversal_over_socket(live: StudioServer) -> None:
    for path in ("/static/../server.py", "/static/..%2f..%2fcore%2fio.py", "/../pyproject.toml"):
        status, _, data = request(live, "GET", path)
        assert status == 404, path
        assert b"def " not in data


def test_dns_rebinding_host_rejected(live: StudioServer) -> None:
    status, _, _ = request(live, "GET", "/api/fixtures", host="attacker.example")
    assert status == 421
    port = int(live.server_address[1])
    assert request(live, "GET", "/api/fixtures", host=f"localhost:{port}")[0] == 200


def test_server_header_does_not_leak_python_version(live: StudioServer) -> None:
    _, headers, _ = request(live, "GET", "/")
    assert "python" not in headers.get("server", "").lower()


def test_port_in_use_is_actionable(fixture_dir: Path) -> None:
    first = make_server([fixture_dir], port=0)
    try:
        port = int(first.server_address[1])
        with pytest.raises(StudioError, match=f"cannot listen on 127.0.0.1:{port}"):
            make_server([fixture_dir], port=port)
    finally:
        first.server_close()


def test_security_headers_constant_matches_contract() -> None:
    assert SECURITY_HEADERS["Content-Security-Policy"].startswith(EXPECTED_CSP)


def _patched_serve(monkeypatch: pytest.MonkeyPatch, host: str, fixture_dir: Path, **kw: Any) -> str:
    real_make = server_mod.make_server
    opened: list[str] = []

    def make(paths: Any, **kwargs: Any) -> StudioServer:
        kwargs["host"] = "127.0.0.1"  # never actually bind a public interface in tests
        kwargs["port"] = 0
        srv = real_make(paths, **kwargs)
        monkeypatch.setattr(
            srv,
            "serve_forever",
            lambda poll_interval=0.5: (_ for _ in ()).throw(KeyboardInterrupt()),
        )
        return srv

    monkeypatch.setattr(server_mod, "make_server", make)
    monkeypatch.setattr(server_mod.webbrowser, "open", lambda url: opened.append(url))
    out = io.StringIO()
    serve([fixture_dir], host=host, port=0, stream=out, **kw)
    return out.getvalue()


def test_serve_loopback_prints_url_and_skips(
    monkeypatch: pytest.MonkeyPatch, fixture_dir: Path
) -> None:
    text = _patched_serve(monkeypatch, "127.0.0.1", fixture_dir, open_browser=False)
    assert "Traceon Studio: 2 fixtures at http://127.0.0.1:" in text
    assert "warning: " in text and "broken.json" in text
    assert "WARNING" not in text
    assert "stopped" in text


def test_serve_non_loopback_warns_loudly(
    monkeypatch: pytest.MonkeyPatch, fixture_dir: Path
) -> None:
    text = _patched_serve(monkeypatch, "0.0.0.0", fixture_dir, open_browser=False)
    assert "WARNING: Traceon Studio is listening on a non-loopback address (0.0.0.0)" in text
    assert "no authentication" in text


def test_serve_recursive(monkeypatch: pytest.MonkeyPatch, fixture_dir: Path) -> None:
    text = _patched_serve(monkeypatch, "127.0.0.1", fixture_dir, open_browser=False, recursive=True)
    assert "3 fixtures" in text
