"""Render diff reports and divergences for terminals, CI logs and pull requests.

Three output families share one vocabulary (call labels, JSONPaths, expected /
actual values):

* rich renderables (:func:`diff_renderable`, :func:`divergence_renderable`,
  :func:`metrics_renderable`, :func:`alignment_renderable`) for the CLI;
* plain or colored text (:func:`render_text`, :func:`render_divergence_text`)
  for logs and pytest failure output;
* GitHub-flavored Markdown (:func:`render_markdown`,
  :func:`divergence_markdown`) for ``$GITHUB_STEP_SUMMARY`` and PR comments.

Fixture content is untrusted: it is always rendered as literal text, never as
rich markup or raw HTML.
"""

from __future__ import annotations

import io
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, TextIO

from rich.console import Console, Group, RenderableType
from rich.table import Table
from rich.text import Text

from traceon.core.schema import AlignedPair, DiffReport, Divergence, RunMetrics

__all__ = [
    "ASCII",
    "STYLES",
    "UNICODE",
    "Glyphs",
    "alignment_renderable",
    "diff_renderable",
    "divergence_location",
    "divergence_markdown",
    "divergence_renderable",
    "format_cost",
    "format_duration",
    "format_tokens",
    "format_value",
    "metrics_renderable",
    "render_divergence_text",
    "render_markdown",
    "render_text",
    "supports_unicode",
]


# ---------------------------------------------------------------------------
# Glyphs and styles
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Glyphs:
    """Symbols used in terminal output; :data:`ASCII` is the fallback set."""

    ok: str
    fail: str
    changed: str
    added: str
    removed: str
    arrow: str
    ellipsis: str
    dot: str
    tee: str
    corner: str
    pipe: str
    blank: str


UNICODE = Glyphs(
    ok="✓",
    fail="✗",
    changed="●",
    added="+",
    removed="-",
    arrow="→",
    ellipsis="…",
    dot="·",
    tee="├─ ",
    corner="└─ ",
    pipe="│  ",
    blank="   ",
)
ASCII = Glyphs(
    ok="v",
    fail="x",
    changed="*",
    added="+",
    removed="-",
    arrow="->",
    ellipsis="...",
    dot="-",
    tee="|- ",
    corner="`- ",
    pipe="|  ",
    blank="   ",
)

#: Muted semantic styles. Only these are used, so output stays calm.
STYLES: dict[str, str] = {
    "ok": "green",
    "fail": "red",
    "changed": "yellow",
    "added": "green",
    "removed": "red",
    "muted": "bright_black",
    "path": "cyan",
    "expected": "red",
    "actual": "green",
    "heading": "bold",
    "label": "bold",
}


def supports_unicode(stream: TextIO | None = None) -> bool:
    """Whether ``stream`` (default: stdout) can encode the :data:`UNICODE` glyphs."""
    stream = stream if stream is not None else sys.stdout
    encoding = getattr(stream, "encoding", None) or "ascii"
    sample = "".join(
        (
            UNICODE.ok,
            UNICODE.fail,
            UNICODE.changed,
            UNICODE.arrow,
            UNICODE.ellipsis,
            UNICODE.dot,
            UNICODE.tee,
            UNICODE.corner,
            UNICODE.pipe,
        )
    )
    try:
        sample.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def format_duration(ms: float | None) -> str:
    """``0.4ms``, ``12ms``, ``1.24s``, ``2m 05s``; ``-`` when unknown."""
    if ms is None:
        return "-"
    if ms < 1:
        return f"{ms:.1f}ms"
    if ms < 1000:
        return f"{ms:.0f}ms"
    seconds = ms / 1000.0
    if seconds < 60:
        return f"{seconds:.2f}s"
    minutes, rest = divmod(round(seconds), 60)
    return f"{minutes}m {rest:02d}s"


def format_tokens(n: int | None) -> str:
    """Thousands-separated token count; ``-`` when unknown."""
    return "-" if n is None else f"{n:,}"


def format_cost(amount: float | None, currency: str | None = "USD") -> str:
    """``$0.0123`` for USD, ``0.0123 EUR`` otherwise; ``-`` when unknown."""
    if amount is None:
        return "-"
    number = f"{amount:,.4f}" if abs(amount) < 100 else f"{amount:,.2f}"
    if currency in (None, "USD"):
        return f"-${number[1:]}" if number.startswith("-") else f"${number}"
    return f"{number} {currency}"


