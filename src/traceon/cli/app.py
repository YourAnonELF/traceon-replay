"""The ``traceon`` command line.

Exit status: ``0`` success, ``1`` divergence / regression / failed run,
``2`` usage error or unreadable / invalid fixture.
"""

from __future__ import annotations

import importlib
import json
import time
from pathlib import Path
from typing import Any, cast

import click
from rich.text import Text

from traceon._version import __version__
from traceon.cli import _views
from traceon.cli._console import (
    EXIT_DIVERGED,
    EXIT_ERROR,
    CliError,
    CliState,
    ColorMode,
)
from traceon.cli._entry import NO_INPUT, build_arguments, call_entry, load_entry, parse_json_option
from traceon.core.errors import FixtureError, TraceonError
from traceon.core.io import load_fixture, save_fixture
from traceon.core.limits import DEFAULT_LIMITS
from traceon.core.schema import Fixture
from traceon.diff.engine import compare
from traceon.diff.render import STYLES, diff_renderable, render_markdown

__all__ = ["main"]

_CONTEXT = {"help_option_names": ["-h", "--help"], "max_content_width": 100}


def _state() -> CliState:
    ctx = click.get_current_context()
    obj = ctx.find_object(CliState)
    if obj is None:  # commands invoked directly, outside the group
        obj = CliState.create(debug=False, color="auto")
        ctx.obj = obj
    return obj


def _require(module: str, feature: str) -> Any:
    """Import an optional Traceon component lazily, with a clear error if absent."""
    try:
        return importlib.import_module(module)
    except ImportError as exc:
        raise CliError(f"{feature} is unavailable: cannot import {module} ({exc})") from exc


def _load(path: str | Path) -> Fixture:
    return load_fixture(path)


class TraceonGroup(click.Group):
    """Click group that turns expected failures into concise ``error:`` lines."""

    def invoke(self, ctx: click.Context) -> Any:
        try:
            return super().invoke(ctx)
        except (click.exceptions.Exit, click.ClickException, click.Abort):
            raise
        except (CliError, TraceonError, OSError) as exc:
            state = ctx.find_object(CliState)
            debug = state.debug if state else bool(ctx.params.get("debug"))
            if debug:
                raise
            state = state or CliState.create(debug=False, color="auto")
            if isinstance(exc, CliError):
                state.error(exc.message, exc.hint)
                code = exc.exit_code
            elif isinstance(exc, OSError):
                state.error(
                    f"{exc.strerror or exc}" + (f": {exc.filename}" if exc.filename else "")
                )
                code = EXIT_ERROR
            else:
                state.error(str(exc))
                code = EXIT_ERROR
            ctx.exit(code)


@click.group(
    cls=TraceonGroup,
    context_settings=_CONTEXT,
    epilog="Run 'traceon COMMAND --help' for details and examples.",
)
@click.version_option(
    __version__, "-V", "--version", prog_name="traceon", message="%(prog)s %(version)s"
)
@click.option("--debug", is_flag=True, help="Show full tracebacks for errors.")
@click.option(
    "--color",
    type=click.Choice(["auto", "always", "never"]),
    default="auto",
    show_default=True,
    help="Colorize output. 'auto' disables color for pipes and when NO_COLOR is set.",
)
@click.pass_context
def main(ctx: click.Context, debug: bool, color: str) -> None:
    """Deterministic replay and time-travel debugging for AI agents.

    Record an agent run to a JSON fixture, replay it offline with zero side
    effects, and see exactly where behavior diverged.

    \b
    Exit status:
      0  success
      1  divergence, regression or failed run
      2  usage error, unreadable or invalid fixture
    """
    ctx.obj = CliState.create(debug=debug, color=cast(ColorMode, color))


# ---------------------------------------------------------------------------
# inspect
# ---------------------------------------------------------------------------


