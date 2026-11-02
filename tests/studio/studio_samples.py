"""Paths and ids of the sample fixtures shared by the Studio tests."""

from __future__ import annotations

from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"
BASELINE = DATA / "research-agent.baseline.json"
CANDIDATE = DATA / "research-agent.candidate.json"
BASELINE_ID = "fx_7d41c09a2be5"
CANDIDATE_ID = "fx_c3e88a1f6d20"
