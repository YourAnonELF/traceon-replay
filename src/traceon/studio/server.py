"""HTTP server behind Traceon Studio.

The server is deliberately small and conservative:

* standard library only (:class:`http.server.ThreadingHTTPServer`);
* binds to ``127.0.0.1`` by default and warns loudly when asked to bind anything else;
* serves only fixture files named on the command line (directories are scanned for
  ``*.json`` files that validate as fixtures); request paths are never mapped onto
  the filesystem -- fixtures are looked up by id and static assets by an allowlist
  built from the packaged ``static/`` directory;
* rejects requests whose ``Host`` header is not the loopback address it serves
  (defends against DNS rebinding) when bound to loopback;
* sends a strict Content-Security-Policy and related headers on every response.

:class:`StudioApp` holds the request handling logic independent of sockets so it
can be tested directly; :func:`make_server` wraps it in a real HTTP server and
:func:`serve` runs it until interrupted.
"""

from __future__ import annotations

import datetime as _dt
import ipaddress
import json
import re
import socket
import socketserver
import sys
import threading
import webbrowser
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO
from urllib.parse import parse_qs, unquote, urlsplit

from traceon.core.errors import FixtureError, TraceonError
from traceon.core.io import load_fixture
from traceon.core.schema import DiffReport, Fixture

if TYPE_CHECKING:
    if sys.version_info >= (3, 11):
        from importlib.resources.abc import Traversable
    else:
        from importlib.abc import Traversable

__all__ = [
    "SECURITY_HEADERS",
    "FixtureEntry",
    "FixtureRegistry",
    "Response",
    "StudioApp",
    "StudioError",
    "StudioServer",
    "make_server",
    "serve",
]

CompareFn = Callable[[Fixture, Fixture], DiffReport]

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'none'"
)

#: Headers sent on every response, including errors.
SECURITY_HEADERS: Mapping[str, str] = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
}

_CONTENT_TYPES: Mapping[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".ico": "image/x-icon",
}

#: Static asset names that may be served: flat names, no separators, known extensions.
_STATIC_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
#: Fixture ids accepted in URLs before registry lookup.
_FIXTURE_ID = re.compile(r"^[A-Za-z0-9._:~-]{1,200}$")
_LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "::1"})


class StudioError(TraceonError):
    """Traceon Studio could not start (port in use, bad bind address, ...)."""


# ---------------------------------------------------------------------------
# Fixture registry
# ---------------------------------------------------------------------------


def _utc_iso(value: _dt.datetime) -> str:
    return value.isoformat()


def summarize_fixture(fixture: Fixture) -> dict[str, Any]:
    """Compact, JSON-ready summary used by the fixture list and run header."""
    calls = fixture.calls()
    tool_calls = sum(1 for e in calls if e.kind == "tool")
    model_calls = sum(1 for e in calls if e.kind == "model")
    value_calls = sum(1 for e in calls if e.kind == "value")
    errors = sum(1 for e in fixture.events if e.status == "error")
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_measured: float | None = None
    cost_estimated: float | None = None
    currencies: set[str] = set()
    for ev in fixture.events:
        usage = ev.usage
        if usage is None:
            continue
        if usage.input_tokens is not None:
            input_tokens = (input_tokens or 0) + usage.input_tokens
        if usage.output_tokens is not None:
            output_tokens = (output_tokens or 0) + usage.output_tokens
        if usage.cost is not None:
            currencies.add(usage.cost.currency)
            if usage.cost.kind == "measured":
                cost_measured = (cost_measured or 0.0) + usage.cost.amount
            else:
                cost_estimated = (cost_estimated or 0.0) + usage.cost.amount
    return {
        "fixture_id": fixture.fixture_id,
        "run_id": fixture.run_id,
        "name": fixture.name,
        "created_at": _utc_iso(fixture.created_at),
        "source": fixture.source,
        "mode": fixture.mode,
        "status": fixture.status,
        "events": len(fixture.events),
        "calls": len(calls),
        "tool_calls": tool_calls,
        "model_calls": model_calls,
        "value_calls": value_calls,
        "errors": errors,
        "duration_ms": run_duration_ms(fixture),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_measured": round(cost_measured, 6) if cost_measured is not None else None,
        "cost_estimated": round(cost_estimated, 6) if cost_estimated is not None else None,
        "currency": next(iter(currencies)) if len(currencies) == 1 else None,
        "warnings": len(fixture.warnings),
    }


