"""Structural comparison of two recorded runs.

:func:`compare` aligns the *calls* of a baseline and a candidate fixture (tool,
model and value events, in order) with a longest-common-subsequence over their
``(kind, name)`` identity, then inspects every aligned pair for argument,
response, status and error differences. Unmatched calls become ``missing_call``
/ ``unexpected_call`` divergences, or ``name_mismatch`` / ``kind_mismatch`` when a
removed and an added call occupy the same slot between two matched calls.

All value comparisons go through :func:`traceon.core.compare.diff_values`, so
paths and messages are identical to the ones produced during replay.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from traceon.core.compare import ValueChange, diff_values
from traceon.core.schema import (
    AlignedPair,
    DiffReport,
    Divergence,
    DivergenceKind,
    Event,
    Fixture,
    Payload,
    RunMetrics,
)
from traceon.replay.matcher import change_phrase

__all__ = ["IgnoreRules", "compare", "label", "metrics"]

#: Upper bound on changes inspected per payload when ignore rules filter them.
_MAX_CHANGES = 10_000

#: Value events (``now``, ``random``, ``uuid4``) differ between any two live runs
#: by design, so their responses are not compared.
_RESPONSE_COMPARED_KINDS = frozenset({"tool", "model"})


# ---------------------------------------------------------------------------
# Ignore rules
# ---------------------------------------------------------------------------


def _pattern_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a JSONPath-like ignore pattern into a regular expression.

    Supported syntax (paths as produced by :mod:`traceon.core.compare`):

    * ``$.a.b`` / ``$.items[0]`` -- an exact path;
    * ``[*]`` -- any array index; ``.*`` -- any object key;
    * ``..key`` -- ``key`` at any depth (``$..id`` matches ``$.a[3].id``).

    A pattern also matches everything *below* the path it names. Patterns that
    do not start with ``$`` are anchored at the root (``"id"`` == ``"$.id"``).
    """
    p = pattern.strip()
    if not p.startswith("$"):
        p = "$" + (p if p.startswith((".", "[")) else "." + p)
    out: list[str] = []
    i = 0
    any_segment = r'(?:\.[^.\[]+|\[\d+\]|\["(?:[^"\\]|\\.)*"\])'
    while i < len(p):
        if p.startswith("..", i):
            out.append(any_segment + r"*\.")
            i += 2
        elif p.startswith("[*]", i):
            out.append(r"\[\d+\]")
            i += 3
        elif p.startswith(".*", i):
            out.append(r'(?:\.[^.\[]+|\["(?:[^"\\]|\\.)*"\])')
            i += 2
        else:
            out.append(re.escape(p[i]))
            i += 1
    return re.compile("".join(out) + r"(?:[.\[].*)?\Z")


@dataclass(frozen=True)
class IgnoreRules:
    """Compiled set of JSONPath patterns whose differences are ignored."""

    patterns: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "_compiled", tuple(_pattern_to_regex(p) for p in self.patterns))

    def matches(self, path: str) -> bool:
        """Whether a difference at ``path`` should be ignored."""
        compiled: tuple[re.Pattern[str], ...] = getattr(self, "_compiled", ())
        return any(rx.match(path) for rx in compiled)

    def first_change(self, expected: Any, actual: Any) -> ValueChange | None:
        """First difference between two canonical values that is not ignored."""
        if not self.patterns:
            changes = diff_values(expected, actual, limit=1)
            return changes[0] if changes else None
        for change in diff_values(expected, actual, limit=_MAX_CHANGES):
            if not self.matches(change.path):
                return change
        return None


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def metrics(fixture: Fixture) -> RunMetrics:
    """Aggregate duration, call counts, errors, tokens and cost of one run.

    Duration is taken from the single root span when there is one, otherwise from
    the earliest ``started_at`` to the latest ``ended_at``. Measured costs
    (reported by the provider) and estimated costs (computed from a price table)
    are summed separately and never mixed. If costs use more than one currency,
    no totals are computed and ``currency`` is ``"mixed"``.
    """
    events = fixture.events
    roots = [e for e in events if e.parent_id is None]
    duration: float | None = None
    if len(roots) == 1 and roots[0].kind == "span" and roots[0].duration_ms is not None:
        duration = roots[0].duration_ms
    elif events:
        ends = [e.ended_at for e in events if e.ended_at is not None]
        if ends:
            start = min(e.started_at for e in events)
            duration = max(0.0, (max(ends) - start).total_seconds() * 1000.0)

    input_tokens: int | None = None
    output_tokens: int | None = None
    measured: float | None = None
    estimated: float | None = None
    currencies: set[str] = set()
    for ev in events:
        usage = ev.usage
        if usage is None:
            continue
        if usage.input_tokens is not None:
            input_tokens = (input_tokens or 0) + usage.input_tokens
        if usage.output_tokens is not None:
            output_tokens = (output_tokens or 0) + usage.output_tokens
        cost = usage.cost
        if cost is None:
            continue
        currencies.add(cost.currency)
        if cost.kind == "measured":
            measured = (measured or 0.0) + cost.amount
        else:
            estimated = (estimated or 0.0) + cost.amount

    currency: str | None = None
    if len(currencies) == 1:
        currency = next(iter(currencies))
    elif len(currencies) > 1:
        currency, measured, estimated = "mixed", None, None

    return RunMetrics(
        status=fixture.status,
        duration_ms=duration,
        events=len(events),
        tool_calls=sum(1 for e in events if e.kind == "tool"),
        model_calls=sum(1 for e in events if e.kind == "model"),
        errors=sum(1 for e in events if e.status == "error"),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_measured=measured,
        cost_estimated=estimated,
        currency=currency,
    )


