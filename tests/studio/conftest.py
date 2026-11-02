from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from studio_samples import BASELINE, BASELINE_ID, CANDIDATE


@pytest.fixture
def fixture_dir(tmp_path: Path) -> Path:
    """A directory with both sample fixtures plus files Studio must skip."""
    d = tmp_path / "fixtures"
    d.mkdir()
    shutil.copy(BASELINE, d / "baseline.json")
    shutil.copy(CANDIDATE, d / "candidate.json")
    (d / "broken.json").write_text("{not json", encoding="utf-8")
    (d / "other-tool.json").write_text('{"name": "package.json-like"}', encoding="utf-8")
    (d / "notes.txt").write_text("not a fixture", encoding="utf-8")
    nested = d / "nested"
    nested.mkdir()
    text = BASELINE.read_text(encoding="utf-8").replace(BASELINE_ID, "fx_nested000001")
    (nested / "deep.json").write_text(text, encoding="utf-8")
    return d