def format_value(value: Any, *, max_len: int = 80, ellipsis: str = "…") -> str:
    """Compact JSON for one value, cut to ``max_len`` characters."""
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(", ", ": "))
    except (TypeError, ValueError):
        text = repr(value)
    if len(text) > max_len:
        text = text[: max(1, max_len - len(ellipsis))] + ellipsis
    return text


def _position_text(div: Divergence) -> str:
    """``call #3`` for call-level divergences, ``run`` for run-level ones."""
    return "run" if div.position is None else f"call #{div.position}"


def _call_label(div: Divergence) -> str | None:
    """The call label, or ``None`` for run-level labels such as ``run:output``."""
    if div.label is None or (div.position is None and div.label.startswith("run:")):
        return None
    return div.label


def divergence_location(div: Divergence) -> str:
    """``call #3 tool:search`` or ``run``."""
    lbl = _call_label(div)
    return _position_text(div) + (f" {lbl}" if lbl else "")


def _has_values(div: Divergence) -> bool:
    return div.path is not None or div.expected is not None or div.actual is not None


# ---------------------------------------------------------------------------
# Rich renderables
# ---------------------------------------------------------------------------


def _message_is_redundant(div: Divergence) -> bool:
    """True when the message only restates the path / expected / actual grid.

    Messages of the form ``Call #3 tool:search diverged at $.query: expected "a",
    got "b"`` add nothing to the grid; messages carrying hints (extra sentences)
    are kept.
    """
    if div.path is None:
        return False
    at = div.message.find(f"diverged at {div.path}: ")
    return at >= 0 and ". " not in div.message[at:]


def divergence_renderable(
    div: Divergence,
    *,
    glyphs: Glyphs = UNICODE,
    title: str = "first divergence",
    value_width: int = 72,
) -> RenderableType:
    """Compact block: kind, position and label, message, then path/expected/actual."""
    head = Text.assemble(
        (f"{glyphs.fail} ", STYLES["fail"]),
        (title, STYLES["heading"]),
        "  ",
        (div.kind, STYLES["fail"]),
        (f" {glyphs.dot} ", STYLES["muted"]),
        _position_text(div),
    )
    if _call_label(div):
        head.append(f" {glyphs.dot} ", style=STYLES["muted"])
        head.append(_call_label(div) or "", style=STYLES["label"])
    parts: list[RenderableType] = [head]
    if not _message_is_redundant(div):
        parts.append(Text("  " + div.message))
    if _has_values(div):
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style=STYLES["muted"], no_wrap=True)
        grid.add_column(overflow="fold")
        if div.path is not None:
            grid.add_row("path", Text(div.path, style=STYLES["path"]))
        if div.expected is not None or div.kind != "unexpected_call":
            expected = format_value(div.expected, max_len=value_width, ellipsis=glyphs.ellipsis)
            grid.add_row("expected", Text(expected, style=STYLES["expected"]))
        if div.actual is not None or div.kind != "missing_call":
            actual = format_value(div.actual, max_len=value_width, ellipsis=glyphs.ellipsis)
            grid.add_row("actual", Text(actual, style=STYLES["actual"]))
        indented = Table.grid()
        indented.add_column(width=4)
        indented.add_column()
        indented.add_row("", grid)
        parts.append(indented)
    return Group(*parts)


def _delta(base: float | None, cand: float | None, fmt: str) -> Text:
    if base is None or cand is None or base == cand:
        return Text("")
    diff = cand - base
    if fmt == "duration":
        body = format_duration(abs(diff))
    elif fmt == "tokens":
        body = f"{abs(int(diff)):,}"
    else:
        body = format_cost(abs(diff), None).lstrip("$")
    pct = f" ({diff / base * 100:+.0f}%)" if base else ""
    sign = "+" if diff > 0 else "-"
    return Text(f"{sign}{body}{pct}", style=STYLES["muted"])


def _status_text(status: str, glyphs: Glyphs) -> Text:
    if status == "ok":
        return Text(f"{glyphs.ok} ok", style=STYLES["ok"])
    return Text(f"{glyphs.fail} {status}", style=STYLES["fail"])


