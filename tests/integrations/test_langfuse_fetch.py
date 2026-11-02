"""Tests for the Langfuse API client. ``langfuse._open`` is replaced, so nothing
touches the network."""

from __future__ import annotations

import base64
import email.message
import io
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

import pytest

from traceon.core.limits import Limits
from traceon.integrations import langfuse
from traceon.integrations.langfuse import LangfuseError, fetch_trace

PK = "pk-lf-1234567890"
SK = "sk-lf-super-secret-value"


class FakeResponse:
    def __init__(
        self, body: bytes, *, status: int = 200, headers: dict[str, str] | None = None
    ) -> None:
        self._body = io.BytesIO(body)
        self.status = status
        self.headers = headers or {}

    def read(self, n: int = -1) -> bytes:
        return self._body.read(n)

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        self._body.close()


def http_error(
    url: str, code: int, headers: dict[str, str] | None = None
) -> urllib.error.HTTPError:
    msg = email.message.Message()
    for k, v in (headers or {}).items():
        msg[k] = v
    return urllib.error.HTTPError(url, code, "error", msg, io.BytesIO(b'{"message":"nope"}'))


Handler = Callable[[urllib.request.Request], Any]


class Transport(list[urllib.request.Request]):
    """Records requests and answers them with queued handlers (the last one repeats)."""

    def __init__(self) -> None:
        super().__init__()
        self.handlers: list[Handler] = []

    def __call__(self, request: urllib.request.Request, timeout: float) -> Any:
        assert timeout > 0
        self.append(request)
        handler = self.handlers[0] if len(self.handlers) == 1 else self.handlers.pop(0)
        result = handler(request)
        if isinstance(result, BaseException):
            raise result
        return result


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Transport:
    transport = Transport()
    monkeypatch.setattr(langfuse, "_open", transport)
    for var in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST", "LANGFUSE_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    return transport


def respond(*handlers: Handler, server: Transport) -> None:
    server.handlers.extend(handlers)


def json_response(data: Any) -> Handler:
    return lambda req: FakeResponse(json.dumps(data).encode())


TRACE = {"id": "abc", "name": "agent", "observations": [{"id": "o1", "type": "SPAN"}]}


def test_fetch_success_uses_basic_auth(server: Transport) -> None:
    respond(json_response(TRACE), server=server)
    data = fetch_trace("abc", host="https://lf.example.com/", public_key=PK, secret_key=SK)
    assert data == TRACE
    (req,) = server
    assert req.full_url == "https://lf.example.com/api/public/traces/abc"
    assert req.get_method() == "GET"
    auth = req.unredirected_hdrs["Authorization"]
    assert auth == "Basic " + base64.b64encode(f"{PK}:{SK}".encode()).decode()
    # Not a redirectable header: credentials never follow a redirect.
    assert "Authorization" not in req.headers
    assert req.headers["Accept"] == "application/json"
    assert req.headers["User-agent"].startswith("traceon-replay/")