# ---------------------------------------------------------------------------
# Alignment
# ---------------------------------------------------------------------------


def _identity(ev: Event) -> tuple[str, str]:
    return (ev.kind, ev.name)


def _lcs_pairs(base: Sequence[Event], cand: Sequence[Event]) -> list[tuple[int, int]]:
    """Index pairs of a longest common subsequence by ``(kind, name)``.

    The common prefix and suffix are matched directly; the remaining middle uses
    the classic dynamic-programming table, so typical (local) diffs stay cheap.
    """
    n, m = len(base), len(cand)
    bk = [_identity(e) for e in base]
    ck = [_identity(e) for e in cand]
    lo = 0
    while lo < n and lo < m and bk[lo] == ck[lo]:
        lo += 1
    hi_b, hi_c = n, m
    while hi_b > lo and hi_c > lo and bk[hi_b - 1] == ck[hi_c - 1]:
        hi_b -= 1
        hi_c -= 1
    rows, cols = hi_b - lo, hi_c - lo
    table = [[0] * (cols + 1) for _ in range(rows + 1)]
    for i in range(rows - 1, -1, -1):
        row, below = table[i], table[i + 1]
        bi = bk[lo + i]
        for j in range(cols - 1, -1, -1):
            if bi == ck[lo + j]:
                row[j] = below[j + 1] + 1
            else:
                row[j] = max(below[j], row[j + 1])
    pairs = [(k, k) for k in range(lo)]
    i = j = 0
    while i < rows and j < cols:
        if bk[lo + i] == ck[lo + j]:
            pairs.append((lo + i, lo + j))
            i += 1
            j += 1
        elif table[i + 1][j] >= table[i][j + 1]:
            i += 1
        else:
            j += 1
    pairs.extend((hi_b + k, hi_c + k) for k in range(n - hi_b))
    return pairs


def label(ev: Event) -> str:
    """Human-readable call label, e.g. ``"tool:search"``."""
    return f"{ev.kind}:{ev.name}"


# ---------------------------------------------------------------------------
# Payload comparison
# ---------------------------------------------------------------------------


def _short_hash(value: str | None) -> str:
    if not value:
        return "<unknown>"
    algo, _, digest = value.partition(":")
    return f"{algo}:{digest[:12]}" if digest else value[:19]


def _payload_divergence(
    head: str,
    noun: str,
    base: Payload,
    cand: Payload,
    rules: IgnoreRules,
    *,
    base_key: str | None = None,
    cand_key: str | None = None,
) -> tuple[str | None, Any, Any, str] | None:
    """Compare two payloads; return ``(path, expected, actual, message)`` or ``None``.

    Captured values are compared structurally. When either side holds only a
    hash, the call keys (requests) or ``sha256`` digests are compared and the
    path is ``$``. Payloads not captured or redacted on either side are skipped.
    Messages use the replay engine's wording (``Call #3 tool:search diverged at
    $.query: expected "a", got "b"``) so diff and replay read identically.
    """
    if base.state == "captured" and cand.state == "captured":
        if base.truncated or cand.truncated:
            if base.sha256 and cand.sha256 and base.sha256 != cand.sha256:
                return (
                    "$",
                    base.sha256,
                    cand.sha256,
                    f"{head} diverged: hash {_short_hash(base.sha256)} in the baseline, "
                    f"{_short_hash(cand.sha256)} in the candidate (the payload was "
                    "truncated, so the changed field cannot be shown)",
                )
            return None
        change = rules.first_change(base.value, cand.value)
        if change is None:
            return None
        return (
            change.path,
            change.expected,
            change.actual,
            f"{head} diverged at {change.path}: {change_phrase(change, noun=noun)}",
        )
    if base_key is not None and cand_key is not None:
        if base_key != cand_key:
            return (
                "$",
                base_key,
                cand_key,
                f"{head} diverged: the request differs (call_key {_short_hash(base_key)} "
                f"in the baseline, {_short_hash(cand_key)} in the candidate). The request "
                "was not fully captured, so the changed field cannot be shown",
            )
        return None
    hashable = {"captured", "hash_only"}
    if (
        base.state in hashable
        and cand.state in hashable
        and base.sha256
        and cand.sha256
        and base.sha256 != cand.sha256
    ):
        return (
            "$",
            base.sha256,
            cand.sha256,
            f"{head} diverged: hash {_short_hash(base.sha256)} in the baseline, "
            f"{_short_hash(cand.sha256)} in the candidate (hash-only capture, so the "
            "changed field cannot be shown)",
        )
    return None


