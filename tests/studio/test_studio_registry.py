from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest
from studio_samples import BASELINE, BASELINE_ID, CANDIDATE, CANDIDATE_ID

from traceon.core.errors import FixtureError
from traceon.core.io import dump_fixture, load_fixture
from traceon.studio.server import FixtureRegistry, run_duration_ms, summarize_fixture


def test_scan_directory_loads_fixtures_and_skips_others(fixture_dir: Path) -> None:
    reg = FixtureRegistry.scan([fixture_dir])
    assert sorted(reg.keys()) == sorted([BASELINE_ID, CANDIDATE_ID])
    joined = "\n".join(reg.warnings)
    assert "broken.json" in joined
    assert "other-tool.json" in joined
    assert "notes.txt" not in joined  # only *.json is considered
    assert "deep.json" not in joined  # not recursive by default


def test_scan_recursive(fixture_dir: Path) -> None:
    reg = FixtureRegistry.scan([fixture_dir], recursive=True)
    assert "fx_nested000001" in reg
    assert len(reg) == 3


def test_scan_explicit_file_only(fixture_dir: Path) -> None:
    reg = FixtureRegistry.scan([fixture_dir / "baseline.json"])
    assert reg.keys() == [BASELINE_ID]
    assert CANDIDATE_ID not in reg


def test_scan_reports_missing_and_invalid_explicit_paths(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("[]", encoding="utf-8")
    reg = FixtureRegistry.scan([tmp_path / "missing.json", bad, tmp_path])
    assert len(reg) == 0
    assert any("no such file" in w for w in reg.warnings)
    assert any(w.startswith(str(bad)) and "skipped" in w for w in reg.warnings)


def test_scan_empty_directory_warns(tmp_path: Path) -> None:
    reg = FixtureRegistry.scan([tmp_path])
    assert len(reg) == 0
    assert reg.warnings == [f"{tmp_path}: no *.json files found"]


def test_same_file_listed_twice_is_loaded_once(fixture_dir: Path) -> None:
    reg = FixtureRegistry.scan([fixture_dir / "baseline.json", fixture_dir])
    assert sorted(reg.keys()) == sorted([BASELINE_ID, CANDIDATE_ID])


def test_duplicate_fixture_ids_are_disambiguated(tmp_path: Path) -> None:
    (tmp_path / "a.json").write_bytes(BASELINE.read_bytes())
    (tmp_path / "b.json").write_bytes(BASELINE.read_bytes())
    reg = FixtureRegistry.scan([tmp_path])
    assert sorted(reg.keys()) == [BASELINE_ID, f"{BASELINE_ID}~2"]
    assert any("duplicate fixture_id" in w for w in reg.warnings)


def test_summary_fields() -> None:
    fx = load_fixture(BASELINE)
    s = summarize_fixture(fx)
    assert s["fixture_id"] == BASELINE_ID
    assert s["events"] == len(fx.events)
    assert s["calls"] == len(fx.calls())
    assert s["tool_calls"] + s["model_calls"] + s["value_calls"] == s["calls"]
    assert s["errors"] == 1
    assert s["input_tokens"] == sum(e.usage.input_tokens or 0 for e in fx.events if e.usage)
    assert s["cost_measured"] is not None and s["cost_estimated"] is not None
    assert s["currency"] == "USD"
    assert s["duration_ms"] == pytest.approx(14906.0)


def test_run_duration_empty() -> None:
    fx = load_fixture(BASELINE).model_copy(update={"events": []})
    assert run_duration_ms(fx) is None


def test_summaries_sorted_newest_first(fixture_dir: Path) -> None:
    reg = FixtureRegistry.scan([fixture_dir])
    ids = [s["id"] for s in reg.summaries()]
    assert ids == [CANDIDATE_ID, BASELINE_ID]
    assert {s["file"] for s in reg.summaries()} == {"baseline.json", "candidate.json"}


def test_get_reloads_changed_file_and_rejects_invalid(fixture_dir: Path) -> None:
    path = fixture_dir / "baseline.json"
    reg = FixtureRegistry.scan([path])
    assert reg.get(BASELINE_ID).name == "research-agent"
    fx = load_fixture(path).model_copy(update={"name": "renamed"})
    path.write_text(dump_fixture(fx), encoding="utf-8")
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    assert reg.get(BASELINE_ID).name == "renamed"
    assert reg.summaries()[0]["name"] == "renamed"
    path.write_text("{}", encoding="utf-8")
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000_000))
    with pytest.raises(FixtureError):
        reg.get(BASELINE_ID)


def test_get_unknown_key() -> None:
    reg = FixtureRegistry.from_fixtures({"x.json": load_fixture(BASELINE)})
    with pytest.raises(KeyError):
        reg.get("nope")
    assert reg.get(BASELINE_ID).fixture_id == BASELINE_ID


def test_sample_fixtures_are_up_to_date() -> None:
    script = Path(__file__).with_name("make_sample_fixtures.py")
    spec = importlib.util.spec_from_file_location("make_sample_fixtures", script)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        del sys.modules[spec.name]
    for candidate, path in ((False, BASELINE), (True, CANDIDATE)):
        built = dump_fixture(mod.build(candidate=candidate))
        assert built == path.read_text(encoding="utf-8"), f"regenerate {path.name}"


def test_sample_fixtures_shape() -> None:
    base = load_fixture(BASELINE)
    cand = load_fixture(CANDIDATE)
    assert 20 <= len(base.events) <= 30
    assert {e.kind for e in base.events} == {"span", "tool", "model", "value"}
    assert any(e.status == "error" for e in base.events)
    states = {e.request.state for e in base.events} | {e.response.state for e in base.events}
    assert {"captured", "hash_only", "not_captured", "redacted"} <= states
    assert any(e.response.truncated for e in base.events)
    b_search = next(e for e in base.events if e.name == "web_search")
    c_search = next(e for e in cand.events if e.name == "web_search")
    assert b_search.request.value["recency_days"] == 30
    assert c_search.request.value["recency_days"] == 365
    assert b_search.call_key != c_search.call_key