def test_env_configuration(server: Transport, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", PK)
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", SK)
    monkeypatch.setenv("LANGFUSE_HOST", "https://us.cloud.langfuse.com")
    respond(json_response(TRACE), server=server)
    fetch_trace("abc")
    assert server[0].full_url.startswith("https://us.cloud.langfuse.com/api/public/traces/")


def test_base_url_env_wins_over_host(server: Transport, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://base.example.com/langfuse")
    monkeypatch.setenv("LANGFUSE_HOST", "https://host.example.com")
    respond(json_response(TRACE), server=server)
    fetch_trace("abc", public_key=PK, secret_key=SK)
    assert server[0].full_url == "https://base.example.com/langfuse/api/public/traces/abc"


def test_default_host(server: Transport) -> None:
    respond(json_response(TRACE), server=server)
    fetch_trace("abc", public_key=PK, secret_key=SK)
    assert server[0].full_url.startswith(langfuse.DEFAULT_HOST + "/api/public/traces/")


def test_trace_id_is_path_encoded(server: Transport) -> None:
    respond(json_response(TRACE), server=server)
    fetch_trace("../admin?x=1", public_key=PK, secret_key=SK)
    assert server[0].full_url.endswith("/api/public/traces/..%2Fadmin%3Fx%3D1")


@pytest.mark.parametrize("trace_id", ["", "   ", "a" * 300, "a\nb"])
def test_invalid_trace_id(trace_id: str) -> None:
    with pytest.raises(LangfuseError):
        fetch_trace(trace_id, public_key=PK, secret_key=SK)


def test_missing_credentials(server: Transport) -> None:
    with pytest.raises(LangfuseError, match="LANGFUSE_PUBLIC_KEY"):
        fetch_trace("abc")
    assert server == []


def test_invalid_timeout() -> None:
    with pytest.raises(LangfuseError, match="timeout"):
        fetch_trace("abc", public_key=PK, secret_key=SK, timeout=0)


@pytest.mark.parametrize(
    ("host", "message"),
    [
        ("ftp://lf.example.com", "invalid Langfuse host"),
        ("not a url", "invalid Langfuse host"),
        ("http://lf.example.com", "plain http"),
        ("https://user:pass@lf.example.com", "must not embed credentials"),
        ("https://lf.example.com/?a=1", "query or fragment"),
    ],
)
def test_host_validation(host: str, message: str, server: Transport) -> None:
    with pytest.raises(LangfuseError, match=message):
        fetch_trace("abc", host=host, public_key=PK, secret_key=SK)
    assert server == []


@pytest.mark.parametrize(
    "host",
    [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://[::1]:3000",
        "http://langfuse.localhost",
    ],
)
def test_plain_http_allowed_for_loopback(host: str, server: Transport) -> None:
    respond(json_response(TRACE), server=server)
    fetch_trace("abc", host=host, public_key=PK, secret_key=SK)
    assert server[0].full_url.startswith(host)


@pytest.mark.parametrize(
    ("code", "message"),
    [(401, "rejected the credentials"), (403, "rejected"), (429, "rate limit"), (500, "HTTP 500")],
)
def test_http_errors_never_leak_credentials(code: int, message: str, server: Transport) -> None:
    respond(lambda req: http_error(req.full_url, code), server=server)
    with pytest.raises(LangfuseError, match=message) as info:
        fetch_trace("abc", public_key=PK, secret_key=SK)
    text = str(info.value) + repr(info.value)
    assert SK not in text and PK not in text
    assert base64.b64encode(f"{PK}:{SK}".encode()).decode() not in text
    assert info.value.__cause__ is None


def test_redirect_is_refused(server: Transport) -> None:
    respond(
        lambda req: http_error(req.full_url, 302, {"Location": "https://evil.example.net/x"}),
        server=server,
    )
    with pytest.raises(LangfuseError, match=r"redirected \(HTTP 302\) to evil.example.net"):
        fetch_trace("abc", public_key=PK, secret_key=SK)


def test_no_redirect_handler_returns_none() -> None:
    handler = langfuse._NoRedirect()
    req = urllib.request.Request("https://a.example.com")
    assert handler.redirect_request(req, None, 302, "Found", {}, "https://b.example.com") is None


def test_network_errors(server: Transport) -> None:
    respond(lambda req: urllib.error.URLError("Name or service not known"), server=server)
    with pytest.raises(LangfuseError, match="could not reach Langfuse"):
        fetch_trace("abc", public_key=PK, secret_key=SK)


def test_timeout(server: Transport) -> None:
    respond(lambda req: TimeoutError("timed out"), server=server)
    with pytest.raises(LangfuseError, match="within 5s"):
        fetch_trace("abc", public_key=PK, secret_key=SK, timeout=5)


def test_url_error_wrapping_timeout(server: Transport) -> None:
    respond(lambda req: urllib.error.URLError(TimeoutError("timed out")), server=server)
    with pytest.raises(LangfuseError, match="did not respond"):
        fetch_trace("abc", public_key=PK, secret_key=SK)


def test_invalid_json(server: Transport) -> None:
    respond(lambda req: FakeResponse(b"<html>gateway</html>"), server=server)
    with pytest.raises(LangfuseError, match="not valid JSON"):
        fetch_trace("abc", public_key=PK, secret_key=SK)


def test_size_cap_from_content_length(server: Transport) -> None:
    big = str(langfuse.MAX_RESPONSE_BYTES + 1)
    respond(lambda req: FakeResponse(b"{}", headers={"Content-Length": big}), server=server)
    with pytest.raises(LangfuseError, match="20 MB"):
        fetch_trace("abc", public_key=PK, secret_key=SK)


def test_size_cap_while_reading(server: Transport, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(langfuse, "MAX_RESPONSE_BYTES", 64)
    respond(lambda req: FakeResponse(b'{"pad": "' + b"x" * 100 + b'"}'), server=server)
    with pytest.raises(LangfuseError, match="import limit"):
        fetch_trace("abc", public_key=PK, secret_key=SK)


def _v2_page(observations: list[dict[str, Any]], cursor: str | None) -> Handler:
    return json_response({"data": observations, "meta": {"cursor": cursor} if cursor else {}})


def test_falls_back_to_v2_observations_with_pagination(server: Transport) -> None:
    respond(
        lambda req: http_error(req.full_url, 404),
        _v2_page([{"id": "o1", "traceName": "agent"}], "c1"),
        _v2_page([{"id": "o2"}], None),
        server=server,
    )
    data = fetch_trace("abc", public_key=PK, secret_key=SK)
    assert data == {"id": "abc", "observations": [{"id": "o1", "traceName": "agent"}, {"id": "o2"}]}
    first = urllib.parse.urlsplit(server[1].full_url)
    assert first.path == "/api/public/v2/observations"
    query = urllib.parse.parse_qs(first.query)
    assert query["traceId"] == ["abc"]
    assert "io" in query["fields"][0].split(",")
    assert "cursor" not in query
    assert urllib.parse.parse_qs(urllib.parse.urlsplit(server[2].full_url).query)["cursor"] == [
        "c1"
    ]
    assert all(r.unredirected_hdrs.get("Authorization") for r in server)


def test_trace_without_observations_is_completed_from_v2(server: Transport) -> None:
    respond(
        json_response({"id": "abc", "name": "agent", "observations": []}),
        _v2_page([{"id": "o1"}], None),
        server=server,
    )
    data = fetch_trace("abc", public_key=PK, secret_key=SK)
    assert data["name"] == "agent"
    assert data["observations"] == [{"id": "o1"}]


def test_not_found(server: Transport) -> None:
    respond(lambda req: http_error(req.full_url, 404), _v2_page([], None), server=server)
    with pytest.raises(LangfuseError, match="not found"):
        fetch_trace("abc", public_key=PK, secret_key=SK)


def test_unexpected_v2_shape(server: Transport) -> None:
    respond(lambda req: http_error(req.full_url, 405), json_response({"rows": []}), server=server)
    with pytest.raises(LangfuseError, match="unexpected response"):
        fetch_trace("abc", public_key=PK, secret_key=SK)


def test_pagination_is_bounded(server: Transport, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(langfuse, "DEFAULT_LIMITS", Limits(max_events=150))
    respond(lambda req: http_error(req.full_url, 404), server=server)
    respond(*[_v2_page([{"id": f"o{i}"}], f"c{i}") for i in range(10)], server=server)
    with pytest.raises(LangfuseError, match="more than 150"):
        fetch_trace("abc", public_key=PK, secret_key=SK)


def test_budget_is_shared_across_pages(server: Transport, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(langfuse, "MAX_RESPONSE_BYTES", 120)
    page = {"data": [{"id": "o" * 40}], "meta": {"cursor": "next"}}
    respond(lambda req: http_error(req.full_url, 404), server=server)
    respond(json_response(page), json_response(page), json_response(page), server=server)
    with pytest.raises(LangfuseError, match="import limit"):
        fetch_trace("abc", public_key=PK, secret_key=SK)


def test_fetch_then_import(server: Transport) -> None:
    respond(
        json_response(
            {
                "id": "abc",
                "timestamp": "2026-10-01T00:00:00Z",
                "name": "agent",
                "observations": [
                    {
                        "id": "o1",
                        "type": "TOOL",
                        "name": "search",
                        "startTime": "2026-10-01T00:00:01Z",
                        "input": {"q": "x"},
                        "output": [1],
                    }
                ],
            }
        ),
        server=server,
    )
    fixture = langfuse.import_trace(fetch_trace("abc", public_key=PK, secret_key=SK))
    assert [e.name for e in fixture.calls()] == ["search"]