def run_duration_ms(fixture: Fixture) -> float | None:
    """Wall-clock span of the run: earliest start to latest end, in milliseconds."""
    if not fixture.events:
        return None
    start = min(e.started_at for e in fixture.events)
    ends: list[_dt.datetime] = []
    for e in fixture.events:
        if e.ended_at is not None:
            ends.append(e.ended_at)
        elif e.duration_ms is not None:
            ends.append(e.started_at + _dt.timedelta(milliseconds=e.duration_ms))
    if not ends:
        return None
    return round((max(ends) - start).total_seconds() * 1000.0, 3)


@dataclass
class FixtureEntry:
    """One fixture file known to the registry."""

    key: str
    path: Path
    summary: dict[str, Any]
    _fixture: Fixture | None = field(default=None, repr=False)
    _mtime_ns: int = 0


class FixtureRegistry:
    """The fixed set of fixture files Studio is allowed to serve.

    Built once from the paths given on the command line. Request handling only
    ever looks fixtures up by key in this registry, so no request can make the
    server read a file that was not explicitly provided.
    """

    def __init__(self) -> None:
        self._entries: dict[str, FixtureEntry] = {}
        self._lock = threading.Lock()
        #: Human-readable notes about files that were skipped.
        self.warnings: list[str] = []

    # Construction ---------------------------------------------------------
    @classmethod
    def scan(cls, paths: Iterable[str | Path], *, recursive: bool = False) -> FixtureRegistry:
        """Load every fixture under ``paths``.

        Files are loaded directly; directories are scanned for ``*.json``
        (non-recursively unless ``recursive``). Files that are not valid
        fixtures are skipped and described in :attr:`warnings`.
        """
        registry = cls()
        seen: set[Path] = set()
        for raw in paths:
            path = Path(raw)
            if path.is_dir():
                pattern = "**/*.json" if recursive else "*.json"
                candidates = sorted(p for p in path.glob(pattern) if p.is_file())
                if not candidates:
                    registry.warnings.append(f"{path}: no *.json files found")
                for candidate in candidates:
                    registry._add_file(candidate, seen, explicit=False)
            elif path.exists():
                registry._add_file(path, seen, explicit=True)
            else:
                registry.warnings.append(f"{path}: no such file or directory")
        return registry

    @classmethod
    def from_fixtures(cls, fixtures: Mapping[str, Fixture]) -> FixtureRegistry:
        """Registry over in-memory fixtures (keyed by a pseudo file name)."""
        registry = cls()
        for name, fixture in fixtures.items():
            registry._insert(Path(name), fixture, mtime_ns=0)
        return registry

    def _add_file(self, path: Path, seen: set[Path], *, explicit: bool) -> None:
        resolved = path.resolve()
        if resolved in seen:
            return
        seen.add(resolved)
        try:
            mtime_ns = resolved.stat().st_mtime_ns
            fixture = load_fixture(resolved)
        except FixtureError as exc:
            prefix = "skipped" if explicit else "skipped (not a Traceon fixture)"
            self.warnings.append(f"{path}: {prefix}: {_first_line(str(exc))}")
            return
        except OSError as exc:
            self.warnings.append(f"{path}: skipped: {exc.strerror or exc}")
            return
        self._insert(resolved, fixture, mtime_ns=mtime_ns)

    def _insert(self, path: Path, fixture: Fixture, *, mtime_ns: int) -> None:
        key = fixture.fixture_id
        if key in self._entries:
            n = 2
            while f"{fixture.fixture_id}~{n}" in self._entries:
                n += 1
            key = f"{fixture.fixture_id}~{n}"
            self.warnings.append(
                f"{path}: duplicate fixture_id {fixture.fixture_id!r}; served as {key!r}"
            )
        summary = summarize_fixture(fixture)
        summary["id"] = key
        summary["file"] = path.name
        self._entries[key] = FixtureEntry(key, path, summary, fixture, mtime_ns)

    # Queries --------------------------------------------------------------
    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, key: object) -> bool:
        return key in self._entries

    def keys(self) -> list[str]:
        return list(self._entries)

    def summaries(self) -> list[dict[str, Any]]:
        """Summaries of every fixture, newest first."""
        items = [dict(e.summary) for e in self._entries.values()]
        items.sort(key=lambda s: (s["created_at"], s["id"]), reverse=True)
        return items

    def get(self, key: str) -> Fixture:
        """Return the fixture for ``key``, re-reading the file if it changed on disk.

        Raises :class:`KeyError` for unknown keys and :class:`FixtureError` if a
        file that changed is no longer a valid fixture.
        """
        with self._lock:
            entry = self._entries[key]
            if entry._mtime_ns:
                try:
                    mtime_ns = entry.path.stat().st_mtime_ns
                except OSError as exc:
                    raise FixtureError(f"{entry.path.name}: no longer readable") from exc
                if mtime_ns != entry._mtime_ns or entry._fixture is None:
                    fixture = load_fixture(entry.path)
                    entry._fixture = fixture
                    entry._mtime_ns = mtime_ns
                    summary = summarize_fixture(fixture)
                    summary["id"] = entry.key
                    summary["file"] = entry.path.name
                    entry.summary = summary
            assert entry._fixture is not None
            return entry._fixture


