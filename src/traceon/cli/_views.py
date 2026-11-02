"""Terminal views for ``traceon inspect``, ``validate``, ``record`` and ``replay``."""

from __future__ import annotations

import datetime as _dt
from collections.abc import Sequence
from typing import Any

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text

from traceon.core.schema import Divergence, Event, Fixture, RunMetrics
from traceon.diff.engine import metrics
from traceon.diff.render import (
    STYLES,
    Glyphs,
    divergence_location,
    divergence_renderable,
    format_cost,
    format_duration,
    format_tokens,
)

# ---------------------------------------------------------------------------
# Shared pieces
# ---------------------------------------------------------------------------


def status_glyph(status: str, glyphs: Glyphs) -> Text:
    """``✓`` for ok, ``✗`` for error, ``●`` for cancelled."""
    if status == "ok":
        return Text(glyphs.ok, style=STYLES["ok"])
    if status == "error":
        return Text(glyphs.fail, style=STYLES["fail"])
    return Text(glyphs.changed, style=STYLES["changed"])


def _timestamp(value: _dt.datetime) -> str:
    if value.tzinfo is not None:
        value = value.astimezone(_dt.timezone.utc)
        return value.strftime("%Y-%m-%d %H:%M:%S UTC")
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _event_duration(ev: Event) -> float | None:
    if ev.duration_ms is not None:
        return ev.duration_ms
    if ev.ended_at is not None:
        return max(0.0, (ev.ended_at - ev.started_at).total_seconds() * 1000.0)
    return None


def _tokens_text(m: RunMetrics, glyphs: Glyphs) -> str | None:
    if m.input_tokens is None and m.output_tokens is None:
        return None
    return f"{format_tokens(m.input_tokens)} in {glyphs.dot} {format_tokens(m.output_tokens)} out"


def cost_text(m: RunMetrics, glyphs: Glyphs) -> str | None:
    """``$0.0392 measured · $0.0225 estimated`` -- the two kinds are never summed."""
    parts = []
    if m.cost_measured is not None:
        parts.append(f"{format_cost(m.cost_measured, m.currency)} measured")
    if m.cost_estimated is not None:
        parts.append(f"{format_cost(m.cost_estimated, m.currency)} estimated")
    if not parts and m.currency == "mixed":
        return "mixed currencies (not summed)"
    return f" {glyphs.dot} ".join(parts) if parts else None


def calls_text(fixture: Fixture, glyphs: Glyphs) -> str:
    counts = {"tool": 0, "model": 0, "value": 0}
    for ev in fixture.calls():
        counts[ev.kind] += 1
    return f" {glyphs.dot} ".join(f"{n} {kind}" for kind, n in counts.items())


def summary_grid(fixture: Fixture, glyphs: Glyphs) -> RenderableType:
    """Two-column key/value overview of one run."""
    m = metrics(fixture)
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style=STYLES["muted"], no_wrap=True)
    grid.add_column()
    status = Text.assemble(status_glyph(fixture.status, glyphs), " ", fixture.status)
    if fixture.error is not None:
        status.append(f"  {fixture.error.type}: {fixture.error.message}", style=STYLES["fail"])
    grid.add_row("status", status)
    grid.add_row("duration", format_duration(m.duration_ms))
    grid.add_row(
        "events", f"{m.events} ({len(fixture.calls())} calls: {calls_text(fixture, glyphs)})"
    )
    if m.errors:
        grid.add_row("errors", Text(str(m.errors), style=STYLES["fail"]))
    tokens = _tokens_text(m, glyphs)
    if tokens:
        grid.add_row("tokens", tokens)
    cost = cost_text(m, glyphs)
    if cost:
        grid.add_row("cost", cost)
    if fixture.redaction.fields_omitted:
        n = len(fixture.redaction.fields_omitted)
        grid.add_row(
            "redaction",
            f"{fixture.redaction.policy_id} policy {glyphs.dot} "
            f"{n} field{'s' if n != 1 else ''} redacted",
        )
    return grid