@main.command(
    epilog="""\b
Examples:
  traceon inspect fixtures/checkout.json
  traceon inspect fixtures/checkout.json --events
  traceon inspect fixtures/checkout.json --json | jq .metrics"""
)
@click.argument("fixture", type=click.Path(dir_okay=False, path_type=Path))
@click.option("--json", "as_json", is_flag=True, help="Print a machine-readable summary.")
@click.option(
    "--tree/--events",
    "tree",
    default=True,
    show_default=True,
    help="Show the span tree, or a flat table of events in sequence order.",
)
def inspect(fixture: Path, as_json: bool, tree: bool) -> None:
    """Summarize a recorded run: status, timings, tokens, cost and its span tree."""
    state = _state()
    fx = _load(fixture)
    if as_json:
        click.echo(json.dumps(_views.inspect_json(fx), indent=2, ensure_ascii=False))
        return
    state.out.print(_views.inspect_renderable(fx, state.glyphs, view="tree" if tree else "events"))


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


@main.command(
    epilog="""\b
Examples:
  traceon validate fixtures/*.json
  traceon validate --strict fixtures/checkout.json"""
)
@click.argument(
    "fixtures", nargs=-1, required=True, type=click.Path(dir_okay=False, path_type=Path)
)
@click.option(
    "--strict",
    is_flag=True,
    help="Also fail on warnings (missing call keys, inconsistent timings...).",
)
def validate(fixtures: tuple[Path, ...], strict: bool) -> None:
    """Check fixtures against the schema, size and depth limits.

    Exits with status 2 if any fixture is invalid (or, with --strict, has warnings).
    """
    state = _state()
    g = state.glyphs
    invalid = 0
    for path in fixtures:
        try:
            fx = _load(path)
        except FixtureError as exc:
            invalid += 1
            lines = str(exc).splitlines()
            state.out.print(Text.assemble((f"{g.fail} ", STYLES["fail"]), (str(path), "bold")))
            for line in lines[:8]:
                state.out.print(Text("    " + line, style=STYLES["muted"]))
            if len(lines) > 8:
                state.out.print(
                    Text(f"    {g.ellipsis} {len(lines) - 8} more lines", style=STYLES["muted"])
                )
            continue
        warnings = _views.semantic_warnings(fx) + list(fx.warnings)
        failed = strict and bool(warnings)
        invalid += int(failed)
        glyph = (
            (g.fail, STYLES["fail"])
            if failed
            else ((g.changed, STYLES["changed"]) if warnings else (g.ok, STYLES["ok"]))
        )
        summary = Text.assemble(
            (f"{glyph[0]} ", glyph[1]),
            (str(path), "bold"),
            (
                f"  {fx.name} {g.dot} {len(fx.events)} events {g.dot} {fx.schema_version}",
                STYLES["muted"],
            ),
        )
        if warnings:
            summary.append(
                f" {g.dot} {len(warnings)} warning{'s' if len(warnings) != 1 else ''}",
                style=STYLES["changed"],
            )
        state.out.print(summary)
        for w in warnings[:10]:
            state.out.print(Text(f"    {w}", style=STYLES["muted"]))
    total = len(fixtures)
    valid = total - invalid
    if invalid:
        state.out.print(
            Text.assemble(("\n", ""), (f"{invalid} of {total} invalid", STYLES["fail"]))
        )
        raise click.exceptions.Exit(EXIT_ERROR)
    state.out.print(
        Text(f"\n{valid} fixture{'s' if valid != 1 else ''} valid", style=STYLES["muted"])
    )


# ---------------------------------------------------------------------------
# record
# ---------------------------------------------------------------------------