def _first_line(text: str) -> str:
    line = text.strip().splitlines()[0] if text.strip() else text
    return line if len(line) <= 300 else line[:297] + "..."


# ---------------------------------------------------------------------------
# Request handling (socket-free)
# ---------------------------------------------------------------------------


@dataclass
class Response:
    status: int
    body: bytes
    content_type: str
    headers: dict[str, str] = field(default_factory=dict)


def _json_response(status: int, payload: Any) -> Response:
    body = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return Response(
        status,
        body.encode("utf-8"),
        "application/json; charset=utf-8",
        {"Cache-Control": "no-store"},
    )


def _error(status: HTTPStatus, message: str) -> Response:
    return _json_response(status.value, {"error": {"status": status.value, "message": message}})


def _default_compare() -> CompareFn | None:
    """Import :func:`traceon.diff.compare` lazily; ``None`` when unavailable."""
    try:
        from traceon.diff import compare
    except ImportError:
        return None
    fn: CompareFn = compare
    return fn


def _static_root() -> Traversable:
    return resources.files("traceon.studio").joinpath("static")


class StudioApp:
    """Routes Studio requests to responses; independent of the HTTP transport."""

    def __init__(
        self,
        registry: FixtureRegistry,
        *,
        compare: CompareFn | None = None,
        static_root: Traversable | Path | None = None,
        allowed_hosts: Iterable[str] | None = None,
    ) -> None:
        self.registry = registry
        self._compare = compare
        root = static_root if static_root is not None else _static_root()
        self._static: dict[str, Traversable | Path] = {}
        for item in root.iterdir():
            name = item.name
            suffix = Path(name).suffix.lower()
            if item.is_file() and _STATIC_NAME.match(name) and suffix in _CONTENT_TYPES:
                self._static[name] = item
        #: Lowercased ``Host`` header values accepted; ``None`` disables the check.
        self.allowed_hosts = (
            frozenset(h.lower() for h in allowed_hosts) if allowed_hosts is not None else None
        )

    @property
    def static_names(self) -> frozenset[str]:
        return frozenset(self._static)

    def handle(
        self, method: str, target: str, headers: Mapping[str, str] | None = None
    ) -> Response:
        """Return the response for one request."""
        if self.allowed_hosts is not None:
            host = (_header(headers, "Host") or "").strip().lower()
            if host not in self.allowed_hosts:
                return _error(
                    HTTPStatus.MISDIRECTED_REQUEST,
                    "Host header not allowed; open Studio via the URL it printed.",
                )
        if method not in ("GET", "HEAD"):
            resp = _error(HTTPStatus.METHOD_NOT_ALLOWED, "Studio is read-only; use GET.")
            resp.headers["Allow"] = "GET, HEAD"
            return resp
        try:
            parts = urlsplit(target)
        except ValueError:
            return _error(HTTPStatus.BAD_REQUEST, "malformed request target")
        path = parts.path
        if path in ("/", "/index.html"):
            return self._static_file("index.html")
        if path == "/favicon.svg" or path == "/favicon.ico":
            return self._static_file("favicon.svg")
        if path.startswith("/static/"):
            return self._static_file(path[len("/static/") :])
        if path == "/api/fixtures":
            return _json_response(
                200,
                {
                    "fixtures": self.registry.summaries(),
                    "warnings": list(self.registry.warnings),
                },
            )
        if path.startswith("/api/fixtures/"):
            return self._fixture(unquote(path[len("/api/fixtures/") :]))
        if path == "/api/diff":
            return self._diff(parse_qs(parts.query, keep_blank_values=True))
        if path.startswith("/api/"):
            return _error(HTTPStatus.NOT_FOUND, f"unknown API endpoint {path!r}")
        return _error(HTTPStatus.NOT_FOUND, "not found")

    # Routes ---------------------------------------------------------------
    def _static_file(self, name: str) -> Response:
        item = self._static.get(name) if _STATIC_NAME.match(name) else None
        if item is None:
            return _error(HTTPStatus.NOT_FOUND, "not found")
        content_type = _CONTENT_TYPES[Path(name).suffix.lower()]
        return Response(200, item.read_bytes(), content_type, {"Cache-Control": "no-cache"})

    def _lookup(self, key: str) -> Fixture | Response:
        if not _FIXTURE_ID.match(key) or key not in self.registry:
            return _error(HTTPStatus.NOT_FOUND, f"no fixture with id {key[:200]!r} was loaded")
        try:
            return self.registry.get(key)
        except FixtureError as exc:
            return _error(
                HTTPStatus.CONFLICT,
                f"fixture changed on disk and is no longer valid: {_first_line(str(exc))}",
            )

    def _fixture(self, key: str) -> Response:
        found = self._lookup(key)
        if isinstance(found, Response):
            return found
        return _json_response(200, found.model_dump(mode="json"))

    def _diff(self, query: Mapping[str, Sequence[str]]) -> Response:
        baseline_key = (query.get("baseline") or [""])[0]
        candidate_key = (query.get("candidate") or [""])[0]
        if not baseline_key or not candidate_key:
            return _error(
                HTTPStatus.BAD_REQUEST,
                "both 'baseline' and 'candidate' query parameters are required",
            )
        baseline = self._lookup(baseline_key)
        if isinstance(baseline, Response):
            return baseline
        candidate = self._lookup(candidate_key)
        if isinstance(candidate, Response):
            return candidate
        compare = self._compare if self._compare is not None else _default_compare()
        if compare is None:
            return _error(
                HTTPStatus.NOT_IMPLEMENTED,
                "diff is unavailable: traceon.diff could not be imported",
            )
        try:
            report = compare(baseline, candidate)
        except TraceonError as exc:
            return _error(
                HTTPStatus.UNPROCESSABLE_ENTITY, f"cannot compare: {_first_line(str(exc))}"
            )
        return _json_response(200, report.model_dump(mode="json"))


