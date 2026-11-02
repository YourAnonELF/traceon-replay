from __future__ import annotations

import pytest
from fixture_factory import FixtureBuilder, simple_run

from traceon.core.schema import Divergence
from traceon.diff import compare
from traceon.diff.render import (
    ASCII,
    UNICODE,
    divergence_markdown,
    format_cost,
    format_duration,
    format_tokens,
    format_value,
    render_divergence_text,
    render_markdown,
    render_text,
    supports_unicode,
)


@pytest.fixture
def diverged():
    return compare(
        simple_run("fx_base"), simple_run("fx_cand", query="weather in Paris", extra_call=True)
    )


EXPECTED_TEXT = """\
baseline   fx_base  agent
candidate  fx_cand  agent

                    baseline    candidate           delta
status                  ✓ ok         ✓ ok
duration                50ms         60ms    +10ms (+20%)
tool calls                 1            2      +1 (+100%)
model calls                2            2
errors                     0            0
input tokens             400          400
output tokens             70           70
cost (measured)      $0.0020      $0.0020
cost (estimated)     $0.0040      $0.0040

     base    cand    call
✓       0       0    value:now
✓       1       1    model:planner
●       2       2    tool:search      argument_mismatch $.query
+               3    tool:fetch       unexpected_call
✓       3       4    model:writer

✗ first divergence  argument_mismatch · call #2 · tool:search
    path      $.query
    expected  "weather in paris"
    actual    "weather in Paris"

  other divergences (1)
    call #3 tool:fetch  unexpected_call  Call #3 tool:fetch (candidate) is not in the baseline

✗ not equivalent  2 divergences · first at call #2 tool:search
"""


def test_text_snapshot(diverged):
    assert render_text(diverged, color=False, width=100) == EXPECTED_TEXT


def test_text_has_no_ansi_without_color(diverged):
    assert "\x1b[" not in render_text(diverged, color=False)


def test_text_with_color_uses_ansi(diverged):
    out = render_text(diverged, color=True)
    assert "\x1b[" in out
    assert "\x1b[31m" in out  # expected value / failure in red


def test_ascii_fallback(diverged):
    out = render_text(diverged, color=False, unicode=False)
    assert "x first divergence" in out
    assert all(g not in out for g in ("✓", "✗", "●", "·", "…"))
    out.encode("ascii")


def test_equivalent_summary():
    out = render_text(compare(simple_run("fx_a"), simple_run("fx_b")), color=False)
    assert out.rstrip().endswith("✓ equivalent  4 calls aligned, no divergences")
    assert "first divergence" not in out


def test_custom_labels(diverged):
    out = render_text(
        diverged, color=False, baseline_label="base.json", candidate_label="cand.json"
    )
    assert out.startswith("baseline   base.json  agent\ncandidate  cand.json  agent\n")


def test_long_unchanged_runs_are_collapsed():
    a, b = FixtureBuilder("r", "fx_a"), FixtureBuilder("r", "fx_b")
    for i in range(30):
        a.tool("step", {"i": i}, i)
        b.tool("step", {"i": i if i != 15 else -1}, i)
    out = render_text(compare(a.build(), b.build()), color=False)
    assert "… 13 unchanged calls" in out
    assert "… 12 unchanged calls" in out
    # 5 table rows (2 context + changed + 2 context), block heading, summary
    assert out.count("tool:step") == 7


def test_untrusted_markup_is_literal():
    a, b = FixtureBuilder("r", "fx_a"), FixtureBuilder("r", "fx_b")
    a.tool("t", {"q": "[bold red]x[/]"}, None)
    b.tool("t", {"q": "[link=http://evil]y[/link]"}, None)
    out = render_text(compare(a.build(), b.build()), color=False)
    assert '"[bold red]x[/]"' in out
    assert '"[link=http://evil]y[/link]"' in out


def test_divergence_block_snapshot():
    div = Divergence(
        kind="argument_mismatch",
        position=3,
        label="tool:search",
        path="$.query",
        expected="a",
        actual="b",
        message='Call #3 tool:search diverged at $.query: expected "a", got "b"',
    )
    assert render_divergence_text(div, unicode=False) == (
        "x first divergence  argument_mismatch - call #3 - tool:search\n"
        "    path      $.query\n"
        '    expected  "a"\n'
        '    actual    "b"\n'
    )


def test_divergence_block_missing_call_has_no_actual():
    div = Divergence(
        kind="missing_call",
        position=1,
        label="tool:x",
        expected="tool:x",
        message="call #1 tool:x was not made by the candidate",
    )
    out = render_divergence_text(div)
    assert "expected" in out and "actual" not in out


