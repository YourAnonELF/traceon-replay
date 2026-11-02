"""pytest integration for Traceon Replay (registered through the ``pytest11`` entry point).

Replay a recorded fixture around a whole test::

    @pytest.mark.traceon("fixtures/checkout.json", order="by_key")
    def test_checkout_agent():
        assert run_checkout_agent("order-42").status == "paid"

Replay explicitly inside a test::

    def test_refund(traceon_replay):
        with traceon_replay("fixtures/refund.json") as run:
            run.set_output(refund_agent("order-7"))

Record once, replay afterwards (vcrpy-style cassettes)::

    def test_research(traceon_cassette):
        with traceon_cassette as run:          # cassettes/<module>/<test>.json
            run.set_output(research_agent("What changed in v2?"))

``--traceon-record`` controls cassettes and markers: ``none`` (default) always
replays and fails if the fixture is missing, ``once`` records missing fixtures
and replays existing ones, ``all`` re-records everything. Relative fixture
paths are resolved against the test file's directory.

When a replay diverges, the failure report contains a rendered *first
divergence* section, and with ``$GITHUB_STEP_SUMMARY`` set a Markdown summary of
every Traceon test is appended to the job summary.
"""

from __future__ import annotations

import importlib
import os
import re
from collections.abc import Generator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any, Literal

import pytest

from traceon.core.errors import DivergenceError
from traceon.core.schema import Divergence
from traceon.diff.render import (
    divergence_location,
    divergence_markdown,
    render_divergence_text,
)

__all__ = ["Cassette", "TraceonDivergence", "assert_no_divergence"]

RecordMode = Literal["none", "once", "all"]
_RECORD_MODES: tuple[RecordMode, ...] = ("none", "once", "all")


# ---------------------------------------------------------------------------
# Assertions
# ---------------------------------------------------------------------------


class TraceonDivergence(AssertionError):
    """Raised by :func:`assert_no_divergence`; carries every divergence found."""

    def __init__(self, message: str, divergences: Sequence[Divergence]) -> None:
        super().__init__(message)
        self.divergences = list(divergences)
        self.divergence = self.divergences[0]


def _divergence_message(divergences: Sequence[Divergence]) -> str:
    first = divergences[0]
    text = render_divergence_text(first, color=False, unicode=False).rstrip()
    n = len(divergences)
    if n > 1:
        text += f"\n  ({n - 1} more divergence{'s' if n > 2 else ''})"
    return f"Traceon run diverged ({n} divergence{'s' if n != 1 else ''})\n{text}"


def assert_no_divergence(report: Any) -> None:
    """Fail the test if ``report`` (a ``ReplayReport`` or ``DiffReport``) has divergences.

    The assertion message is the rendered first divergence: call label,
    JSONPath, expected and actual values.
    """
    __tracebackhide__ = True
    divergences = list(getattr(report, "divergences", None) or [])
    if not divergences:
        return
    raise TraceonDivergence(_divergence_message(divergences), divergences)


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------


@dataclass
class _Use:
    """One fixture used by a test (via marker, ``traceon_replay`` or a cassette)."""

    path: Path
    mode: Literal["record", "replay"]
    handle: Any = None


@dataclass
class _Outcome:
    nodeid: str
    path: str
    mode: str
    ok: bool
    divergences: list[Divergence] = field(default_factory=list)
    error: str | None = None


@dataclass
class _State:
    outcomes: list[_Outcome] = field(default_factory=list)


_STATE = pytest.StashKey[_State]()
_USES = pytest.StashKey[list[_Use]]()


def _uses(item: pytest.Item) -> list[_Use]:
    uses = item.stash.get(_USES, None)
    if uses is None:
        uses = []
        item.stash[_USES] = uses
    return uses


# ---------------------------------------------------------------------------
# Opening record / replay handles
# ---------------------------------------------------------------------------


def _record_mode(config: pytest.Config) -> RecordMode:
    value = config.getoption("traceon_record", None) or config.getini("traceon_record") or "none"
    if value not in _RECORD_MODES:
        raise pytest.UsageError(
            f"traceon_record must be one of {', '.join(_RECORD_MODES)}; got {value!r}"
        )
    mode: RecordMode = value
    return mode


def _resolve(item: pytest.Item, path: str | os.PathLike[str]) -> Path:
    p = Path(path)
    return p if p.is_absolute() else Path(item.path).parent / p


def _decide(path: Path, record_mode: RecordMode) -> Literal["record", "replay"]:
    if record_mode == "all" or (record_mode == "once" and not path.exists()):
        return "record"
    return "replay"


def _missing_message(path: Path) -> str:
    return f"Traceon fixture not found: {path}\nRecord it with: pytest --traceon-record=once"


def _open_replay(path: Path, kwargs: Mapping[str, Any]) -> Any:
    module = importlib.import_module("traceon.replay")
    return module.replay_run(path, **dict(kwargs))


def _open_record(path: Path, name: str, input_value: Any) -> Any:
    module = importlib.import_module("traceon.sdk")
    return module.record_run(name, path, input=input_value)


