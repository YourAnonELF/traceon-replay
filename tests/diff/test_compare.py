from __future__ import annotations

import pytest
from fixture_factory import FixtureBuilder, hash_only, simple_run

from traceon.core.schema import Payload
from traceon.diff import IgnoreRules, compare


def kinds(report):
    return [d.kind for d in report.divergences]


def statuses(report):
    return [p.status for p in report.alignment]


class TestIdentical:
    def test_identical_runs_are_equivalent(self):
        report = compare(simple_run("fx_a"), simple_run("fx_b"))
        assert report.equivalent
        assert report.first_divergence is None
        assert report.divergences == []
        assert statuses(report) == ["same"] * 4
        assert (report.baseline_id, report.candidate_id) == ("fx_a", "fx_b")

    def test_self_comparison(self):
        fx = simple_run()
        assert compare(fx, fx).equivalent

    def test_empty_runs(self):
        a = FixtureBuilder("empty", "fx_a").build()
        b = FixtureBuilder("empty", "fx_b").build()
        report = compare(a, b)
        assert report.equivalent and report.alignment == []

    def test_value_event_responses_are_not_compared(self):
        a = FixtureBuilder("r", "fx_a")
        a.value("uuid4", "aaaa")
        b = FixtureBuilder("r", "fx_b")
        b.value("uuid4", "bbbb")
        assert compare(a.build(), b.build()).equivalent


class TestArgumentMismatch:
    def test_reports_path_expected_actual(self):
        report = compare(simple_run(query="weather in paris"), simple_run(query="weather in Paris"))
        assert not report.equivalent
        first = report.first_divergence
        assert first is not None
        assert first.kind == "argument_mismatch"
        assert first.label == "tool:search"
        assert first.position == 2
        assert first.path == "$.query"
        assert first.expected == "weather in paris"
        assert first.actual == "weather in Paris"
        assert first.message == (
            "Call #2 tool:search diverged at $.query: "
            'expected "weather in paris", got "weather in Paris"'
        )
        assert statuses(report) == ["same", "same", "changed", "same"]

    def test_nested_paths(self):
        a = FixtureBuilder("r", "fx_a")
        a.tool("t", {"items": [{"id": 1}, {"id": 2}]}, None)
        b = FixtureBuilder("r", "fx_b")
        b.tool("t", {"items": [{"id": 1}, {"id": 3}]}, None)
        div = compare(a.build(), b.build()).first_divergence
        assert div is not None and div.path == "$.items[1].id"
        assert (div.expected, div.actual) == (2, 3)

    def test_hash_only_requests_fall_back_to_call_key(self):
        a = FixtureBuilder("r", "fx_a")
        a.model("llm", {"prompt": "v1"}, {"text": "x"}, request_payload=hash_only({"prompt": "v1"}))
        b = FixtureBuilder("r", "fx_b")
        b.model("llm", {"prompt": "v2"}, {"text": "x"}, request_payload=hash_only({"prompt": "v2"}))
        div = compare(a.build(), b.build()).first_divergence
        assert div is not None and div.kind == "argument_mismatch"
        assert div.path == "$"
        assert "call_key" in div.message and "cannot be shown" in div.message
        assert str(div.expected).startswith("sha256:")

    def test_hash_only_identical_requests_match(self):
        a = FixtureBuilder("r", "fx_a")
        a.model("llm", {"prompt": "v1"}, {"text": "x"}, request_payload=hash_only({"prompt": "v1"}))
        b = FixtureBuilder("r", "fx_b")
        b.model("llm", {"prompt": "v1"}, {"text": "x"}, request_payload=hash_only({"prompt": "v1"}))
        assert compare(a.build(), b.build()).equivalent

    def test_not_captured_requests_without_keys_are_skipped(self):
        a = FixtureBuilder("r", "fx_a")
        a.tool("t", {"q": 1}, "x", request_payload=Payload.not_captured())
        b = FixtureBuilder("r", "fx_b")
        b.tool("t", {"q": 2}, "x", request_payload=Payload.not_captured())
        fa, fb = a.build(), b.build()
        fa.events[1].call_key = None
        fb.events[1].call_key = None
        assert compare(fa, fb).equivalent