def test_divergence_block_run_level():
    div = Divergence(
        kind="status_mismatch",
        expected="ok",
        actual="error",
        label="run:status",
        message="Run status diverged: the baseline finished 'ok'",
    )
    out = render_divergence_text(div)
    assert "status_mismatch · run\n" in out
    assert "run:status" not in out
    assert "Run status diverged" in out


EXPECTED_MARKDOWN = """\
### Traceon diff: 2 divergences

Baseline `fx_base` (agent) vs candidate `fx_cand` (agent)

| | baseline | candidate |
|---|---:|---:|
| status | ok | ok |
| duration | 50ms | 60ms |
| tool calls | 1 | 2 |
| model calls | 2 | 2 |
| errors | 0 | 0 |
| input tokens | 400 | 400 |
| output tokens | 70 | 70 |
| cost (measured) | $0.0020 | $0.0020 |
| cost (estimated) | $0.0040 | $0.0040 |

#### First divergence

**argument_mismatch** at call #2 `tool:search`

Call #2 tool:search diverged at $.query: expected "weather in paris", got "weather in Paris"

Path: `$.query`

```diff
- "weather in paris"
+ "weather in Paris"
```

<details><summary>All divergences (2)</summary>

| where | call | kind | detail |
|---|---|---|---|
| call #2 | `tool:search` | argument_mismatch | Call #2 tool:search diverged at $.query: expected "weather in paris", got "weather in Paris" |
| call #3 | `tool:fetch` | unexpected_call | Call #3 tool:fetch (candidate) is not in the baseline |

</details>

<details><summary>Call alignment (5 rows)</summary>

| base | cand | call | change |
|---:|---:|---|---|
| 0 | 0 | `value:now` |  |
| 1 | 1 | `model:planner` |  |
| 2 | 2 | `tool:search` | changed (argument_mismatch) |
|  | 3 | `tool:fetch` | added (unexpected_call) |
| 3 | 4 | `model:writer` |  |

</details>
"""


def test_markdown_snapshot(diverged):
    assert render_markdown(diverged) == EXPECTED_MARKDOWN


def test_markdown_equivalent():
    md = render_markdown(compare(simple_run("fx_a"), simple_run("fx_b")))
    assert md.startswith("### Traceon diff: equivalent\n")
    assert "First divergence" not in md and "<details><summary>All" not in md


def test_markdown_escapes_untrusted_content():
    div = Divergence(
        kind="output_mismatch",
        position=0,
        label="tool:`x`",
        path="$.a",
        expected="a|b",
        actual="</details><script>",
        message="pipe | and <b>html</b>\nnewline",
    )
    md = divergence_markdown(div)
    assert "pipe \\| and &lt;b&gt;html&lt;/b&gt; newline" in md
    assert "`` tool:`x` ``" in md
    # values live inside a fenced block, where HTML is inert
    assert '+ "</details><script>"' in md


def test_markdown_value_with_backtick_fence():
    div = Divergence(
        kind="output_mismatch", position=0, path="$", expected="```", actual="x", message="m"
    )
    md = divergence_markdown(div)
    assert "````diff" in md


@pytest.mark.parametrize(
    ("ms", "text"),
    [(None, "-"), (0.25, "0.2ms"), (12.4, "12ms"), (1240, "1.24s"), (125_000, "2m 05s")],
)
def test_format_duration(ms, text):
    assert format_duration(ms) == text


def test_format_helpers():
    assert format_tokens(None) == "-"
    assert format_tokens(1234567) == "1,234,567"
    assert format_cost(0.01234) == "$0.0123"
    assert format_cost(1234.5) == "$1,234.50"
    assert format_cost(0.5, "EUR") == "0.5000 EUR"
    assert format_cost(None) == "-"
    assert format_value({"b": 1, "a": "x"}) == '{"a": "x", "b": 1}'
    assert format_value("x" * 100, max_len=10) == '"xxxxxxxx…'
    assert format_value("x" * 100, max_len=10, ellipsis="...") == '"xxxxxx...'


def test_supports_unicode():
    class Stream:
        def __init__(self, encoding):
            self.encoding = encoding

    assert supports_unicode(Stream("utf-8"))  # type: ignore[arg-type]
    assert not supports_unicode(Stream("cp1252"))  # type: ignore[arg-type]
    assert not supports_unicode(Stream("ascii"))  # type: ignore[arg-type]
    assert UNICODE.ok != ASCII.ok
