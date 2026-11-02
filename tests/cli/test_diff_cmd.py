from __future__ import annotations

import json


def test_diff_equivalent(invoke, fixtures_dir):
    result = invoke("diff", str(fixtures_dir["base"]), str(fixtures_dir["same"]))
    assert result.exit_code == 0
    assert "✓ equivalent" in result.output


def test_diff_divergent(invoke, fixtures_dir):
    result = invoke("diff", str(fixtures_dir["base"]), str(fixtures_dir["changed"]))
    assert result.exit_code == 1
    out = result.output
    assert "✗ first divergence  argument_mismatch · call #2 · tool:search" in out
    assert '    expected  "weather in paris"' in out
    assert '    actual    "weather in Paris"' in out
    assert "✗ not equivalent  2 divergences" in out
    # Labels are the file paths. Long temp paths (macOS, Windows CI runners) are
    # soft-wrapped by the console, so compare with whitespace removed.
    assert "".join(str(fixtures_dir["base"]).split()) in "".join(out.split())


def test_diff_ignore(invoke, fixtures_dir, fxf, tmp_path):
    from traceon.core.io import save_fixture

    other = save_fixture(fxf.simple_run("fx_q", query="other"), tmp_path / "q.json")
    base = str(fixtures_dir["base"])
    assert invoke("diff", base, str(other)).exit_code == 1
    assert invoke("diff", base, str(other), "--ignore", "$.query").exit_code == 0


def test_diff_no_output(invoke, fxf, tmp_path):
    from traceon.core.io import save_fixture

    a = save_fixture(fxf.FixtureBuilder("r", "fx_a").build(output=1), tmp_path / "a.json")
    b = save_fixture(fxf.FixtureBuilder("r", "fx_b").build(output=2), tmp_path / "b.json")
    assert invoke("diff", str(a), str(b)).exit_code == 1
    assert invoke("diff", str(a), str(b), "--no-output").exit_code == 0


def test_diff_json(invoke, fixtures_dir):
    result = invoke("diff", str(fixtures_dir["base"]), str(fixtures_dir["changed"]), "--json")
    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["equivalent"] is False
    assert data["first_divergence"]["path"] == "$.query"
    assert [p["status"] for p in data["alignment"]] == ["same", "same", "changed", "added", "same"]


def test_diff_markdown(invoke, fixtures_dir):
    result = invoke("diff", str(fixtures_dir["base"]), str(fixtures_dir["changed"]), "--markdown")
    assert result.exit_code == 1
    assert result.output.startswith("### Traceon diff: 2 divergences\n")
    assert '```diff\n- "weather in paris"\n+ "weather in Paris"\n```' in result.output


def test_diff_missing_candidate(invoke, fixtures_dir, tmp_path):
    result = invoke("diff", str(fixtures_dir["base"]), str(tmp_path / "missing.json"))
    assert result.exit_code == 2
    assert "fixture not found" in result.stderr


def test_diff_requires_two_arguments(invoke, fixtures_dir):
    assert invoke("diff", str(fixtures_dir["base"])).exit_code == 2