def _run_name(item: pytest.Item) -> str:
    return item.name[:512]


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("traceon", "Traceon Replay")
    group.addoption(
        "--traceon-record",
        dest="traceon_record",
        choices=_RECORD_MODES,
        default=None,
        help="Record Traceon fixtures: 'none' (default) only replays, 'once' records "
        "missing fixtures, 'all' re-records every fixture.",
    )
    parser.addini("traceon_record", "Default for --traceon-record.", default="none")


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "traceon(fixture, *, mode='strict', order='strict', overrides=None, **replay_kwargs): "
        "replay a recorded Traceon fixture around this test.",
    )
    config.stash[_STATE] = _State()


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: pytest.Item) -> Generator[None, object, object]:
    marker = item.get_closest_marker("traceon")
    if marker is None:
        return (yield)
    if len(marker.args) > 1:
        raise pytest.UsageError("@pytest.mark.traceon takes a single fixture path")
    kwargs = dict(marker.kwargs)
    raw_path = marker.args[0] if marker.args else kwargs.pop("fixture", None)
    if raw_path is None:
        raise pytest.UsageError(
            f"{item.nodeid}: @pytest.mark.traceon needs a fixture path, "
            "e.g. @pytest.mark.traceon('fixtures/run.json')"
        )
    path = _resolve(item, raw_path)
    mode = _decide(path, _record_mode(item.config))
    if mode == "replay" and not path.exists():
        pytest.fail(_missing_message(path), pytrace=False)
    if mode == "record":
        handle = _open_record(path, _run_name(item), None)
    else:
        handle = _open_replay(path, kwargs)
    _uses(item).append(_Use(path=path, mode=mode, handle=handle))
    with handle:
        return (yield)


def _divergences_from(exc: BaseException | None) -> list[Divergence]:
    if isinstance(exc, (DivergenceError, TraceonDivergence)):
        return list(exc.divergences)
    report = getattr(exc, "traceon_report", None)
    return list(getattr(report, "divergences", None) or [])


def _report_of(handle: Any) -> Any:
    try:
        return handle.report
    except Exception:
        return None


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    rep = yield
    if call.when != "call":
        return rep
    exc = call.excinfo.value if call.excinfo is not None else None
    divergences = _divergences_from(exc)
    if divergences:
        text = render_divergence_text(divergences[0], color=False, unicode=False).rstrip()
        if len(divergences) > 1:
            lines = [text, "", f"other divergences ({len(divergences) - 1}):"]
            for div in divergences[1:11]:
                lines.append(f"  {divergence_location(div)}  {div.kind}: {div.message}")
            text = "\n".join(lines)
        rep.sections.append(("traceon: first divergence", text))

    state = item.config.stash.get(_STATE, None)
    uses = item.stash.get(_USES, [])
    if state is not None:
        for use in uses:
            report = _report_of(use.handle) if use.mode == "replay" else None
            divs = list(getattr(report, "divergences", None) or []) or divergences
            state.outcomes.append(
                _Outcome(
                    nodeid=item.nodeid,
                    path=_display_path(use.path, item.config),
                    mode=use.mode,
                    ok=rep.passed and not divs,
                    divergences=divs,
                    error=None if rep.passed or divs else _short_error(exc),
                )
            )
        if not uses and isinstance(exc, TraceonDivergence):
            state.outcomes.append(
                _Outcome(
                    nodeid=item.nodeid, path="", mode="assert", ok=False, divergences=divergences
                )
            )
    return rep


def _short_error(exc: BaseException | None) -> str | None:
    if exc is None:
        return None
    first = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
    return f"{type(exc).__name__}: {first}" if first else type(exc).__name__