def _compare_pair(b: Event, c: Event, b_pos: int, rules: IgnoreRules) -> list[Divergence]:
    out: list[Divergence] = []
    lbl = label(b)
    head = f"Call #{b_pos} {lbl}"
    common: dict[str, Any] = {
        "position": b_pos,
        "baseline_event_id": b.event_id,
        "candidate_event_id": c.event_id,
        "label": lbl,
    }
    both = _both_captured(b.request, c.request)
    req = _payload_divergence(
        head,
        "argument",
        b.request,
        c.request,
        rules,
        base_key=None if both else b.call_key,
        cand_key=None if both else c.call_key,
    )
    if req is not None:
        path, expected, actual, message = req
        out.append(
            Divergence(
                kind="argument_mismatch",
                path=path,
                expected=expected,
                actual=actual,
                message=message,
                **common,
            )
        )
    if b.status != c.status:
        detail = f" ({c.error.type}: {c.error.message})" if c.error else ""
        out.append(
            Divergence(
                kind="status_mismatch",
                expected=b.status,
                actual=c.status,
                message=f"{head} diverged: status {b.status!r} in the baseline, "
                f"{c.status!r} in the candidate{detail}",
                **common,
            )
        )
    elif b.status == "error" and b.error is not None and c.error is not None:
        be, ce = b.error, c.error
        if be.type != ce.type or be.message != ce.message:
            out.append(
                Divergence(
                    kind="error_mismatch",
                    path="$.type" if be.type != ce.type else "$.message",
                    expected=f"{be.type}: {be.message}",
                    actual=f"{ce.type}: {ce.message}",
                    message=f"{head} failed differently: the baseline raised {be.type}: "
                    f"{be.message!r}, the candidate raised {ce.type}: {ce.message!r}",
                    **common,
                )
            )
    if b.kind in _RESPONSE_COMPARED_KINDS and b.status == "ok" and c.status == "ok":
        resp = _payload_divergence(f"{head} response", "field", b.response, c.response, rules)
        if resp is not None:
            path, expected, actual, message = resp
            out.append(
                Divergence(
                    kind="output_mismatch",
                    path=path,
                    expected=expected,
                    actual=actual,
                    message=message,
                    **common,
                )
            )
    return out


def _both_captured(a: Payload, b: Payload) -> bool:
    return a.state == "captured" and b.state == "captured"


# ---------------------------------------------------------------------------
# compare()
# ---------------------------------------------------------------------------


