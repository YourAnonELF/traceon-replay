"""Unit tests for static/logic.js (playback math, diff partner choice), run with node."""

from __future__ import annotations

import json
import shutil
import subprocess
from importlib import resources
from pathlib import Path

import pytest

LOGIC = Path(str(resources.files("traceon.studio").joinpath("static", "logic.js")))

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def run_js(body: str) -> object:
    node = shutil.which("node")
    assert node is not None
    script = (
        f"const m = await import({json.dumps(LOGIC.as_uri())});\n"
        f"const out = (() => {{ {body} }})();\n"
        "process.stdout.write(JSON.stringify(out));\n"
    )
    result = subprocess.run(
        [node, "--input-type=module", "-"],
        input=script,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


SUMMARIES = [
    {"id": "o1", "name": "order-status-agent"},
    {"id": "r2", "name": "research-agent (prompt v2)"},
    {"id": "x1", "name": "billing"},
    {"id": "r1", "name": "research-agent"},
    {"id": "o0", "name": "order-status-agent (retry)"},
]


def test_partner_prefers_same_run_name() -> None:
    out = run_js(
        f"const S = {json.dumps(SUMMARIES)}; return [m.pickPartner(S, 'r1'), m.pickPartner(S, 'r2'), m.pickPartner(S, 'o1'), m.pickPartner(S, 'o0')];"
    )
    assert out == ["r2", "r1", "o0", "o1"]


def test_partner_falls_back_to_prefix_then_word_then_newest() -> None:
    summaries = [
        {"id": "a", "name": "zeta"},
        {"id": "b", "name": "research-agent-v3"},
        {"id": "c", "name": "research-tool"},
        {"id": "d", "name": "research-agent"},
        {"id": "e", "name": "lonely"},
    ]
    out = run_js(
        f"const S = {json.dumps(summaries)}; return [m.pickPartner(S, 'd'), m.pickPartner(S, 'c'), m.pickPartner(S, 'e'), m.pickPartner(S, 'missing'), m.pickPartner([S[0]], 'a')];"
    )
    assert out == ["b", "b", "a", "a", None]


def test_default_pair_orders_older_as_baseline() -> None:
    out = run_js(
        f"const S = {json.dumps(SUMMARIES)}; return [m.defaultPair(S, 'r1'), m.defaultPair(S, 'r2'), m.defaultPair(S, null), m.defaultPair(S.slice(0, 1), null)];"
    )
    assert out == [["r1", "r2"], ["r1", "r2"], ["o0", "o1"], [None, None]]


def test_base_name_strips_trailing_parenthetical() -> None:
    out = run_js(
        "return [m.baseName('research-agent (prompt v2)'), m.baseName('A (b) (c)'), m.baseName('x')];"
    )
    assert out == ["research-agent", "a (b)", "x"]


def test_timeline_and_stepping() -> None:
    out = run_js(
        "const tl = m.buildTimeline(['span','tool','model','value'], [0,10,5,30], [100,20,3,0], 100);"
        "return { tl, at: [m.callAt(tl.times, -1), m.callAt(tl.times, 5), m.callAt(tl.times, 29.9), m.callAt(tl.times, 500)],"
        " next: [m.nextCallTime(tl.times, -1), m.nextCallTime(tl.times, 5), m.nextCallTime(tl.times, 30)],"
        " prev: [m.prevCallTime(tl.times, 10), m.prevCallTime(tl.times, 5), m.prevCallTime(tl.times, 99)] };"
    )
    assert out == {
        "tl": {"order": [2, 1, 3], "times": [5, 10, 30], "total": 100},
        "at": [-1, 0, 1, 2],
        "next": [5, 10, None],
        "prev": [5, 0, 30],
    }


def test_progress_and_uniform_mapping() -> None:
    out = run_js(
        "const times = [5, 10, 30];"
        "return { p: [m.progress(10, 20, 5), m.progress(10, 20, 20), m.progress(10, 20, 99), m.progress(10, 0, 10)],"
        " ut: [m.uniformTime(times, 100, 0), m.uniformTime(times, 100, 1.5), m.uniformTime(times, 100, 2.5), m.uniformTime(times, 100, 9)],"
        " uc: [m.uniformCursor(times, 100, 20), m.uniformCursor(times, 100, 65), m.uniformCursor(times, 100, 0), m.uniformCursor(times, 100, 100)] };"
    )
    assert out == {"p": [0, 0.5, 1, 1], "ut": [0, 20, 65, 100], "uc": [1.5, 2.5, 0, 3]}