def metrics_renderable(
    baseline: RunMetrics, candidate: RunMetrics, *, glyphs: Glyphs = UNICODE
) -> RenderableType:
    """Side-by-side baseline / candidate metrics with a delta column."""
    table = Table(
        box=None, pad_edge=False, padding=(0, 2), show_edge=False, header_style=STYLES["muted"]
    )
    table.add_column("", style=STYLES["muted"], no_wrap=True)
    table.add_column("baseline", justify="right", no_wrap=True)
    table.add_column("candidate", justify="right", no_wrap=True)
    table.add_column("delta", justify="right", no_wrap=True)
    table.add_row(
        "status", _status_text(baseline.status, glyphs), _status_text(candidate.status, glyphs), ""
    )
    table.add_row(
        "duration",
        format_duration(baseline.duration_ms),
        format_duration(candidate.duration_ms),
        _delta(baseline.duration_ms, candidate.duration_ms, "duration"),
    )
    table.add_row(
        "tool calls",
        str(baseline.tool_calls),
        str(candidate.tool_calls),
        _delta(baseline.tool_calls, candidate.tool_calls, "tokens"),
    )
    table.add_row(
        "model calls",
        str(baseline.model_calls),
        str(candidate.model_calls),
        _delta(baseline.model_calls, candidate.model_calls, "tokens"),
    )
    table.add_row(
        "errors",
        str(baseline.errors),
        str(candidate.errors),
        _delta(baseline.errors, candidate.errors, "tokens"),
    )
    if baseline.input_tokens is not None or candidate.input_tokens is not None:
        table.add_row(
            "input tokens",
            format_tokens(baseline.input_tokens),
            format_tokens(candidate.input_tokens),
            _delta(baseline.input_tokens, candidate.input_tokens, "tokens"),
        )
    if baseline.output_tokens is not None or candidate.output_tokens is not None:
        table.add_row(
            "output tokens",
            format_tokens(baseline.output_tokens),
            format_tokens(candidate.output_tokens),
            _delta(baseline.output_tokens, candidate.output_tokens, "tokens"),
        )
    if baseline.cost_measured is not None or candidate.cost_measured is not None:
        table.add_row(
            "cost (measured)",
            format_cost(baseline.cost_measured, baseline.currency),
            format_cost(candidate.cost_measured, candidate.currency),
            _delta(baseline.cost_measured, candidate.cost_measured, "cost"),
        )
    if baseline.cost_estimated is not None or candidate.cost_estimated is not None:
        table.add_row(
            "cost (estimated)",
            format_cost(baseline.cost_estimated, baseline.currency),
            format_cost(candidate.cost_estimated, candidate.currency),
            _delta(baseline.cost_estimated, candidate.cost_estimated, "cost"),
        )
    return table


@dataclass(frozen=True)
class _AlignedRow:
    pair: AlignedPair
    b_index: int | None
    c_index: int | None


def _indexed_rows(alignment: Sequence[AlignedPair]) -> list[_AlignedRow]:
    rows: list[_AlignedRow] = []
    bi = ci = 0
    for pair in alignment:
        b = c = None
        if pair.status in ("same", "changed"):
            b, c = bi, ci
            bi, ci = bi + 1, ci + 1
        elif pair.status == "removed":
            b, bi = bi, bi + 1
        else:
            c, ci = ci, ci + 1
        rows.append(_AlignedRow(pair, b, c))
    return rows


class _RowNotes:
    """Divergences indexed by the event ids of the aligned row they belong to."""

    def __init__(self, report: DiffReport) -> None:
        self._by_base: dict[str, list[Divergence]] = {}
        self._by_cand: dict[str, list[Divergence]] = {}
        for div in report.divergences:
            if div.position is None:
                continue
            if div.baseline_event_id is not None:
                self._by_base.setdefault(div.baseline_event_id, []).append(div)
            if div.candidate_event_id is not None:
                self._by_cand.setdefault(div.candidate_event_id, []).append(div)

    def for_pair(self, pair: AlignedPair) -> list[Divergence]:
        found: list[Divergence] = []
        if pair.baseline_event_id is not None:
            found.extend(self._by_base.get(pair.baseline_event_id, []))
        if pair.candidate_event_id is not None:
            found.extend(
                d
                for d in self._by_cand.get(pair.candidate_event_id, [])
                if not any(d is f for f in found)
            )
        return found