def _display_path(path: Path, config: pytest.Config) -> str:
    try:
        return path.resolve().relative_to(Path(config.rootpath).resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def pytest_terminal_summary(terminalreporter: Any, exitstatus: int, config: pytest.Config) -> None:
    del exitstatus
    state = config.stash.get(_STATE, None)
    if state is None or not state.outcomes:
        return
    replays = [o for o in state.outcomes if o.mode == "replay"]
    records = [o for o in state.outcomes if o.mode == "record"]
    diverged = [o for o in state.outcomes if o.divergences]
    parts = []
    if replays:
        parts.append(f"{len(replays)} replayed")
    if records:
        parts.append(f"{len(records)} recorded")
    if diverged:
        parts.append(f"{len(diverged)} diverged")
    terminalreporter.write_line("traceon: " + ", ".join(parts))


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    del exitstatus
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    config = session.config
    if not target or hasattr(config, "workerinput"):
        return
    state = config.stash.get(_STATE, None)
    if state is None or not state.outcomes:
        return
    try:
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(render_summary_markdown(state.outcomes))
    except OSError as exc:  # never fail the test run because of the summary
        config.issue_config_time_warning(
            pytest.PytestWarning(f"traceon: cannot write GITHUB_STEP_SUMMARY: {exc}"), 2
        )


def _md_escape(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ").replace("<", "&lt;").replace(">", "&gt;")


def render_summary_markdown(outcomes: Sequence[_Outcome]) -> str:
    """Markdown job summary of every Traceon test in the session."""
    diverged = [o for o in outcomes if o.divergences]
    failed = [o for o in outcomes if not o.ok]
    title = (
        f"### Traceon: {len(diverged)} of {len(outcomes)} diverged"
        if diverged
        else f"### Traceon: {len(outcomes)} fixture{'s' if len(outcomes) != 1 else ''}, "
        f"{'all passing' if not failed else f'{len(failed)} failing'}"
    )
    lines = [title, "", "| test | fixture | mode | result |", "|---|---|---|---|"]
    for o in outcomes:
        if o.divergences:
            first = o.divergences[0]
            result = f"diverged: {first.kind} at {divergence_location(first)}"
        elif o.ok:
            result = "ok"
        else:
            result = f"failed: {_md_escape(o.error or 'error')}"
        path = f"`{o.path}`" if o.path else ""
        lines.append(f"| `{_md_escape(o.nodeid)}` | {path} | {o.mode} | {result} |")
    for o in diverged:
        lines += ["", f"#### `{_md_escape(o.nodeid)}`", "", divergence_markdown(o.divergences[0])]
    return "\n".join(lines) + "\n\n"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def traceon_replay(request: pytest.FixtureRequest) -> Any:
    """Factory: ``traceon_replay(path, **replay_kwargs)`` returns a replay handle.

    Use it as a (sync or async) context manager. Keyword arguments are passed to
    :func:`traceon.replay.replay_run`; relative paths are resolved against the
    test file's directory.
    """
    item = request.node

    def open_replay(path: str | os.PathLike[str], **kwargs: Any) -> Any:
        resolved = _resolve(item, path)
        if not resolved.exists():
            pytest.fail(_missing_message(resolved), pytrace=False)
        handle = _open_replay(resolved, kwargs)
        _uses(item).append(_Use(path=resolved, mode="replay", handle=handle))
        return handle

    return open_replay


_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def _default_cassette_path(item: pytest.Item) -> Path:
    module = Path(item.path)
    test_name = _UNSAFE.sub("_", item.name).strip("_") or "test"
    return module.parent / "cassettes" / module.stem / f"{test_name}.json"


class Cassette:
    """Records on first use and replays afterwards, per ``--traceon-record``.

    Enter it directly (``with traceon_cassette as run``) for the default path
    ``cassettes/<test module>/<test name>.json`` next to the test file, or call
    it to customize: ``traceon_cassette("fixtures/x.json", input={...}, order="by_key")``.
    The value bound by ``as`` is the underlying record or replay handle; both
    support ``set_output(value)``.
    """

    def __init__(
        self,
        item: pytest.Item,
        path: Path,
        *,
        record_mode: RecordMode,
        name: str,
        input: Any = None,
        replay_kwargs: Mapping[str, Any] | None = None,
    ) -> None:
        self._item = item
        self.path = path
        self.record_mode: RecordMode = record_mode
        self.name = name
        self.input = input
        self.replay_kwargs = dict(replay_kwargs or {})
        self.handle: Any = None

    @property
    def mode(self) -> Literal["record", "replay"]:
        """What entering the cassette will do right now: ``record`` or ``replay``."""
        return _decide(self.path, self.record_mode)

    def __call__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        name: str | None = None,
        input: Any = None,
        **replay_kwargs: Any,
    ) -> Cassette:
        resolved = _resolve(self._item, path) if path is not None else self.path
        return Cassette(
            self._item,
            resolved,
            record_mode=self.record_mode,
            name=name or self.name,
            input=input,
            replay_kwargs=replay_kwargs,
        )

    def _open(self) -> Any:
        if self.handle is not None:
            raise RuntimeError(
                "this cassette is already in use; call traceon_cassette() to get a fresh one"
            )
        mode = self.mode
        if mode == "replay":
            if not self.path.exists():
                pytest.fail(_missing_message(self.path), pytrace=False)
            self.handle = _open_replay(self.path, self.replay_kwargs)
        else:
            self.handle = _open_record(self.path, self.name, self.input)
        _uses(self._item).append(_Use(path=self.path, mode=mode, handle=self.handle))
        return self.handle

    def __enter__(self) -> Any:
        return self._open().__enter__()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None:
        result: bool | None = self.handle.__exit__(exc_type, exc, tb)
        return result

    async def __aenter__(self) -> Any:
        return await self._open().__aenter__()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None:
        result: bool | None = await self.handle.__aexit__(exc_type, exc, tb)
        return result


@pytest.fixture
def traceon_cassette(request: pytest.FixtureRequest) -> Cassette:
    """A :class:`Cassette` for this test (record once, replay afterwards)."""
    item = request.node
    return Cassette(
        item,
        _default_cassette_path(item),
        record_mode=_record_mode(request.config),
        name=_run_name(item),
    )