def header(fixture: Fixture, glyphs: Glyphs) -> RenderableType:
    """Run name and id, then provenance (created, mode, source, versions)."""
    title = Text.assemble(
        (fixture.name, STYLES["heading"]), "  ", (fixture.fixture_id, STYLES["muted"])
    )
    bits = [
        f"{'recorded' if fixture.mode == 'record' else 'replayed'} {_timestamp(fixture.created_at)}"
    ]
    if fixture.source != "traceon":
        bits.append(f"imported from {fixture.source}")
    env = fixture.environment
    if env.traceon_version:
        bits.append(f"traceon {env.traceon_version}")
    if env.python:
        bits.append(f"Python {env.python}")
    return Group(title, Text(f" {glyphs.dot} ".join(bits), style=STYLES["muted"]))


# ---------------------------------------------------------------------------
# inspect
# ---------------------------------------------------------------------------


def _event_label(ev: Event) -> Text:
    if ev.kind == "span":
        return Text(ev.name, style=STYLES["label"])
    return Text(f"{ev.kind}:{ev.name}")


def _event_detail(ev: Event, glyphs: Glyphs) -> Text:
    detail = Text(style=STYLES["muted"])
    if ev.error is not None:
        detail.append(f"{ev.error.type}: {ev.error.message}", style=STYLES["fail"])
    elif ev.status == "cancelled":
        detail.append("cancelled", style=STYLES["changed"])
    usage = ev.usage
    if usage is not None:
        if usage.input_tokens is not None or usage.output_tokens is not None:
            if detail:
                detail.append(f" {glyphs.dot} ")
            detail.append(
                f"{format_tokens(usage.input_tokens)} in / {format_tokens(usage.output_tokens)} out"
            )
        if usage.cost is not None:
            if detail:
                detail.append(f" {glyphs.dot} ")
            suffix = "" if usage.cost.kind == "measured" else " est."
            detail.append(format_cost(usage.cost.amount, usage.cost.currency) + suffix)
    if ev.side_effect:
        if detail:
            detail.append(f" {glyphs.dot} ")
        detail.append("side effect", style=STYLES["changed"])
    return detail


def tree_renderable(fixture: Fixture, glyphs: Glyphs) -> RenderableType:
    """Span tree: guides, status glyphs, details and right-aligned durations."""
    table = Table(box=None, show_header=False, pad_edge=False, padding=(0, 1), expand=False)
    table.add_column("event", no_wrap=True, overflow="ellipsis", max_width=64)
    table.add_column("detail", overflow="fold", max_width=56)
    table.add_column("duration", justify="right", no_wrap=True, style=STYLES["muted"])
    children: dict[str | None, list[Event]] = {}
    for ev in fixture.events:
        children.setdefault(ev.parent_id, []).append(ev)

    def walk(parent: str | None, prefix: str) -> None:
        kids = children.get(parent, [])
        for i, ev in enumerate(kids):
            last = i == len(kids) - 1
            guide = "" if parent is None else prefix + (glyphs.corner if last else glyphs.tee)
            line = Text(guide, style=STYLES["muted"])
            if ev.kind != "span" or ev.status != "ok":
                line.append_text(status_glyph(ev.status, glyphs))
                line.append(" ")
            line.append_text(_event_label(ev))
            table.add_row(line, _event_detail(ev, glyphs), format_duration(_event_duration(ev)))
            child_prefix = (
                "" if parent is None else prefix + (glyphs.blank if last else glyphs.pipe)
            )
            walk(ev.event_id, child_prefix)

    walk(None, "")
    return table


def events_renderable(fixture: Fixture, glyphs: Glyphs) -> RenderableType:
    """Flat event table in sequence order."""
    table = Table(box=None, pad_edge=False, padding=(0, 2), header_style=STYLES["muted"])
    table.add_column("seq", justify="right", style=STYLES["muted"], no_wrap=True)
    table.add_column("", no_wrap=True)
    table.add_column("event", no_wrap=True)
    table.add_column("id", style=STYLES["muted"], no_wrap=True)
    table.add_column("parent", style=STYLES["muted"], no_wrap=True)
    table.add_column("detail", overflow="fold")
    table.add_column("duration", justify="right", style=STYLES["muted"], no_wrap=True)
    for ev in fixture.events:
        table.add_row(
            str(ev.sequence),
            status_glyph(ev.status, glyphs),
            _event_label(ev) if ev.kind != "span" else Text(f"span:{ev.name}"),
            ev.event_id,
            ev.parent_id or "",
            _event_detail(ev, glyphs),
            format_duration(_event_duration(ev)),
        )
    return table