def _collapse(rows: list[_AlignedRow], context: int) -> list[_AlignedRow | int]:
    """Replace long runs of unchanged rows with a count, keeping ``context`` rows."""
    out: list[_AlignedRow | int] = []
    run: list[_AlignedRow] = []

    def flush(at_start: bool, at_end: bool) -> None:
        head = 0 if at_start else context
        tail = 0 if at_end else context
        if len(run) > head + tail + 1:
            out.extend(run[:head])
            out.append(len(run) - head - tail)
            out.extend(run[len(run) - tail :] if tail else [])
        else:
            out.extend(run)
        run.clear()

    started = False
    for row in rows:
        if row.pair.status == "same":
            run.append(row)
            continue
        flush(at_start=not started, at_end=False)
        started = True
        out.append(row)
    flush(at_start=not started, at_end=True)
    if not started:
        # No changes at all: show everything rather than a single counter.
        return list(rows)
    return out


def alignment_renderable(
    report: DiffReport, *, glyphs: Glyphs = UNICODE, context: int = 2
) -> RenderableType:
    """Call-by-call alignment table; long unchanged stretches are collapsed."""
    table = Table(
        box=None, pad_edge=False, padding=(0, 2), show_edge=False, header_style=STYLES["muted"]
    )
    table.add_column("", no_wrap=True, width=1)
    table.add_column("base", justify="right", no_wrap=True, style=STYLES["muted"])
    table.add_column("cand", justify="right", no_wrap=True, style=STYLES["muted"])
    table.add_column("call", no_wrap=True)
    table.add_column("", overflow="fold")
    notes = _RowNotes(report)
    glyph = {
        "same": (glyphs.ok, STYLES["ok"]),
        "changed": (glyphs.changed, STYLES["changed"]),
        "added": (glyphs.added, STYLES["added"]),
        "removed": (glyphs.removed, STYLES["removed"]),
    }
    for item in _collapse(_indexed_rows(report.alignment), context):
        if isinstance(item, int):
            table.add_row(
                "",
                "",
                "",
                Text(f"{glyphs.ellipsis} {item} unchanged calls", style=STYLES["muted"]),
                "",
            )
            continue
        pair = item.pair
        sym, style = glyph[pair.status]
        call_label = Text(f"{pair.kind}:{pair.name}", style="" if pair.status == "same" else style)
        divs = notes.for_pair(pair)
        note = Text(style=STYLES["muted"])
        for k, div in enumerate(divs):
            if k:
                note.append(", ")
            note.append(div.kind)
            if div.path:
                note.append(" ")
                note.append(div.path, style=STYLES["path"])
        table.add_row(
            Text(sym, style=style),
            "" if item.b_index is None else str(item.b_index),
            "" if item.c_index is None else str(item.c_index),
            call_label,
            note,
        )
    return table


def _summary_line(report: DiffReport, glyphs: Glyphs) -> Text:
    n_calls = sum(1 for p in report.alignment if p.status in ("same", "changed"))
    if report.equivalent:
        return Text.assemble(
            (f"{glyphs.ok} equivalent", STYLES["ok"]),
            (f"  {n_calls} calls aligned, no divergences", STYLES["muted"]),
        )
    n = len(report.divergences)
    first = report.first_divergence
    text = Text.assemble(
        (f"{glyphs.fail} not equivalent", STYLES["fail"]),
        f"  {n} divergence{'s' if n != 1 else ''}",
    )
    if first is not None:
        text.append(f" {glyphs.dot} first at {divergence_location(first)}", style=STYLES["muted"])
    return text