@main.command(
    epilog="""\b
Examples:
  traceon record --entry my_agent.main:run --out fixtures/run.json \\
      --input '{"question": "What changed in v2?"}'
  traceon record --entry agent.py:main --out fixtures/run.json --input @input.json"""
)
@click.option(
    "--entry",
    required=True,
    metavar="MODULE:FUNC",
    help="Function to run, e.g. my_agent.main:run (or path/to/file.py:run). "
    "Async functions are supported.",
)
@click.option(
    "--out",
    "out",
    required=True,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Where to write the fixture.",
)
@click.option(
    "--input",
    "input_",
    metavar="JSON|@FILE",
    help="Input for the entry: a JSON object becomes keyword arguments, any other "
    "JSON value the single positional argument.",
)
@click.option("--name", help="Run name stored in the fixture (default: the function name).")
def record(entry: str, out: Path, input_: str | None, name: str | None) -> None:
    """Run an agent entry point once and record every call to a fixture."""
    state = _state()
    value = parse_json_option(input_, "--input") if input_ is not None else NO_INPUT
    fn = load_entry(entry)
    args, kwargs = build_arguments(fn, value)
    from traceon.sdk import record_run

    run_name = name or getattr(fn, "__name__", None) or entry.rpartition(":")[2]
    handle = record_run(run_name, out, input=None if value is NO_INPUT else value)
    error: BaseException | None = None
    try:
        with handle:
            result = call_entry(fn, args, kwargs)
            handle.set_output(result)
    except Exception as exc:
        error = exc
    try:
        fixture: Fixture = handle.fixture
    except Exception:
        if error is not None:
            raise CliError(
                f"the entry raised {type(error).__name__}: {error}; no fixture was produced",
                exit_code=EXIT_DIVERGED,
            ) from error
        raise
    state.out.print(_views.record_renderable(fixture, str(out), state.glyphs, error=error))
    if error is not None:
        if state.debug:
            raise error
        raise click.exceptions.Exit(EXIT_DIVERGED)


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------


def _parse_overrides(values: tuple[str, ...]) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    for item in values:
        name, sep, raw = item.partition("=")
        if not sep or not name.strip():
            raise click.BadParameter(f"expected NAME=JSON, got {item!r}", param_hint="'--override'")
        try:
            overrides[name.strip()] = json.loads(raw)
        except ValueError as exc:
            raise click.BadParameter(
                f"value for {name.strip()!r} is not valid JSON ({exc}); "
                f"""quote strings, e.g. {name.strip()}='"text"'""",
                param_hint="'--override'",
            ) from exc
    return overrides


def _fixture_input(fx: Fixture) -> Any:
    payload = fx.input
    if payload.state == "not_captured":
        return NO_INPUT
    if not payload.is_servable:
        reason = "truncated" if payload.truncated else payload.state
        raise CliError(
            f"the recorded input cannot be replayed ({reason})",
            hint="pass the input explicitly with --input JSON",
        )
    return payload.value


@main.command(
    epilog="""\b
Examples:
  traceon replay fixtures/run.json --entry my_agent.main:run
  traceon replay fixtures/run.json --entry my_agent.main:run --by-key
  traceon replay fixtures/run.json --entry my_agent.main:run \\
      --override 'tool:send_email={"status": "skipped"}' --save fixtures/replayed.json"""
)
@click.argument("fixture", type=click.Path(dir_okay=False, path_type=Path))
@click.option(
    "--entry",
    required=True,
    metavar="MODULE:FUNC",
    help="Function to replay, e.g. my_agent.main:run.",
)
@click.option(
    "--strict/--by-key",
    "strict",
    default=True,
    show_default=True,
    help="Match calls in recorded order, or per (kind, name) queue for concurrent agents.",
)
@click.option(
    "--override",
    "overrides",
    multiple=True,
    metavar="NAME=JSON",
    help="Serve this constant instead of the recording for a call name "
    "(e.g. search or tool:search). Repeatable.",
)
@click.option(
    "--input",
    "input_",
    metavar="JSON|@FILE",
    help="Input for the entry (default: the input stored in the fixture).",
)
@click.option(
    "--save",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Write the replayed run as a new fixture.",
)
@click.option("--allow-network", is_flag=True, help="Do not block network access during replay.")
def replay(
    fixture: Path,
    entry: str,
    strict: bool,
    overrides: tuple[str, ...],
    input_: str | None,
    save: Path | None,
    allow_network: bool,
) -> None:
    """Re-run an entry point against a recording, offline, and report divergences.

    Every tool call, model call and nondeterministic value is served from the
    fixture; nothing reaches the network. Exits with status 1 on divergence.
    """
    state = _state()
    fx = _load(fixture)
    parsed_overrides = _parse_overrides(overrides)
    value = parse_json_option(input_, "--input") if input_ is not None else _fixture_input(fx)
    fn = load_entry(entry)
    args, kwargs = build_arguments(fn, value)
    from traceon.replay import replay_run

    handle = replay_run(
        fx,
        mode="override" if parsed_overrides else "strict",
        overrides=parsed_overrides or None,
        order="strict" if strict else "by_key",
        block_network=not allow_network,
        raise_on_divergence=False,
        save_to=save,
    )
    entry_error: BaseException | None = None
    started = time.perf_counter()
    try:
        with handle:
            result = call_entry(fn, args, kwargs)
            handle.set_output(result)
    except Exception as exc:
        entry_error = exc
    elapsed = (time.perf_counter() - started) * 1000.0
    report = getattr(entry_error, "traceon_report", None)
    if report is None:
        try:
            report = handle.report
        except Exception:
            report = None
    if report is None:
        if entry_error is None:
            raise CliError("replay finished without a report")
        if isinstance(entry_error, TraceonError):
            raise entry_error
        raise CliError(
            f"replay failed: {type(entry_error).__name__}: {entry_error}", exit_code=EXIT_DIVERGED
        ) from entry_error
    state.out.print(
        _views.replay_renderable(
            report,
            fx,
            state.glyphs,
            elapsed_ms=elapsed,
            network_blocked=not allow_network,
            entry_error=entry_error,
            saved_to=str(save) if save else None,
        )
    )
    if not getattr(report, "ok", False):
        raise click.exceptions.Exit(EXIT_DIVERGED)


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------


