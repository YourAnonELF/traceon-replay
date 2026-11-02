from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

from traceon.core.io import load_fixture

LANGFUSE_TRACE = {
    "id": "tr1",
    "name": "lf-agent",
    "timestamp": "2026-01-01T00:00:00Z",
    "input": {"q": 1},
    "output": "ok",
    "observations": [
        {
            "id": "o1",
            "traceId": "tr1",
            "type": "GENERATION",
            "name": "gpt",
            "startTime": "2026-01-01T00:00:01Z",
            "endTime": "2026-01-01T00:00:02Z",
            "input": "hi",
            "output": "yo",
            "usage": {"input": 3, "output": 4},
            "weird": 1,
        },
        {
            "id": "o2",
            "traceId": "tr1",
            "type": "TOOL",
            "name": "search",
            "startTime": "2026-01-01T00:00:03Z",
            "endTime": "2026-01-01T00:00:04Z",
            "input": {"q": "x"},
            "output": [1],
        },
    ],
}


class TestImportLangfuse:
    def test_import_from_file_with_real_importer(self, invoke, tmp_path):
        src = tmp_path / "trace.json"
        src.write_text(json.dumps(LANGFUSE_TRACE), encoding="utf-8")
        out = tmp_path / "out" / "fx.json"
        result = invoke("import", "langfuse", "--file", str(src), "--out", str(out))
        assert result.exit_code == 0, result.output
        assert "✓ imported" in result.output and "3 events" in result.output
        assert "1 field could not be mapped" in result.output
        fx = load_fixture(out)
        assert fx.source == "langfuse"
        assert [e.kind for e in fx.calls()] == ["model", "tool"]

    def test_import_by_trace_id_uses_fetch(self, invoke, tmp_path, monkeypatch):
        import traceon.integrations.langfuse as lf

        calls = {}

        def fake_fetch(trace_id, *, host=None, **kwargs):
            calls["args"] = (trace_id, host)
            return LANGFUSE_TRACE

        monkeypatch.setattr(lf, "fetch_trace", fake_fetch)
        out = tmp_path / "fx.json"
        result = invoke(
            "import",
            "langfuse",
            "--trace-id",
            "tr1",
            "--host",
            "https://lf.example",
            "--out",
            str(out),
        )
        assert result.exit_code == 0, result.output
        assert calls["args"] == ("tr1", "https://lf.example")
        assert "trace tr1" in result.output
        assert out.exists()

    def test_fetch_error_is_concise(self, invoke, tmp_path, monkeypatch):
        import traceon.integrations.langfuse as lf
        from traceon.core.errors import TraceonError

        def failing(trace_id, **kwargs):
            raise TraceonError("Langfuse returned HTTP 401: check LANGFUSE_PUBLIC_KEY")

        monkeypatch.setattr(lf, "fetch_trace", failing)
        result = invoke("import", "langfuse", "--trace-id", "x", "--out", str(tmp_path / "o.json"))
        assert result.exit_code == 2
        assert "error: Langfuse returned HTTP 401" in result.stderr

    def test_network_error_is_concise(self, invoke, tmp_path, monkeypatch):
        import traceon.integrations.langfuse as lf

        def failing(trace_id, **kwargs):
            raise ConnectionRefusedError(111, "Connection refused")

        monkeypatch.setattr(lf, "fetch_trace", failing)
        result = invoke("import", "langfuse", "--trace-id", "x", "--out", str(tmp_path / "o.json"))
        assert result.exit_code == 2
        assert "Connection refused" in result.stderr

    @pytest.mark.parametrize("args", [[], ["--trace-id", "a", "--file", "b.json"]])
    def test_exactly_one_source(self, invoke, tmp_path, args):
        result = invoke("import", "langfuse", *args, "--out", str(tmp_path / "o.json"))
        assert result.exit_code == 2
        assert "exactly one of --trace-id or --file" in result.output

    def test_invalid_json_file(self, invoke, tmp_path):
        src = tmp_path / "t.json"
        src.write_text("{nope", encoding="utf-8")
        result = invoke("import", "langfuse", "--file", str(src), "--out", str(tmp_path / "o.json"))
        assert result.exit_code == 2
        assert "is not valid JSON" in result.stderr

    def test_non_object_json(self, invoke, tmp_path):
        src = tmp_path / "t.json"
        src.write_text("[1, 2]", encoding="utf-8")
        result = invoke("import", "langfuse", "--file", str(src), "--out", str(tmp_path / "o.json"))
        assert result.exit_code == 2
        assert "expected a JSON object" in result.stderr

    def test_missing_file(self, invoke, tmp_path):
        result = invoke(
            "import",
            "langfuse",
            "--file",
            str(tmp_path / "none.json"),
            "--out",
            str(tmp_path / "o.json"),
        )
        assert result.exit_code == 2
        assert "cannot read" in result.stderr

    def test_importer_unavailable(self, invoke, tmp_path, monkeypatch):
        monkeypatch.setitem(sys.modules, "traceon.integrations.langfuse", None)
        result = invoke("import", "langfuse", "--trace-id", "x", "--out", str(tmp_path / "o.json"))
        assert result.exit_code == 2
        assert "the Langfuse importer is unavailable" in result.stderr