def _header(headers: Mapping[str, str] | None, name: str) -> str | None:
    if headers is None:
        return None
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


# ---------------------------------------------------------------------------
# HTTP transport
# ---------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    server: StudioServer
    server_version = "TraceonStudio"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        self._respond(send_body=True)

    def do_HEAD(self) -> None:
        self._respond(send_body=False)

    def _reject(self) -> None:
        self._respond(send_body=True)

    do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _reject

    def _respond(self, *, send_body: bool) -> None:
        resp = self.server.app.handle(self.command, self.path, dict(self.headers.items()))
        self.send_response(resp.status)
        self.send_header("Content-Type", resp.content_type)
        self.send_header("Content-Length", str(len(resp.body)))
        for key, value in SECURITY_HEADERS.items():
            self.send_header(key, value)
        for key, value in resp.headers.items():
            self.send_header(key, value)
        self.end_headers()
        if send_body and self.command != "HEAD":
            self.wfile.write(resp.body)

    def log_message(self, format: str, *args: Any) -> None:
        # Quiet by default: Studio is an interactive tool, not a web server log.
        return


class StudioServer(ThreadingHTTPServer):
    """Threading HTTP server carrying a :class:`StudioApp` as ``.app``."""

    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second process bind the same port and steal
    # requests; use SO_EXCLUSIVEADDRUSE there instead.
    allow_reuse_address = sys.platform != "win32"

    def __init__(self, address: tuple[str, int], app: StudioApp) -> None:
        self.app = app
        if ":" in address[0]:
            self.address_family = socket.AF_INET6
        super().__init__(address, _Handler)

    def server_bind(self) -> None:
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if sys.platform == "win32" and exclusive is not None:
            self.socket.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        # Skip HTTPServer.server_bind: its getfqdn() can block on reverse DNS.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)


