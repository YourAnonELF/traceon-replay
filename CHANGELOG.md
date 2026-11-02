# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
The fixture format is versioned separately (`schema_version`).

## [Unreleased]

## [0.1.0] - 2026-11-02

First public release.

### Added

- Fixture format `traceon.fixture/v1`: versioned JSON with spans, tool, model
  and value events; payload states `captured`, `hash_only`, `redacted`,
  `not_captured`; token usage and measured / estimated cost; JSON Schema in
  `schemas/fixture.schema.json`.
- Recording SDK: `record_run` (sync and async), `@tool`, `@model`, `span`,
  `now`, `random`, `uuid4`; `CapturePolicy` (model prompts hashed by default)
  and `RedactionPolicy` (sensitive keys and common secret formats).
- Offline replay: `replay_run` with strict or by-key call ordering, strict and
  override modes, overrides by name or label, fail-closed matching with
  first-divergence reports (JSON path, expected, actual), recorded error
  re-raising, process-wide network blocking, `ReplayReport`.
- Diff engine: `traceon.compare` with LCS alignment, ignore paths, per-run
  metrics, text, JSON and Markdown output.
- CLI: `traceon record`, `replay`, `diff`, `inspect`, `validate`,
  `import langfuse`, `studio`.
- pytest plugin: `@pytest.mark.traceon`, `traceon_replay` and
  `traceon_cassette` fixtures, `--traceon-record={none,once,all}`,
  `assert_no_divergence`, GitHub Actions job summary.
- Traceon Studio: local read-only web UI for runs and diffs.
- Langfuse importer: `fetch_trace` (public API, basic auth, no redirects,
  20 MB cap) and `import_trace` with a documented field coverage matrix.
- Examples: basic agent, failed tool call reproduced and fixed, pytest
  regression suite.

[Unreleased]: https://github.com/YourAnonELF/traceon-replay/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/YourAnonELF/traceon-replay/releases/tag/v0.1.0
