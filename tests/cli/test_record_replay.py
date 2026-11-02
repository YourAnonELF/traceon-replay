"""End-to-end ``traceon record`` / ``traceon replay`` against the real SDK and replay engine."""

from __future__ import annotations

import json

import pytest

from traceon.core.io import load_fixture


def record(invoke, *extra: str, entry: str = "agent_mod:run", out: str = "run.json"):
    return invoke("record", "--entry", entry, "--out", out, *extra)


class TestRecord:
    def test_records_dict_input_as_kwargs(self, invoke, project):
        result = record(invoke, "--input", '{"question": "replay"}')
        assert result.exit_code == 0, result.output
        assert "✓ recorded run  3 calls" in result.output
        assert "1 tool · 1 model · 1 value" in result.output
        fx = load_fixture(project / "run.json")
        assert fx.name == "run"
        assert fx.input.value == {"question": "replay"}
        assert fx.output.value == "SUMMARIZE DOC ABOUT REPLAY, DOC 2"
        assert [e.name for e in fx.calls()] == ["now", "search", "llm"]

    def test_custom_name_and_nested_out_dir(self, invoke, project):
        result = record(
            invoke,
            "--input",
            '{"question": "q"}',
            "--name",
            "nightly",
            out="fixtures/deep/run.json",
        )
        assert result.exit_code == 0, result.output
        assert load_fixture(project / "fixtures/deep/run.json").name == "nightly"

    def test_async_entry(self, invoke, project):
        result = record(invoke, "--input", '{"question": "async"}', entry="agent_mod:arun")
        assert result.exit_code == 0, result.output
        assert load_fixture(project / "run.json").output.value.startswith("SUMMARIZE DOC ABOUT")

    def test_scalar_input_is_positional(self, invoke, project):
        result = record(invoke, "--input", '"hello"', entry="agent_mod:positional")
        assert result.exit_code == 0, result.output
        fx = load_fixture(project / "run.json")
        assert fx.calls()[0].request.value == {"query": "hello"}

    def test_input_from_file(self, invoke, project):
        (project / "in.json").write_text('{"question": "from file"}', encoding="utf-8")
        result = record(invoke, "--input", "@in.json")
        assert result.exit_code == 0, result.output
        assert load_fixture(project / "run.json").input.value == {"question": "from file"}

    def test_no_input(self, invoke, project):
        assert record(invoke, entry="agent_mod:no_args").exit_code == 0

    def test_entry_given_as_file_path(self, invoke, project):
        result = record(invoke, "--input", '{"question": "x"}', entry="agent_mod.py:run")
        assert result.exit_code == 0, result.output

    def test_entry_that_raises_still_writes_fixture(self, invoke, project):
        result = record(invoke, "--input", '{"question": "x"}', entry="agent_mod:boom")
        assert result.exit_code == 1
        assert "the entry raised ValueError: agent exploded" in result.output
        fx = load_fixture(project / "run.json")
        assert fx.status == "error"
        assert fx.error is not None and "agent exploded" in fx.error.message

    @pytest.mark.parametrize(
        ("entry", "message"),
        [
            ("agent_mod", "invalid --entry 'agent_mod'"),
            ("agent_mod:", "invalid --entry"),
            ("missing_mod:run", "cannot import module 'missing_mod'"),
            ("agent_mod:nope", "'agent_mod' has no attribute 'nope'"),
            ("agent_mod:NOT_CALLABLE", "is not callable"),
            ("nowhere.py:run", "entry file not found"),
        ],
    )
    def test_bad_entry(self, invoke, project, entry, message):
        result = record(invoke, entry=entry)
        assert result.exit_code == 2
        assert message in result.stderr
        assert "Traceback" not in result.output
        assert not (project / "run.json").exists()

    def test_entry_with_import_error_inside(self, invoke, project):
        (project / "broken_mod.py").write_text("import not_a_real_dependency\n", encoding="utf-8")
        result = record(invoke, entry="broken_mod:run")
        assert result.exit_code == 2
        assert "importing 'broken_mod' failed" in result.stderr
        assert "not_a_real_dependency" in result.stderr

    def test_input_signature_mismatch(self, invoke, project):
        result = record(invoke, "--input", '{"wrong": 1}')
        assert result.exit_code == 2
        assert "input does not match run(question: str)" in result.stderr

    @pytest.mark.parametrize("value", ["{not json", "hello"])
    def test_invalid_json_input(self, invoke, project, value):
        result = record(invoke, "--input", value)
        assert result.exit_code == 2
        assert "--input is not valid JSON" in result.stderr
        assert "hint" in result.stderr

    def test_missing_input_file(self, invoke, project):
        result = record(invoke, "--input", "@nope.json")
        assert result.exit_code == 2
        assert "cannot read --input file" in result.stderr

    def test_required_options(self, invoke, project):
        assert invoke("record", "--out", "x.json").exit_code == 2
        assert invoke("record", "--entry", "agent_mod:run").exit_code == 2