def inspect_renderable(fixture: Fixture, glyphs: Glyphs, *, view: str) -> RenderableType:
    parts: list[RenderableType] = [
        header(fixture, glyphs),
        Text(""),
        summary_grid(fixture, glyphs),
        Text(""),
    ]
    if fixture.events:
        parts.append(
            tree_renderable(fixture, glyphs)
            if view == "tree"
            else events_renderable(fixture, glyphs)
        )
    else:
        parts.append(Text("no events recorded", style=STYLES["muted"]))
    if fixture.warnings:
        parts += [Text(""), Text(f"warnings ({len(fixture.warnings)})", style=STYLES["changed"])]
        parts += [Text(f"  {glyphs.dot} {w}", style=STYLES["muted"]) for w in fixture.warnings]
    return Group(*parts)


def inspect_json(fixture: Fixture) -> dict[str, Any]:
    """Machine-readable summary used by ``traceon inspect --json``."""
    return {
        "fixture_id": fixture.fixture_id,
        "run_id": fixture.run_id,
        "name": fixture.name,
        "schema_version": fixture.schema_version,
        "created_at": fixture.created_at.isoformat(),
        "source": fixture.source,
        "mode": fixture.mode,
        "status": fixture.status,
        "error": fixture.error.model_dump(mode="json") if fixture.error else None,
        "metrics": metrics(fixture).model_dump(mode="json"),
        "warnings": list(fixture.warnings),
        "events": [
            {
                "event_id": ev.event_id,
                "parent_id": ev.parent_id,
                "sequence": ev.sequence,
                "kind": ev.kind,
                "name": ev.name,
                "status": ev.status,
                "duration_ms": _event_duration(ev),
                "side_effect": ev.side_effect,
                "error": ev.error.model_dump(mode="json") if ev.error else None,
                "usage": ev.usage.model_dump(mode="json") if ev.usage else None,
            }
            for ev in fixture.events
        ],
    }


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


def semantic_warnings(fixture: Fixture) -> list[str]:
    """Problems that do not break the schema but weaken replay."""
    out: list[str] = []
    for ev in fixture.calls():
        if ev.call_key is None:
            out.append(f"{ev.kind}:{ev.name} ({ev.event_id}) has no call_key")
    for ev in fixture.events:
        if ev.ended_at is not None and ev.ended_at < ev.started_at:
            out.append(f"{ev.kind}:{ev.name} ({ev.event_id}) ends before it starts")
        if ev.status == "error" and ev.error is None:
            out.append(f"{ev.kind}:{ev.name} ({ev.event_id}) failed without error details")
    return out


# ---------------------------------------------------------------------------
# record / replay
# ---------------------------------------------------------------------------


def record_renderable(
    fixture: Fixture, path: str, glyphs: Glyphs, *, error: BaseException | None
) -> RenderableType:
    m = metrics(fixture)
    if error is None:
        head = Text.assemble(
            (f"{glyphs.ok} recorded ", STYLES["ok"]),
            (fixture.name, STYLES["label"]),
            (
                f"  {len(fixture.calls())} calls in {format_duration(m.duration_ms)}",
                STYLES["muted"],
            ),
        )
    else:
        head = Text.assemble(
            (f"{glyphs.fail} recorded ", STYLES["fail"]),
            (fixture.name, STYLES["label"]),
            ("  the entry raised ", STYLES["muted"]),
            (f"{type(error).__name__}: {error}", STYLES["fail"]),
        )
    grid = Table.grid(padding=(0, 2))
    grid.add_column(width=0)
    grid.add_column(style=STYLES["muted"], no_wrap=True)
    grid.add_column()
    grid.add_row("", "fixture", Text(path, style=STYLES["path"]))
    grid.add_row("", "calls", calls_text(fixture, glyphs))
    tokens = _tokens_text(m, glyphs)
    if tokens:
        grid.add_row("", "tokens", tokens)
    cost = cost_text(m, glyphs)
    if cost:
        grid.add_row("", "cost", cost)
    redacted = len(fixture.redaction.fields_omitted)
    if redacted:
        grid.add_row("", "redacted", f"{redacted} field{'s' if redacted != 1 else ''}")
    return Group(head, grid)