class TestResponseAndOutput:
    def test_tool_response_mismatch(self):
        a = FixtureBuilder("r", "fx_a")
        a.tool("search", {"q": "x"}, {"hits": 3})
        b = FixtureBuilder("r", "fx_b")
        b.tool("search", {"q": "x"}, {"hits": 4})
        div = compare(a.build(), b.build()).first_divergence
        assert div is not None and div.kind == "output_mismatch"
        assert div.label == "tool:search" and div.path == "$.hits"
        assert div.message == "Call #0 tool:search response diverged at $.hits: expected 3, got 4"

    def test_run_output_mismatch_comes_last(self):
        report = compare(simple_run(answer="sunny"), simple_run(answer="rainy"))
        assert kinds(report) == ["output_mismatch", "output_mismatch"]
        assert report.divergences[0].label == "model:writer"
        run_level = report.divergences[-1]
        assert run_level.position is None and run_level.path == "$.answer"
        assert run_level.label == "run:output"
        assert run_level.message == 'Run output diverged at $.answer: expected "sunny", got "rainy"'

    def test_compare_output_false(self):
        a = FixtureBuilder("r", "fx_a").build(output="a")
        b = FixtureBuilder("r", "fx_b").build(output="b")
        assert not compare(a, b).equivalent
        assert compare(a, b, compare_output=False).equivalent

    def test_output_not_captured_on_one_side_is_not_compared(self):
        a = FixtureBuilder("r", "fx_a").build(output="a")
        b = FixtureBuilder("r", "fx_b").build()
        assert compare(a, b).equivalent

    def test_hash_only_outputs(self):
        a = FixtureBuilder("r", "fx_a").build(output_payload=hash_only("a"))
        b = FixtureBuilder("r", "fx_b").build(output_payload=hash_only("b"))
        div = compare(a, b).first_divergence
        assert div is not None and div.kind == "output_mismatch"
        assert "hash-only capture" in div.message and div.path == "$"

    def test_truncated_payloads_compare_hashes(self):
        trunc_a = Payload(state="captured", value="aaa", sha256="sha256:1", truncated=True)
        trunc_b = Payload(state="captured", value="aaa", sha256="sha256:2", truncated=True)
        a = FixtureBuilder("r", "fx_a")
        a.tool("t", {}, None, response_payload=trunc_a)
        b = FixtureBuilder("r", "fx_b")
        b.tool("t", {}, None, response_payload=trunc_b)
        div = compare(a.build(), b.build()).first_divergence
        assert div is not None and "truncated" in div.message