def diff_renderable(
    report: DiffReport,
    *,
    glyphs: Glyphs = UNICODE,
    baseline_label: str | None = None,
    candidate_label: str | None = None,
    max_divergences: int = 10,
) -> RenderableType:
    """Full diff view: header, metrics, alignment, first divergence, summary."""
    b_label = baseline_label or report.baseline_id
    c_label = candidate_label or report.candidate_id
    header = Text.assemble(
        ("baseline   ", STYLES["muted"]),
        (b_label, STYLES["label"]),
        ("  ", ""),
        (report.baseline_name, STYLES["muted"]),
    )
    header2 = Text.assemble(
        ("candidate  ", STYLES["muted"]),
        (c_label, STYLES["label"]),
        ("  ", ""),
        (report.candidate_name, STYLES["muted"]),
    )
    parts: list[RenderableType] = [
        header,
        header2,
        Text(""),
        metrics_renderable(report.baseline_metrics, report.candidate_metrics, glyphs=glyphs),
    ]
    if report.alignment:
        parts += [Text(""), alignment_renderable(report, glyphs=glyphs)]
    if report.first_divergence is not None:
        parts += [Text(""), divergence_renderable(report.first_divergence, glyphs=glyphs)]
        rest = report.divergences[1:]
        if rest:
            parts.append(Text(""))
            parts.append(Text(f"  other divergences ({len(rest)})", style=STYLES["muted"]))
            listing = Table.grid(padding=(0, 2))
            listing.add_column(width=2)
            listing.add_column(no_wrap=True, style=STYLES["muted"])
            listing.add_column(no_wrap=True)
            listing.add_column(overflow="fold")
            for div in rest[:max_divergences]:
                where = divergence_location(div)
                listing.add_row("", where, Text(div.kind, style=STYLES["fail"]), Text(div.message))
            if len(rest) > max_divergences:
                listing.add_row("", f"{glyphs.ellipsis} {len(rest) - max_divergences} more", "", "")
            parts.append(listing)
    parts += [Text(""), _summary_line(report, glyphs)]
    return Group(*parts)


# ---------------------------------------------------------------------------
# Text output
# ---------------------------------------------------------------------------


def _string_console(color: bool, width: int) -> Console:
    return Console(
        file=io.StringIO(),
        width=width,
        color_system="standard" if color else None,
        force_terminal=color,
        no_color=not color,
        highlight=False,
        emoji=False,
        markup=False,
        legacy_windows=False,
        soft_wrap=False,
    )


def render_renderable(renderable: RenderableType, *, color: bool = False, width: int = 100) -> str:
    """Render any rich renderable to a string (ANSI-colored when ``color``)."""
    console = _string_console(color, width)
    console.print(renderable)
    out = console.file.getvalue()  # type: ignore[attr-defined]
    assert isinstance(out, str)
    return "\n".join(line.rstrip() for line in out.splitlines()) + "\n"


def render_text(
    report: DiffReport,
    *,
    color: bool,
    width: int = 100,
    unicode: bool = True,
    baseline_label: str | None = None,
    candidate_label: str | None = None,
) -> str:
    """Render a diff report as terminal text. With ``color=False`` it is plain text."""
    return render_renderable(
        diff_renderable(
            report,
            glyphs=UNICODE if unicode else ASCII,
            baseline_label=baseline_label,
            candidate_label=candidate_label,
        ),
        color=color,
        width=width,
    )


def render_divergence_text(
    div: Divergence,
    *,
    color: bool = False,
    width: int = 100,
    unicode: bool = True,
    title: str = "first divergence",
) -> str:
    """Render one divergence block (used by pytest failure output)."""
    return render_renderable(
        divergence_renderable(div, glyphs=UNICODE if unicode else ASCII, title=title),
        color=color,
        width=width,
    )


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------