def replay_renderable(
    report: Any,
    fixture: Fixture,
    glyphs: Glyphs,
    *,
    elapsed_ms: float,
    network_blocked: bool,
    entry_error: BaseException | None,
    saved_to: str | None,
    max_divergences: int = 10,
) -> RenderableType:
    """Outcome of ``traceon replay``: verdict line, counters, first divergence."""
    divergences: Sequence[Divergence] = list(getattr(report, "divergences", []) or [])
    ok = bool(getattr(report, "ok", not divergences))
    served = int(getattr(report, "calls_served", 0) or 0)
    overridden = int(getattr(report, "calls_overridden", 0) or 0)
    total = int(getattr(report, "calls_total_recorded", len(fixture.calls())) or 0)
    attempts = int(getattr(report, "network_attempts", 0) or 0)

    parts: list[RenderableType] = []
    stats = Table.grid(padding=(0, 2))
    stats.add_column(width=0)
    stats.add_column(style=STYLES["muted"], no_wrap=True)
    stats.add_column()
    calls = f"{served}/{total} served from recording"
    if overridden:
        calls += f" {glyphs.dot} {overridden} overridden"
    stats.add_row("", "calls", calls)
    if network_blocked:
        net = Text("blocked")
        if attempts:
            net.append(
                f" {glyphs.dot} {attempts} attempt{'s' if attempts != 1 else ''} refused",
                style=STYLES["changed"],
            )
    else:
        net = Text("allowed", style=STYLES["changed"])
    stats.add_row("", "network", net)
    stats.add_row("", "time", format_duration(elapsed_ms))
    if saved_to:
        stats.add_row("", "saved", Text(saved_to, style=STYLES["path"]))
    if entry_error is not None and not divergences:
        stats.add_row(
            "",
            "raised",
            Text(f"{type(entry_error).__name__}: {entry_error}", style=STYLES["changed"]),
        )

    if ok and not divergences:
        parts.append(
            Text.assemble(
                (f"{glyphs.ok} replay matches the recording", STYLES["ok"]),
                ("  ", ""),
                (fixture.name, STYLES["muted"]),
            )
        )
        parts.append(stats)
        return Group(*parts)

    first = getattr(report, "first_divergence", None) or divergences[0]
    n = len(divergences)
    parts.append(
        Text.assemble(
            (f"{glyphs.fail} replay diverged", STYLES["fail"]),
            f"  {n} divergence{'s' if n != 1 else ''}",
            ("  ", ""),
            (fixture.name, STYLES["muted"]),
        )
    )
    parts.append(stats)
    parts.append(Text(""))
    parts.append(divergence_renderable(first, glyphs=glyphs))
    rest = [d for d in divergences if d is not first]
    if rest:
        parts.append(Text(""))
        listing = Table.grid(padding=(0, 2))
        listing.add_column(width=0)
        listing.add_column(style=STYLES["muted"], no_wrap=True)
        listing.add_column(no_wrap=True)
        listing.add_column(overflow="fold")
        for div in rest[:max_divergences]:
            where = divergence_location(div)
            listing.add_row("", where, Text(div.kind, style=STYLES["fail"]), div.message)
        if len(rest) > max_divergences:
            listing.add_row("", f"{glyphs.ellipsis} {len(rest) - max_divergences} more", "", "")
        parts.append(Text(f"  other divergences ({len(rest)})", style=STYLES["muted"]))
        parts.append(listing)
    return Group(*parts)