class TestStatusAndErrors:
    def test_call_status_mismatch(self):
        a = FixtureBuilder("r", "fx_a")
        a.tool("fetch", {"u": 1}, "ok")
        b = FixtureBuilder("r", "fx_b")
        b.tool("fetch", {"u": 1}, status="error", error=("builtins.TimeoutError", "slow"))
        report = compare(a.build(), b.build())
        assert kinds(report) == ["status_mismatch"]
        div = report.divergences[0]
        assert (div.expected, div.actual) == ("ok", "error")
        assert div.message == (
            "Call #0 tool:fetch diverged: status 'ok' in the baseline, "
            "'error' in the candidate (builtins.TimeoutError: slow)"
        )

    def test_error_type_mismatch(self):
        a = FixtureBuilder("r", "fx_a")
        a.tool("fetch", {}, status="error", error=("builtins.TimeoutError", "slow"))
        b = FixtureBuilder("r", "fx_b")
        b.tool("fetch", {}, status="error", error=("builtins.ValueError", "bad"))
        div = compare(a.build(), b.build()).first_divergence
        assert div is not None and div.kind == "error_mismatch" and div.path == "$.type"
        assert (div.expected, div.actual) == (
            "builtins.TimeoutError: slow",
            "builtins.ValueError: bad",
        )
        assert div.message.startswith("Call #0 tool:fetch failed differently")

    def test_error_message_mismatch(self):
        a = FixtureBuilder("r", "fx_a")
        a.tool("fetch", {}, status="error", error=("builtins.ValueError", "one"))
        b = FixtureBuilder("r", "fx_b")
        b.tool("fetch", {}, status="error", error=("builtins.ValueError", "two"))
        div = compare(a.build(), b.build()).first_divergence
        assert div is not None and div.kind == "error_mismatch" and div.path == "$.message"

    def test_run_status_mismatch(self):
        a = FixtureBuilder("r", "fx_a").build()
        b = FixtureBuilder("r", "fx_b").build(status="error", error=("builtins.RuntimeError", "x"))
        report = compare(a, b)
        assert kinds(report) == ["status_mismatch"]
        assert report.divergences[0].position is None

    def test_run_error_mismatch(self):
        a = FixtureBuilder("r", "fx_a").build(status="error", error=("builtins.KeyError", "a"))
        b = FixtureBuilder("r", "fx_b").build(status="error", error=("builtins.KeyError", "b"))
        assert kinds(compare(a, b)) == ["error_mismatch"]


class TestAlignment:
    def test_inserted_call(self):
        report = compare(simple_run(), simple_run("fx_c", extra_call=True))
        assert statuses(report) == ["same", "same", "same", "added", "same"]
        assert kinds(report) == ["unexpected_call"]
        div = report.divergences[0]
        assert div.label == "tool:fetch" and div.position == 3
        assert div.baseline_event_id is None and div.candidate_event_id is not None

    def test_removed_call(self):
        report = compare(simple_run(extra_call=True), simple_run("fx_c"))
        assert statuses(report) == ["same", "same", "same", "removed", "same"]
        assert kinds(report) == ["missing_call"]
        assert report.divergences[0].message == (
            "Call #3 tool:fetch is in the baseline but was never made by the candidate"
        )

    def test_renamed_call_is_name_mismatch(self):
        a = FixtureBuilder("r", "fx_a")
        a.tool("search", {"q": 1}, "x")
        a.tool("save", {}, "ok")
        b = FixtureBuilder("r", "fx_b")
        b.tool("web_search", {"q": 1}, "x")
        b.tool("save", {}, "ok")
        report = compare(a.build(), b.build())
        assert statuses(report) == ["removed", "added", "same"]
        assert kinds(report) == ["name_mismatch"]
        div = report.divergences[0]
        assert (div.expected, div.actual) == ("search", "web_search")
        assert div.message == "Call #0 diverged: expected tool:search, got tool:web_search"
        assert div.position == 0

    def test_kind_mismatch(self):
        a = FixtureBuilder("r", "fx_a")
        a.tool("summarize", {}, "x")
        b = FixtureBuilder("r", "fx_b")
        b.model("summarize", {}, "x")
        assert kinds(compare(a.build(), b.build())) == ["kind_mismatch"]

    def test_lcs_keeps_common_calls_aligned(self):
        a = FixtureBuilder("r", "fx_a")
        for n in ["a", "b", "c", "d", "e"]:
            a.tool(n, {}, n)
        b = FixtureBuilder("r", "fx_b")
        for n in ["a", "x", "c", "d", "y", "e"]:
            b.tool(n, {}, n)
        report = compare(a.build(), b.build())
        rows = [(p.status, p.name) for p in report.alignment]
        assert rows == [
            ("same", "a"),
            ("removed", "b"),
            ("added", "x"),
            ("same", "c"),
            ("same", "d"),
            ("added", "y"),
            ("same", "e"),
        ]
        assert kinds(report) == ["name_mismatch", "unexpected_call"]

    def test_repeated_calls_removed_in_middle(self):
        a = FixtureBuilder("r", "fx_a")
        for i in range(4):
            a.tool("fetch", {"i": i}, i)
        b = FixtureBuilder("r", "fx_b")
        for i in (0, 1, 3):
            b.tool("fetch", {"i": i}, i)
        report = compare(a.build(), b.build())
        # The LCS by identity aligns the first three; the third differs in arguments,
        # and one recorded call is missing at the end.
        assert statuses(report) == ["same", "same", "changed", "removed"]
        assert kinds(report) == ["argument_mismatch", "output_mismatch", "missing_call"]

    def test_first_divergence_is_earliest(self):
        a = FixtureBuilder("r", "fx_a")
        a.tool("one", {"v": 1}, 1)
        a.tool("two", {"v": 1}, 1)
        b = FixtureBuilder("r", "fx_b")
        b.tool("one", {"v": 2}, 1)
        b.tool("three", {"v": 1}, 1)
        report = compare(a.build(), b.build(output="x"))
        assert report.first_divergence == report.divergences[0]
        assert report.first_divergence is not None
        assert report.first_divergence.label == "tool:one"

    def test_spans_are_not_aligned(self):
        a = FixtureBuilder("r", "fx_a")
        a.span("phase-1")
        a.tool("t", {}, 1)
        b = FixtureBuilder("r", "fx_b")
        b.span("renamed-phase")
        b.tool("t", {}, 1)
        report = compare(a.build(), b.build())
        assert report.equivalent and len(report.alignment) == 1

    def test_large_runs_are_fast(self):
        a = FixtureBuilder("r", "fx_a")
        b = FixtureBuilder("r", "fx_b")
        for i in range(1500):
            a.tool(f"t{i % 7}", {"i": i}, i)
            if i != 700:
                b.tool(f"t{i % 7}", {"i": i}, i)
        report = compare(a.build(), b.build())
        assert kinds(report) == ["missing_call"]
        assert report.divergences[0].position == 700