@main.command(
    epilog="""\b
Examples:
  traceon diff fixtures/baseline.json fixtures/candidate.json
  traceon diff base.json cand.json --ignore '$..request_id' --ignore '$.timestamp'
  traceon diff base.json cand.json --markdown >> "$GITHUB_STEP_SUMMARY"
"""
)
@click.argument("baseline", type=click.Path(dir_okay=False, path_type=Path))
@click.argument("candidate", type=click.Path(dir_okay=False, path_type=Path))
@click.option("--json", "fmt", flag_value="json", help="Print the report as JSON.")
@click.option(
    "--markdown",
    "fmt",
    flag_value="markdown",
    help="Print GitHub-flavored Markdown (for CI summaries and PR comments).",
)
@click.option(
    "--ignore",
    "ignore",
    multiple=True,
    metavar="PATH",
    help="JSONPath to ignore in requests, responses and output, e.g. '$.timestamp', "
    "'$.items[*].id', '$..request_id'. Repeatable.",
)
@click.option("--no-output", is_flag=True, help="Do not compare the final run outputs.")
def diff(
    baseline: Path, candidate: Path, fmt: str | None, ignore: tuple[str, ...], no_output: bool
) -> None:
    """Compare two runs call by call. Exits with status 1 if they diverge."""
    state = _state()
    base, cand = _load(baseline), _load(candidate)
    report = compare(base, cand, ignore_paths=ignore, compare_output=not no_output)
    if fmt == "json":
        click.echo(report.model_dump_json(indent=2))
    elif fmt == "markdown":
        click.echo(
            render_markdown(report, baseline_label=str(baseline), candidate_label=str(candidate)),
            nl=False,
        )
    else:
        state.out.print(
            diff_renderable(
                report,
                glyphs=state.glyphs,
                baseline_label=str(baseline),
                candidate_label=str(candidate),
            )
        )
    if not report.equivalent:
        raise click.exceptions.Exit(EXIT_DIVERGED)


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------


@main.group("import", context_settings=_CONTEXT)
def import_() -> None:
    """Convert traces from other tools into Traceon fixtures."""


