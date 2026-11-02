from __future__ import annotations

import json

from traceon import __version__
from traceon.core.io import save_fixture


def test_version(invoke):
    result = invoke("--version")
    assert result.exit_code == 0
    assert result.output.strip() == f"traceon {__version__}"


def test_help_lists_commands(invoke):
    result = invoke("--help")
    assert result.exit_code == 0
    for name in ("inspect", "validate", "record", "replay", "diff", "import", "studio"):
        assert name in result.output


def test_every_command_has_examples(invoke):
    for args in (
        ["inspect"],
        ["validate"],
        ["record"],
        ["replay"],
        ["diff"],
        ["import", "langfuse"],
        ["studio"],
    ):
        result = invoke(*args, "--help")
        assert result.exit_code == 0, args
        assert "Examples:" in result.output, args


def test_unknown_command_is_usage_error(invoke):
    assert invoke("frobnicate").exit_code == 2


def test_inspect_tree(invoke, fixtures_dir):
    result = invoke("inspect", str(fixtures_dir["base"]))
    assert result.exit_code == 0
    out = result.output
    assert out.splitlines()[0] == "agent  fx_base"
    assert "recorded 2026-01-01 12:00:00 UTC" in out
    assert "4 calls: 1 tool · 2 model · 1 value" in out
    assert "400 in · 70 out" in out
    assert "$0.0020 measured · $0.0040 estimated" in out
    assert "├─ ✓ value:now" in out
    assert "└─ ✓ model:writer" in out
    assert "\x1b[" not in out  # not a TTY: no color


def test_inspect_tree_durations_are_right_aligned(invoke, fixtures_dir):
    out = invoke("inspect", str(fixtures_dir["base"])).output
    rows = [line for line in out.splitlines() if line.startswith(("├─", "└─", "agent "))]
    rows = [r for r in rows if r.endswith("ms")]
    assert len(rows) >= 4
    assert len({len(r) for r in rows}) == 1


def test_inspect_shows_errors_and_side_effects(invoke, tmp_path, fxf):
    b = fxf.FixtureBuilder("failing", "fx_err")
    b.tool("pay", {"amount": 5}, {"ok": True}, side_effect=True)
    b.tool("fetch", {}, status="error", error=("builtins.TimeoutError", "timed out"))
    path = save_fixture(
        b.build(
            status="error",
            error=("builtins.TimeoutError", "timed out"),
            warnings=["unmapped field: foo"],
        ),
        tmp_path / "f.json",
    )
    out = invoke("inspect", str(path)).output
    assert "✗ error  builtins.TimeoutError: timed out" in out
    assert "✗ tool:fetch" in out and "builtins.TimeoutError: timed out" in out
    assert "side effect" in out
    assert "warnings (1)" in out and "unmapped field: foo" in out


def test_inspect_events_table(invoke, fixtures_dir):
    result = invoke("inspect", str(fixtures_dir["base"]), "--events")
    assert result.exit_code == 0
    assert "seq" in result.output and "parent" in result.output
    assert "tool:search" in result.output and "span:agent" in result.output


def test_inspect_json(invoke, fixtures_dir):
    result = invoke("inspect", str(fixtures_dir["base"]), "--json")
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["fixture_id"] == "fx_base"
    assert data["metrics"]["tool_calls"] == 1
    assert data["metrics"]["cost_measured"] == 0.002
    assert [e["kind"] for e in data["events"]] == ["span", "value", "model", "tool", "model"]


def test_inspect_missing_file(invoke, tmp_path):
    result = invoke("inspect", str(tmp_path / "nope.json"))
    assert result.exit_code == 2
    assert "error: fixture not found" in result.stderr
    assert "Traceback" not in result.output


def test_inspect_bad_fixture(invoke, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"schema_version": "traceon.fixture/v0"}', encoding="utf-8")
    result = invoke("inspect", str(bad))
    assert result.exit_code == 2
    assert "unsupported schema_version" in result.stderr


def test_debug_shows_traceback(invoke, tmp_path):
    from traceon.core.errors import FixtureError

    try:
        invoke("--debug", "inspect", str(tmp_path / "nope.json"))
    except FixtureError as exc:
        assert "fixture not found" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("--debug should re-raise")


def test_no_color_env_and_color_always(invoke, fixtures_dir):
    path = str(fixtures_dir["base"])
    assert "\x1b[" in invoke("--color", "always", "inspect", path).output
    # An explicit --color always wins over NO_COLOR.
    assert "\x1b[" in invoke("--color", "always", "inspect", path, env={"NO_COLOR": "1"}).output
    assert "\x1b[" not in invoke("inspect", path, env={"NO_COLOR": "1"}).output
    assert "\x1b[" not in invoke("--color", "never", "inspect", path).output


def test_validate_ok(invoke, fixtures_dir):
    result = invoke("validate", str(fixtures_dir["base"]), str(fixtures_dir["same"]))
    assert result.exit_code == 0
    assert result.output.count("✓") == 2
    assert "2 fixtures valid" in result.output


def test_validate_reports_each_invalid_file(invoke, fixtures_dir, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    result = invoke("validate", str(fixtures_dir["base"]), str(bad), str(tmp_path / "gone.json"))
    assert result.exit_code == 2
    assert "fixture is not valid JSON" in result.output
    assert "fixture not found" in result.output
    assert "2 of 3 invalid" in result.output


def test_validate_schema_violation(invoke, tmp_path, fxf):
    fx = fxf.simple_run().model_dump(mode="json")
    fx["events"][2]["sequence"] = 0  # not strictly increasing
    path = tmp_path / "seq.json"
    path.write_text(json.dumps(fx), encoding="utf-8")
    result = invoke("validate", str(path))
    assert result.exit_code == 2
    assert "sequence" in result.output


def test_validate_warnings_and_strict(invoke, tmp_path, fxf):
    fx = fxf.simple_run()
    fx.events[2].call_key = None
    path = save_fixture(fx, tmp_path / "w.json")
    result = invoke("validate", str(path))
    assert result.exit_code == 0
    assert "1 warning" in result.output and "has no call_key" in result.output
    assert invoke("validate", "--strict", str(path)).exit_code == 2


def test_validate_requires_argument(invoke):
    assert invoke("validate").exit_code == 2