class TestIgnorePaths:
    @pytest.mark.parametrize(
        ("pattern", "path", "expected"),
        [
            ("$.timestamp", "$.timestamp", True),
            ("timestamp", "$.timestamp", True),
            ("$.meta", "$.meta.request_id", True),
            ("$.meta", "$.metadata", False),
            ("$.items[*].id", "$.items[3].id", True),
            ("$.items[*].id", "$.items[3].name", False),
            ("$..request_id", "$.a[2].b.request_id", True),
            ("$..request_id", "$.request_id", True),
            ("$.*.id", "$.user.id", True),
            ("$.*.id", "$.user.name", False),
            ('$["content-type"]', '$["content-type"]', True),
        ],
    )
    def test_pattern_matching(self, pattern, path, expected):
        assert IgnoreRules((pattern,)).matches(path) is expected

    def test_ignored_argument_difference(self):
        a = FixtureBuilder("r", "fx_a")
        a.tool("search", {"q": "x", "ts": 1}, None)
        b = FixtureBuilder("r", "fx_b")
        b.tool("search", {"q": "x", "ts": 2}, None)
        assert not compare(a.build(), b.build()).equivalent
        assert compare(a.build(), b.build(), ignore_paths=["$.ts"]).equivalent

    def test_ignore_reports_next_difference(self):
        a = FixtureBuilder("r", "fx_a")
        a.tool("search", {"a": 1, "q": "x"}, None)
        b = FixtureBuilder("r", "fx_b")
        b.tool("search", {"a": 2, "q": "y"}, None)
        div = compare(a.build(), b.build(), ignore_paths=["$.a"]).first_divergence
        assert div is not None and div.path == "$.q"

    def test_ignore_applies_to_output(self):
        a = FixtureBuilder("r", "fx_a").build(output={"id": "1", "text": "hi"})
        b = FixtureBuilder("r", "fx_b").build(output={"id": "2", "text": "hi"})
        assert compare(a, b, ignore_paths=["$..id"]).equivalent
