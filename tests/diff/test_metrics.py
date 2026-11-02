from __future__ import annotations

from fixture_factory import FixtureBuilder, simple_run

from traceon.diff import metrics


def test_counts_tokens_and_costs_kept_separate():
    m = metrics(simple_run())
    assert m.status == "ok"
    assert m.events == 5
    assert (m.tool_calls, m.model_calls, m.errors) == (1, 2, 0)
    assert (m.input_tokens, m.output_tokens) == (400, 70)
    assert m.cost_measured == 0.002
    assert m.cost_estimated == 0.004
    assert m.currency == "USD"


def test_duration_from_root_span():
    b = FixtureBuilder("r", "fx")
    b.tool("t", {}, 1)
    assert metrics(b.build(root_duration_ms=1234.5)).duration_ms == 1234.5


def test_duration_from_event_bounds_without_root_span():
    b = FixtureBuilder("r", "fx", root=False, step_ms=100)
    b.tool("a", {}, 1, duration_ms=50)
    b.tool("b", {}, 1, duration_ms=250)
    # a starts at 0, b starts at 100 and ends at 350.
    assert metrics(b.build()).duration_ms == 350


def test_no_usage_means_none_not_zero():
    b = FixtureBuilder("r", "fx")
    b.tool("t", {}, 1)
    m = metrics(b.build())
    assert m.input_tokens is None and m.output_tokens is None
    assert m.cost_measured is None and m.cost_estimated is None and m.currency is None


def test_empty_fixture():
    m = metrics(FixtureBuilder("r", "fx", root=False).build())
    assert m.events == 0 and m.duration_ms is None


def test_errors_are_counted():
    b = FixtureBuilder("r", "fx")
    b.tool("t", {}, status="error", error=("builtins.ValueError", "x"))
    b.tool("t", {}, 2)
    assert metrics(b.build(status="error")).errors == 2  # call + root span


def test_mixed_currencies_are_not_summed():
    b = FixtureBuilder("r", "fx")
    b.model("a", {}, "x", tokens=(1, 1), cost=1.0, currency="USD")
    b.model("b", {}, "x", tokens=(1, 1), cost=2.0, currency="EUR")
    m = metrics(b.build())
    assert m.currency == "mixed"
    assert m.cost_measured is None and m.cost_estimated is None
    assert m.input_tokens == 2


def test_partial_token_reporting():
    b = FixtureBuilder("r", "fx")
    b.model("a", {}, "x", tokens=(10, 5))
    b.model("b", {}, "x")
    m = metrics(b.build())
    assert (m.input_tokens, m.output_tokens) == (10, 5)
