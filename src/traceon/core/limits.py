"""Hard limits applied to fixtures and payloads.

Fixtures are treated as untrusted input: they may be shared in bug reports or
imported from third-party systems. Every reader and writer in Traceon enforces
these bounds so a malicious or accidental fixture cannot exhaust memory or
recursion depth.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Limits:
    #: Maximum size of a fixture file on disk, in bytes.
    max_fixture_bytes: int = 50 * 1024 * 1024
    #: Maximum size of a single captured payload (canonical JSON), in bytes.
    #: Larger payloads are truncated and marked ``truncated=True``.
    max_payload_bytes: int = 256 * 1024
    #: Maximum number of events in one fixture.
    max_events: int = 50_000
    #: Maximum JSON nesting depth of any value inside a fixture.
    max_json_depth: int = 64
    #: Maximum parent/child nesting depth of spans.
    max_span_depth: int = 128
    #: Maximum wall-clock seconds a replay may run before it is aborted.
    max_replay_seconds: float = 600.0


DEFAULT_LIMITS = Limits()