def _md_cell(text: str) -> str:
    """Escape text for a GitHub table cell (pipes, newlines, HTML)."""
    return (
        text.replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("\n", " ")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _md_code(text: str) -> str:
    """Inline code span that survives backticks inside ``text``."""
    text = text.replace("\n", " ")
    fence = "`"
    while fence in text:
        fence += "`"
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def _md_block(text: str, lang: str = "") -> str:
    fence = "```"
    while fence in text:
        fence += "`"
    return f"{fence}{lang}\n{text}\n{fence}"


def divergence_markdown(div: Divergence, *, value_width: int = 200) -> str:
    """Markdown block for one divergence: heading line, message, values as a diff."""
    lbl = _call_label(div)
    where = _position_text(div) + (f" {_md_code(lbl)}" if lbl else "")
    lines = [f"**{div.kind}** at {where}", "", _md_cell(div.message)]
    if _has_values(div):
        lines.append("")
        if div.path is not None:
            lines.append(f"Path: {_md_code(div.path)}")
            lines.append("")
        body: list[str] = []
        if div.expected is not None or div.kind != "unexpected_call":
            body.append("- " + format_value(div.expected, max_len=value_width, ellipsis="..."))
        if div.actual is not None or div.kind != "missing_call":
            body.append("+ " + format_value(div.actual, max_len=value_width, ellipsis="..."))
        lines.append(_md_block("\n".join(body), "diff"))
    return "\n".join(lines)


def _md_metrics(b: RunMetrics, c: RunMetrics) -> list[str]:
    rows: list[tuple[str, str, str]] = [
        ("status", b.status, c.status),
        ("duration", format_duration(b.duration_ms), format_duration(c.duration_ms)),
        ("tool calls", str(b.tool_calls), str(c.tool_calls)),
        ("model calls", str(b.model_calls), str(c.model_calls)),
        ("errors", str(b.errors), str(c.errors)),
    ]
    if b.input_tokens is not None or c.input_tokens is not None:
        rows.append(("input tokens", format_tokens(b.input_tokens), format_tokens(c.input_tokens)))
    if b.output_tokens is not None or c.output_tokens is not None:
        rows.append(
            ("output tokens", format_tokens(b.output_tokens), format_tokens(c.output_tokens))
        )
    if b.cost_measured is not None or c.cost_measured is not None:
        rows.append(
            (
                "cost (measured)",
                format_cost(b.cost_measured, b.currency),
                format_cost(c.cost_measured, c.currency),
            )
        )
    if b.cost_estimated is not None or c.cost_estimated is not None:
        rows.append(
            (
                "cost (estimated)",
                format_cost(b.cost_estimated, b.currency),
                format_cost(c.cost_estimated, c.currency),
            )
        )
    out = ["| | baseline | candidate |", "|---|---:|---:|"]
    out += [f"| {name} | {_md_cell(x)} | {_md_cell(y)} |" for name, x, y in rows]
    return out


def _md_alignment(report: DiffReport) -> list[str]:
    symbols = {"same": "", "changed": "changed", "added": "added", "removed": "removed"}
    notes = _RowNotes(report)
    out = ["| base | cand | call | change |", "|---:|---:|---|---|"]
    for row in _indexed_rows(report.alignment):
        pair = row.pair
        kinds = [d.kind for d in notes.for_pair(pair)]
        change = symbols[pair.status] + (f" ({', '.join(kinds)})" if kinds else "")
        out.append(
            f"| {'' if row.b_index is None else row.b_index} "
            f"| {'' if row.c_index is None else row.c_index} "
            f"| {_md_code(f'{pair.kind}:{pair.name}')} | {change} |"
        )
    return out


def render_markdown(
    report: DiffReport,
    *,
    baseline_label: str | None = None,
    candidate_label: str | None = None,
    max_divergences: int = 50,
) -> str:
    """GitHub-flavored Markdown suitable for ``$GITHUB_STEP_SUMMARY`` and PR comments."""
    verdict = (
        "equivalent"
        if report.equivalent
        else (f"{len(report.divergences)} divergence{'s' if len(report.divergences) != 1 else ''}")
    )
    b_label = baseline_label or report.baseline_id
    c_label = candidate_label or report.candidate_id
    lines = [
        f"### Traceon diff: {verdict}",
        "",
        f"Baseline {_md_code(b_label)} ({_md_cell(report.baseline_name)}) "
        f"vs candidate {_md_code(c_label)} ({_md_cell(report.candidate_name)})",
        "",
        *_md_metrics(report.baseline_metrics, report.candidate_metrics),
    ]
    if report.first_divergence is not None:
        lines += ["", "#### First divergence", "", divergence_markdown(report.first_divergence)]
    if len(report.divergences) > 1:
        shown = report.divergences[:max_divergences]
        lines += [
            "",
            f"<details><summary>All divergences ({len(report.divergences)})</summary>",
            "",
            "| where | call | kind | detail |",
            "|---|---|---|---|",
        ]
        for div in shown:
            lines.append(
                f"| {_position_text(div)} | {_md_code(lbl) if (lbl := _call_label(div)) else ''} "
                f"| {div.kind} | {_md_cell(div.message)} |"
            )
        if len(report.divergences) > max_divergences:
            lines.append(f"| | | | {len(report.divergences) - max_divergences} more not shown |")
        lines += ["", "</details>"]
    if report.alignment:
        lines += [
            "",
            f"<details><summary>Call alignment ({len(report.alignment)} rows)</summary>",
            "",
            *_md_alignment(report),
            "",
            "</details>",
        ]
    return "\n".join(lines) + "\n"