@pytest.fixture
def recorded(invoke, project):
    result = record(invoke, "--input", '{"question": "replay"}')
    assert result.exit_code == 0, result.output
    return project / "run.json"


class TestReplay:
    def test_matching_replay(self, invoke, recorded):
        result = invoke("replay", str(recorded), "--entry", "agent_mod:run")
        assert result.exit_code == 0, result.output
        assert "✓ replay matches the recording" in result.output
        assert "3/3 served from recording" in result.output
        assert "network  blocked" in result.output

    def test_async_replay_by_key(self, invoke, project):
        assert record(invoke, "--input", '{"question": "a"}', entry="agent_mod:arun").exit_code == 0
        result = invoke("replay", "run.json", "--entry", "agent_mod:arun", "--by-key")
        assert result.exit_code == 0, result.output

    def test_divergence_exit_code_and_block(self, invoke, recorded, monkeypatch):
        monkeypatch.setenv("AGENT_SUFFIX", " v2")
        result = invoke("replay", str(recorded), "--entry", "agent_mod:run")
        assert result.exit_code == 1
        out = result.output
        assert "✗ replay diverged" in out
        assert "✗ first divergence  argument_mismatch · call #1 · tool:search" in out
        assert "    path      $.query" in out
        assert '    expected  "replay"' in out
        assert '    actual    "replay v2"' in out
        assert "missing_call" in out

    def test_save_replayed_fixture(self, invoke, recorded, project):
        result = invoke(
            "replay", str(recorded), "--entry", "agent_mod:run", "--save", "replayed.json"
        )
        assert result.exit_code == 0, result.output
        replayed = load_fixture(project / "replayed.json")
        assert replayed.mode == "replay"
        diff = invoke("diff", str(recorded), "replayed.json")
        assert diff.exit_code == 0, diff.output

    def test_override_constant(self, invoke, recorded):
        result = invoke(
            "replay",
            str(recorded),
            "--entry",
            "agent_mod:run",
            "--override",
            'tool:search=["patched"]',
        )
        assert "1 overridden" in result.output
        # The model now sees a different prompt than recorded (hash-only), so it diverges.
        assert result.exit_code == 1
        assert "model:llm" in result.output

    def test_input_override(self, invoke, recorded):
        result = invoke(
            "replay",
            str(recorded),
            "--entry",
            "agent_mod:run",
            "--input",
            '{"question": "different"}',
        )
        assert result.exit_code == 1
        assert '"different"' in result.output

    @pytest.mark.parametrize("override", ["no-equals", "=1", "search={bad"])
    def test_bad_override(self, invoke, recorded, override):
        result = invoke("replay", str(recorded), "--entry", "agent_mod:run", "--override", override)
        assert result.exit_code == 2
        assert "--override" in result.stderr

    def test_missing_fixture(self, invoke, project):
        result = invoke("replay", "nope.json", "--entry", "agent_mod:run")
        assert result.exit_code == 2
        assert "fixture not found" in result.stderr

    def test_invalid_fixture(self, invoke, project):
        (project / "bad.json").write_text("[]", encoding="utf-8")
        result = invoke("replay", "bad.json", "--entry", "agent_mod:run")
        assert result.exit_code == 2
        assert "fixture must be a JSON object" in result.stderr

    def test_bad_entry(self, invoke, recorded):
        result = invoke("replay", str(recorded), "--entry", "agent_mod:missing")
        assert result.exit_code == 2
        assert "has no attribute 'missing'" in result.stderr

    def test_uncaptured_input_is_reported(self, invoke, recorded, project):
        data = json.loads(recorded.read_text(encoding="utf-8"))
        data["input"] = {"state": "redacted"}
        (project / "redacted.json").write_text(json.dumps(data), encoding="utf-8")
        result = invoke("replay", "redacted.json", "--entry", "agent_mod:run")
        assert result.exit_code == 2
        assert "recorded input cannot be replayed (redacted)" in result.stderr
        assert "--input" in result.stderr
