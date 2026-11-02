"""Socket-free tests of request routing and policy in StudioApp."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from studio_samples import BASELINE_ID, CANDIDATE_ID

from traceon.core.errors import FixtureError
from traceon.core.schema import DiffReport, Fixture, RunMetrics
from traceon.studio import server as server_mod
from traceon.studio.server import FixtureRegistry, StudioApp


def fake_compare(baseline: Fixture, candidate: Fixture) -> DiffReport:
    return DiffReport(
        baseline_id=baseline.fixture_id,
        candidate_id=candidate.fixture_id,
        baseline_name=baseline.name,
        candidate_name=candidate.name,
        equivalent=False,
        baseline_metrics=RunMetrics(status=baseline.status),
        candidate_metrics=RunMetrics(status=candidate.status),
    )


@pytest.fixture
def app(fixture_dir: Path) -> StudioApp:
    return StudioApp(FixtureRegistry.scan([fixture_dir]), compare=fake_compare)


def body(resp: server_mod.Response) -> Any:
    return json.loads(resp.body.decode("utf-8"))


def test_index_and_favicon(app: StudioApp) -> None:
    for path in ("/", "/index.html"):
        resp = app.handle("GET", path)
        assert resp.status == 200
        assert resp.content_type.startswith("text/html")
        assert b"Traceon Studio" in resp.body
    fav = app.handle("GET", "/favicon.svg")
    assert fav.status == 200 and fav.content_type == "image/svg+xml"


def test_static_assets_served_with_types(app: StudioApp) -> None:
    for name, ctype in (
        ("app.js", "text/javascript"),
        ("styles.css", "text/css"),
        ("theme.js", "text/javascript"),
    ):
        resp = app.handle("GET", f"/static/{name}")
        assert resp.status == 200, name
        assert resp.content_type.startswith(ctype)


def test_static_allowlist_is_flat_and_typed(app: StudioApp) -> None:
    names = app.static_names
    assert {"index.html", "app.js", "styles.css", "favicon.svg"} <= names
    assert all("/" not in n and "\\" not in n for n in names)
    assert all(not n.endswith(".py") for n in names)


@pytest.mark.parametrize(
    "target",
    [
        "/static/../server.py",
        "/static/..%2fserver.py",
        "/static/%2e%2e/server.py",
        "/static/%2e%2e%2fserver.py",
        "/static/..\\server.py",
        "/static/..%5cserver.py",
        "/static//etc/passwd",
        "/static/C:/Windows/win.ini",
        "/static/",
        "/static/.hidden.js",
        "/static/app.js%00.png",
        "/static/nested/app.js",
        "/../../etc/passwd",
        "/server.py",
        "/__init__.py",
        "/etc/passwd",
    ],
)
def test_static_traversal_rejected(app: StudioApp, target: str) -> None:
    resp = app.handle("GET", target)
    assert resp.status == 404
    assert b"import" not in resp.body


def test_list_fixtures(app: StudioApp) -> None:
    resp = app.handle("GET", "/api/fixtures")
    assert resp.status == 200
    assert resp.headers["Cache-Control"] == "no-store"
    data = body(resp)
    assert [f["id"] for f in data["fixtures"]] == [CANDIDATE_ID, BASELINE_ID]
    assert any("broken.json" in w for w in data["warnings"])


def test_get_fixture_round_trips(app: StudioApp) -> None:
    resp = app.handle("GET", f"/api/fixtures/{BASELINE_ID}")
    assert resp.status == 200
    fx = Fixture.model_validate(body(resp))
    assert fx.fixture_id == BASELINE_ID


def test_get_fixture_query_string_ignored(app: StudioApp) -> None:
    assert app.handle("GET", f"/api/fixtures/{BASELINE_ID}?x=1").status == 200


@pytest.mark.parametrize(
    "key",
    [
        "nope",
        "..",
        "../baseline.json",
        "..%2f..%2fetc%2fpasswd",
        "%2e%2e",
        "baseline.json",
        "candidate",
        "fx_nested000001",  # exists on disk, but in a subdirectory that was not scanned
        "C:%5cWindows%5cwin.ini",
        "%00",
        "a" * 300,
    ],
)
def test_unknown_or_hostile_fixture_ids_404(app: StudioApp, key: str) -> None:
    resp = app.handle("GET", f"/api/fixtures/{key}")
    assert resp.status == 404
    assert body(resp)["error"]["status"] == 404


def test_only_listed_files_are_served(fixture_dir: Path) -> None:
    app = StudioApp(FixtureRegistry.scan([fixture_dir / "baseline.json"]), compare=fake_compare)
    assert app.handle("GET", f"/api/fixtures/{BASELINE_ID}").status == 200
    assert app.handle("GET", f"/api/fixtures/{CANDIDATE_ID}").status == 404
    assert len(body(app.handle("GET", "/api/fixtures"))["fixtures"]) == 1


def test_unknown_api_route(app: StudioApp) -> None:
    resp = app.handle("GET", "/api/everything")
    assert resp.status == 404
    assert "unknown API endpoint" in body(resp)["error"]["message"]


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
def test_read_only(app: StudioApp, method: str) -> None:
    resp = app.handle(method, "/api/fixtures")
    assert resp.status == 405
    assert resp.headers["Allow"] == "GET, HEAD"


def test_diff_ok(app: StudioApp) -> None:
    resp = app.handle("GET", f"/api/diff?baseline={BASELINE_ID}&candidate={CANDIDATE_ID}")
    assert resp.status == 200
    report = DiffReport.model_validate(body(resp))
    assert (report.baseline_id, report.candidate_id) == (BASELINE_ID, CANDIDATE_ID)


@pytest.mark.parametrize(
    "query",
    ["", "?baseline=x", f"?candidate={CANDIDATE_ID}", f"?baseline=&candidate={CANDIDATE_ID}"],
)
def test_diff_requires_both_params(app: StudioApp, query: str) -> None:
    assert app.handle("GET", f"/api/diff{query}").status == 400


def test_diff_unknown_fixture(app: StudioApp) -> None:
    resp = app.handle("GET", f"/api/diff?baseline={BASELINE_ID}&candidate=..%2f..%2fx")
    assert resp.status == 404


def test_diff_unavailable_returns_501(fixture_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server_mod, "_default_compare", lambda: None)
    app = StudioApp(FixtureRegistry.scan([fixture_dir]))
    resp = app.handle("GET", f"/api/diff?baseline={BASELINE_ID}&candidate={CANDIDATE_ID}")
    assert resp.status == 501
    assert "traceon.diff" in body(resp)["error"]["message"]


def test_diff_default_compare_used_lazily(
    fixture_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str]] = []

    def recording(b: Fixture, c: Fixture) -> DiffReport:
        calls.append((b.fixture_id, c.fixture_id))
        return fake_compare(b, c)

    monkeypatch.setattr(server_mod, "_default_compare", lambda: recording)
    app = StudioApp(FixtureRegistry.scan([fixture_dir]))
    assert calls == []
    resp = app.handle("GET", f"/api/diff?baseline={CANDIDATE_ID}&candidate={BASELINE_ID}")
    assert resp.status == 200
    assert calls == [(CANDIDATE_ID, BASELINE_ID)]


def test_default_compare_handles_missing_module(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "traceon.diff":
            raise ImportError("simulated")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert server_mod._default_compare() is None


def test_diff_compare_error_is_422(fixture_dir: Path) -> None:
    def failing(b: Fixture, c: Fixture) -> DiffReport:
        raise FixtureError("schema mismatch between runs")

    app = StudioApp(FixtureRegistry.scan([fixture_dir]), compare=failing)
    resp = app.handle("GET", f"/api/diff?baseline={BASELINE_ID}&candidate={CANDIDATE_ID}")
    assert resp.status == 422
    assert "schema mismatch" in body(resp)["error"]["message"]


def test_fixture_invalid_after_change_is_409(fixture_dir: Path) -> None:
    import os

    path = fixture_dir / "baseline.json"
    app = StudioApp(FixtureRegistry.scan([path]))
    path.write_text("{}", encoding="utf-8")
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    resp = app.handle("GET", f"/api/fixtures/{BASELINE_ID}")
    assert resp.status == 409
    assert "no longer valid" in body(resp)["error"]["message"]


def test_host_header_check(fixture_dir: Path) -> None:
    app = StudioApp(
        FixtureRegistry.scan([fixture_dir]), allowed_hosts={"127.0.0.1:7777", "localhost:7777"}
    )
    assert app.handle("GET", "/api/fixtures", {"Host": "127.0.0.1:7777"}).status == 200
    assert app.handle("GET", "/api/fixtures", {"host": "LOCALHOST:7777"}).status == 200
    assert app.handle("GET", "/api/fixtures", {"Host": "evil.example:7777"}).status == 421
    assert app.handle("GET", "/api/fixtures", {}).status == 421
    assert app.handle("GET", "/api/fixtures").status == 421


def test_allowed_hosts_and_loopback_helpers() -> None:
    assert server_mod.is_loopback("127.0.0.1")
    assert server_mod.is_loopback("127.1.2.3")
    assert server_mod.is_loopback("::1")
    assert server_mod.is_loopback("localhost")
    assert not server_mod.is_loopback("0.0.0.0")
    assert not server_mod.is_loopback("192.168.1.10")
    assert not server_mod.is_loopback("example.com")
    hosts = server_mod._allowed_hosts("127.0.0.1", 8123)
    assert {"127.0.0.1:8123", "localhost:8123", "[::1]:8123"} <= hosts
    assert "127.0.0.1" not in hosts