def compare(
    baseline: Fixture,
    candidate: Fixture,
    *,
    ignore_paths: Sequence[str] = (),
    compare_output: bool = True,
) -> DiffReport:
    """Compare two runs call by call and return a :class:`DiffReport`.

    Args:
        baseline: The reference run (usually the recording that is known good).
        candidate: The run under test.
        ignore_paths: JSONPath patterns whose differences are ignored in
            requests, responses and the run output, e.g. ``"$.timestamp"``,
            ``"$.items[*].id"`` or ``"$..request_id"``.
        compare_output: Also compare the final run output payloads.

    Divergence ``position`` values are indexes into ``baseline.calls()`` (for
    ``unexpected_call``: into ``candidate.calls()``). Divergences are ordered by
    where they occur in the aligned run; run-level ones (status, output) come
    last. ``first_divergence`` is the earliest one.
    """
    rules = IgnoreRules(tuple(ignore_paths))
    b_calls, c_calls = baseline.calls(), candidate.calls()
    pairs = _lcs_pairs(b_calls, c_calls)

    rows: list[AlignedPair] = []
    ordered: list[tuple[int, Divergence]] = []

    def flush_gap(b_lo: int, b_hi: int, c_lo: int, c_hi: int) -> None:
        removed = list(range(b_lo, b_hi))
        added = list(range(c_lo, c_hi))
        for bi in removed:
            ev = b_calls[bi]
            rows.append(
                AlignedPair(
                    status="removed", kind=ev.kind, name=ev.name, baseline_event_id=ev.event_id
                )
            )
        for ci in added:
            ev = c_calls[ci]
            rows.append(
                AlignedPair(
                    status="added", kind=ev.kind, name=ev.name, candidate_event_id=ev.event_id
                )
            )
        order = len(rows) - len(removed) - len(added)
        paired = min(len(removed), len(added))
        for k in range(paired):
            b, c = b_calls[removed[k]], c_calls[added[k]]
            pos = removed[k]
            if b.kind != c.kind:
                kind: DivergenceKind = "kind_mismatch"
                msg = (
                    f"Call #{pos} diverged: expected a {b.kind} call ({label(b)}), "
                    f"got a {c.kind} call ({label(c)})"
                )
                expected, actual = str(b.kind), str(c.kind)
            else:
                kind = "name_mismatch"
                msg = f"Call #{pos} diverged: expected {label(b)}, got {label(c)}"
                expected, actual = b.name, c.name
            ordered.append(
                (
                    order + k,
                    Divergence(
                        kind=kind,
                        position=pos,
                        baseline_event_id=b.event_id,
                        candidate_event_id=c.event_id,
                        label=label(c),
                        expected=expected,
                        actual=actual,
                        message=msg,
                    ),
                )
            )
        for k in range(paired, len(removed)):
            b = b_calls[removed[k]]
            ordered.append(
                (
                    order + k,
                    Divergence(
                        kind="missing_call",
                        position=removed[k],
                        baseline_event_id=b.event_id,
                        label=label(b),
                        expected=label(b),
                        message=f"Call #{removed[k]} {label(b)} is in the baseline but was never "
                        "made by the candidate",
                    ),
                )
            )
        for k in range(paired, len(added)):
            c = c_calls[added[k]]
            ordered.append(
                (
                    order + len(removed) + k,
                    Divergence(
                        kind="unexpected_call",
                        position=added[k],
                        candidate_event_id=c.event_id,
                        label=label(c),
                        actual=label(c),
                        message=f"Call #{added[k]} {label(c)} (candidate) is not in the baseline",
                    ),
                )
            )

    prev_b = prev_c = 0
    for bi, ci in pairs:
        flush_gap(prev_b, bi, prev_c, ci)
        b, c = b_calls[bi], c_calls[ci]
        found = _compare_pair(b, c, bi, rules)
        order = len(rows)
        rows.append(
            AlignedPair(
                status="changed" if found else "same",
                kind=b.kind,
                name=b.name,
                baseline_event_id=b.event_id,
                candidate_event_id=c.event_id,
            )
        )
        ordered.extend((order, d) for d in found)
        prev_b, prev_c = bi + 1, ci + 1
    flush_gap(prev_b, len(b_calls), prev_c, len(c_calls))

    tail = len(rows)
    b_err, c_err = baseline.error, candidate.error
    if baseline.status != candidate.status:
        b_detail = f" ({b_err.type}: {b_err.message})" if b_err else ""
        c_detail = f" ({c_err.type}: {c_err.message})" if c_err else ""
        ordered.append(
            (
                tail,
                Divergence(
                    kind="status_mismatch",
                    label="run:status",
                    expected=baseline.status,
                    actual=candidate.status,
                    message=f"Run status diverged: the baseline finished {baseline.status!r}{b_detail} "
                    f"but the candidate finished {candidate.status!r}{c_detail}",
                ),
            )
        )
    elif (
        b_err is not None
        and c_err is not None
        and (b_err.type != c_err.type or b_err.message != c_err.message)
    ):
        ordered.append(
            (
                tail,
                Divergence(
                    kind="error_mismatch",
                    label="run:error",
                    path="$.type" if b_err.type != c_err.type else "$.message",
                    expected=f"{b_err.type}: {b_err.message}",
                    actual=f"{c_err.type}: {c_err.message}",
                    message=f"Run failed differently: the baseline raised {b_err.type}: "
                    f"{b_err.message!r}, the candidate raised {c_err.type}: {c_err.message!r}",
                ),
            )
        )
    if compare_output:
        out = _payload_divergence("Run output", "field", baseline.output, candidate.output, rules)
        if out is not None:
            path, expected, actual, message = out
            ordered.append(
                (
                    tail + 1,
                    Divergence(
                        kind="output_mismatch",
                        label="run:output",
                        path=path,
                        expected=expected,
                        actual=actual,
                        message=message,
                    ),
                )
            )

    ordered.sort(key=lambda item: item[0])  # stable: keeps per-row order
    divergences = [d for _, d in ordered]
    return DiffReport(
        baseline_id=baseline.fixture_id,
        candidate_id=candidate.fixture_id,
        baseline_name=baseline.name,
        candidate_name=candidate.name,
        equivalent=not divergences,
        first_divergence=divergences[0] if divergences else None,
        divergences=divergences,
        alignment=rows,
        baseline_metrics=metrics(baseline),
        candidate_metrics=metrics(candidate),
    )