def is_loopback(host: str) -> bool:
    """True if ``host`` names only the local machine."""
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def _allowed_hosts(host: str, port: int) -> set[str]:
    names = set(_LOOPBACK_NAMES) | {host.lower()}
    allowed: set[str] = set()
    for name in names:
        shown = f"[{name}]" if ":" in name else name
        allowed.add(f"{shown}:{port}")
        if port == 80:
            allowed.add(shown)
    return allowed


def make_server(
    paths: Sequence[str | Path],
    *,
    host: str = "127.0.0.1",
    port: int = 7777,
    recursive: bool = False,
    compare: CompareFn | None = None,
) -> StudioServer:
    """Build (but do not start) a Studio HTTP server over the fixtures in ``paths``.

    ``port=0`` picks a free port; read it back from ``server.server_address``.
    The returned server exposes ``server.app`` (:class:`StudioApp`).
    """
    registry = FixtureRegistry.scan(paths, recursive=recursive)
    app = StudioApp(registry, compare=compare)
    try:
        server = StudioServer((host, port), app)
    except OSError as exc:
        hint = f"; try --port {port + 1}" if port else ""
        raise StudioError(f"cannot listen on {host}:{port}: {exc.strerror or exc}{hint}") from exc
    bound_port = int(server.server_address[1])
    if is_loopback(host):
        app.allowed_hosts = frozenset(_allowed_hosts(host, bound_port))
    return server


def _url(host: str, port: int) -> str:
    shown = f"[{host}]" if ":" in host else host
    if host in ("0.0.0.0", "::"):
        shown = "127.0.0.1"
    return f"http://{shown}:{port}/"


def serve(
    paths: Sequence[str | Path],
    *,
    host: str = "127.0.0.1",
    port: int = 7777,
    open_browser: bool = True,
    recursive: bool = False,
    stream: TextIO | None = None,
) -> None:
    """Run Traceon Studio until interrupted (Ctrl+C).

    Only fixtures found in ``paths`` are served. Binding a non-loopback
    ``host`` exposes fixture contents to the network and prints a warning.
    """
    out = stream if stream is not None else sys.stderr
    server = make_server(paths, host=host, port=port, recursive=recursive)
    app = server.app
    bound_port = int(server.server_address[1])
    url = _url(host, bound_port)
    for warning in app.registry.warnings:
        out.write(f"warning: {warning}\n")
    if not is_loopback(host):
        out.write(
            "\n"
            "WARNING: Traceon Studio is listening on a non-loopback address "
            f"({host}).\n"
            "         Anyone who can reach this machine can read every loaded fixture,\n"
            "         including captured prompts, tool arguments and responses.\n"
            "         Studio has no authentication. Use --host 127.0.0.1 unless you\n"
            "         are on a trusted, isolated network.\n\n"
        )
    count = len(app.registry)
    noun = "fixture" if count == 1 else "fixtures"
    out.write(f"Traceon Studio: {count} {noun} at {url}  (Ctrl+C to stop)\n")
    out.flush()
    if open_browser:
        threading.Timer(0.3, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        out.write("Traceon Studio stopped.\n")
    finally:
        server.server_close()