@import_.command(
    "langfuse",
    epilog="""\b
Examples:
  traceon import langfuse --trace-id 9f3c... --out fixtures/incident.json
  traceon import langfuse --file trace-export.json --out fixtures/incident.json

\b
Credentials come from LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY; the host from
LANGFUSE_BASE_URL or LANGFUSE_HOST (default https://cloud.langfuse.com).""",
)
@click.option("--trace-id", help="Fetch this trace from the Langfuse public API.")
@click.option(
    "--file",
    "file",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Read a trace JSON export (GET /api/public/traces/{id}) from disk.",
)
@click.option(
    "--out",
    required=True,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Where to write the fixture.",
)
@click.option("--host", help="Langfuse host (overrides LANGFUSE_BASE_URL / LANGFUSE_HOST).")
def import_langfuse(trace_id: str | None, file: Path | None, out: Path, host: str | None) -> None:
    """Import a Langfuse trace as a replayable fixture."""
    state = _state()
    if (trace_id is None) == (file is None):
        raise click.UsageError("pass exactly one of --trace-id or --file")
    langfuse = _require("traceon.integrations.langfuse", "the Langfuse importer")
    if file is not None:
        try:
            size = file.stat().st_size
        except OSError as exc:
            raise CliError(f"cannot read {file}: {exc.strerror or exc}") from exc
        if size > DEFAULT_LIMITS.max_fixture_bytes:
            raise CliError(f"{file} is {size} bytes; limit is {DEFAULT_LIMITS.max_fixture_bytes}")
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise CliError(f"{file} is not valid JSON: {exc}") from exc
        source = str(file)
    else:
        data = langfuse.fetch_trace(trace_id, host=host)
        source = f"trace {trace_id}"
    if not isinstance(data, dict):
        raise CliError(f"{source}: expected a JSON object with trace fields and 'observations'")
    try:
        fixture: Fixture = langfuse.import_trace(data)
    except (KeyError, TypeError, ValueError) as exc:
        raise CliError(f"cannot import {source}: {exc}") from exc
    save_fixture(fixture, out)
    g = state.glyphs
    state.out.print(
        Text.assemble(
            (f"{g.ok} imported ", STYLES["ok"]),
            (source, "bold"),
            (f" {g.arrow} ", STYLES["muted"]),
            (str(out), STYLES["path"]),
            (f"  {len(fixture.events)} events", STYLES["muted"]),
        )
    )
    if fixture.warnings:
        n = len(fixture.warnings)
        state.out.print(
            Text(
                f"  {n} field{'s' if n != 1 else ''} could not be mapped:", style=STYLES["changed"]
            )
        )
        for w in fixture.warnings[:10]:
            state.out.print(Text(f"    {w}", style=STYLES["muted"]))
        if n > 10:
            state.out.print(
                Text(
                    f"    {g.ellipsis} {n - 10} more (see the fixture's warnings)",
                    style=STYLES["muted"],
                )
            )


# ---------------------------------------------------------------------------
# studio
# ---------------------------------------------------------------------------


@main.command(
    epilog="""\b
Examples:
  traceon studio fixtures/
  traceon studio fixtures/ --recursive
  traceon studio base.json cand.json --port 8080 --no-open"""
)
@click.argument("paths", nargs=-1, type=click.Path(exists=True, path_type=Path))
@click.option(
    "--host",
    default="127.0.0.1",
    show_default=True,
    help="Interface to bind. Anything but loopback exposes your fixtures.",
)
@click.option(
    "--port",
    default=7777,
    show_default=True,
    type=click.IntRange(0, 65535),
    help="Port to listen on (0 picks a free port).",
)
@click.option("--no-open", is_flag=True, help="Do not open a browser.")
@click.option("--recursive", "-r", is_flag=True, help="Scan directories recursively for fixtures.")
def studio(paths: tuple[Path, ...], host: str, port: int, no_open: bool, recursive: bool) -> None:
    """Browse fixtures and diffs in a local web UI (no external requests).

    PATHS are fixture files or directories scanned for fixtures (default: the
    current directory). Stop the server with Ctrl+C.
    """
    state = _state()
    server = _require("traceon.studio.server", "Traceon Studio")
    studio_error: type[Exception] = getattr(server, "StudioError", TraceonError)
    targets = list(paths) or [Path.cwd()]
    try:
        server.serve(targets, host=host, port=port, open_browser=not no_open, recursive=recursive)
    except KeyboardInterrupt:
        state.out.print(Text("stopped", style=STYLES["muted"]))
    except studio_error as exc:
        raise CliError(str(exc)) from exc
    except OSError as exc:
        raise CliError(
            f"cannot serve on {host}:{port}: {exc.strerror or exc}",
            hint=f"try --port {port + 1}" if port else None,
        ) from exc