class TestStudio:
    @pytest.fixture
    def fake_server(self, monkeypatch):
        calls: dict[str, object] = {}
        module = types.ModuleType("traceon.studio.server")

        def serve(paths, *, host, port, open_browser, recursive):
            calls.update(
                paths=paths, host=host, port=port, open_browser=open_browser, recursive=recursive
            )

        module.serve = serve  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "traceon.studio.server", module)
        return calls

    def test_delegates_to_serve(self, invoke, fake_server, tmp_path):
        result = invoke("studio", str(tmp_path), "--port", "8123", "--no-open")
        assert result.exit_code == 0, result.output
        assert fake_server == {
            "paths": [tmp_path],
            "host": "127.0.0.1",
            "port": 8123,
            "open_browser": False,
            "recursive": False,
        }

    def test_recursive_flag(self, invoke, fake_server, tmp_path):
        assert invoke("studio", str(tmp_path), "--recursive").exit_code == 0
        assert fake_server["recursive"] is True

    def test_defaults_to_current_directory(self, invoke, fake_server, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert invoke("studio").exit_code == 0
        assert fake_server["paths"] == [Path.cwd()]
        assert fake_server["open_browser"] is True

    def test_missing_path_is_usage_error(self, invoke, fake_server, tmp_path):
        assert invoke("studio", str(tmp_path / "nope")).exit_code == 2

    def test_bad_port(self, invoke, fake_server):
        assert invoke("studio", "--port", "70000").exit_code == 2

    def test_bind_error(self, invoke, monkeypatch):
        module = types.ModuleType("traceon.studio.server")

        def serve(paths, **kwargs):
            raise OSError(98, "Address already in use")

        module.serve = serve  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "traceon.studio.server", module)
        result = invoke("studio", "--port", "7777")
        assert result.exit_code == 2
        assert "cannot serve on 127.0.0.1:7777: Address already in use" in result.stderr
        assert "try --port 7778" in result.stderr

    def test_real_studio_error_port_in_use(self, invoke, fixtures_dir):
        from traceon.studio.server import make_server

        busy = make_server([fixtures_dir["dir"]], port=0)
        try:
            port = busy.server_address[1]
            result = invoke("studio", str(fixtures_dir["dir"]), "--port", str(port), "--no-open")
        finally:
            busy.server_close()
        assert result.exit_code == 2
        assert f"error: cannot listen on 127.0.0.1:{port}" in result.stderr
        assert "--port" in result.stderr
        assert "Traceback" not in result.output

    def test_real_server_serves_fixtures(self, fixtures_dir):
        import json as _json
        import threading
        import urllib.request

        from traceon.studio.server import make_server

        server = make_server([fixtures_dir["dir"]], port=0)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
        thread.start()
        try:
            port = server.server_address[1]
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/diff?baseline=fx_base&candidate=fx_changed", timeout=5
            ) as resp:
                report = _json.loads(resp.read())
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
        assert report["equivalent"] is False
        assert report["first_divergence"]["path"] == "$.query"

    def test_keyboard_interrupt_exits_cleanly(self, invoke, monkeypatch):
        module = types.ModuleType("traceon.studio.server")

        def serve(paths, **kwargs):
            raise KeyboardInterrupt

        module.serve = serve  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "traceon.studio.server", module)
        assert invoke("studio").exit_code == 0
