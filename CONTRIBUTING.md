# Contributing to Traceon Replay

Thanks for your interest. Bug reports, fixes, documentation improvements and
well-scoped features are welcome. For larger changes, please open an issue
first so we can agree on the approach before you invest time.

## Development setup

You need Python 3.10+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/YourAnonELF/traceon-replay.git
cd traceon-replay
uv sync --extra dev          # creates .venv and installs traceon-replay in editable mode
```

Optionally install the git hooks (ruff on commit):

```bash
uv run --with pre-commit pre-commit install
```

## Checks

Run these before opening a pull request; CI runs the same commands on Linux,
macOS and Windows with Python 3.10-3.13.

```bash
uv run ruff check .                        # lint
uv run ruff format --check .               # formatting (fix with: uv run ruff format .)
uv run mypy                                # type check (strict, configured in pyproject.toml)
uv run pytest                              # unit, integration and end-to-end tests
uv run python scripts/gen_schema.py --check   # fixture JSON Schema is up to date
```

Tests must not access the network. Use recorded JSON samples (see
`tests/integrations/data/`) or fakes instead.

If you change `traceon/core/schema.py`, regenerate the JSON Schema and commit
it:

```bash
uv run python scripts/gen_schema.py
```

If you change the examples, run them:

```bash
uv run python examples/basic_agent/replay.py
uv run python examples/failed_tool_call/replay.py
uv run pytest examples/regression_test
```

Re-generate example fixtures (`record.py` in each example) only when the
recorded behavior is meant to change, and mention it in the pull request.

## Project layout

| Path | Contents |
|---|---|
| `src/traceon/core/` | Fixture schema, canonical JSON, comparison, I/O, runtime contract |
| `src/traceon/sdk/` | Recording API |
| `src/traceon/replay/` | Replay engine and network guard |
| `src/traceon/diff/` | Diff engine and renderers |
| `src/traceon/cli/`, `src/traceon/pytest_plugin.py` | CLI and pytest plugin |
| `src/traceon/studio/` | Local web UI |
| `src/traceon/integrations/` | Importers (Langfuse) |
| `tests/<area>/` | Tests per area; `tests/e2e/` exercises the public API, CLI and examples |
| `docs/` | Documentation |

See [docs/architecture.md](docs/architecture.md) for how the pieces fit.

## Guidelines

- Public functions and classes have type hints and docstrings. `mypy --strict`
  must pass.
- Keep runtime dependencies to pydantic, click and rich. New dependencies need
  a strong reason; prefer the standard library.
- Error messages should say what went wrong and what to do about it.
- Security-relevant code (redaction, fixture loading, network guard, Studio,
  importers) needs tests for the failure cases, not just the happy path.
- Changes to the fixture format are breaking for other readers; discuss them
  in an issue first.
- Add an entry under `## [Unreleased]` in [CHANGELOG.md](CHANGELOG.md) for
  user-visible changes.

## Commit messages

Use short, imperative subject lines, optionally with a
[Conventional Commits](https://www.conventionalcommits.org) prefix:

```text
fix(replay): report hash mismatch for hash-only requests
feat(cli): add --ignore to traceon diff
docs: explain by-key ordering
```

Explain the *why* in the body when it is not obvious. One logical change per
commit; squash fix-ups before review.

No Developer Certificate of Origin sign-off and no CLA are required. By
submitting a contribution you agree that it is licensed under the
[Apache License 2.0](LICENSE), as stated in section 5 of the license.

## Reporting bugs

Open an issue using the bug template. A fixture that reproduces the problem is
the most useful attachment -- but **review it for secrets and personal data
first**; redaction is pattern-based and not exhaustive. For security issues,
follow [SECURITY.md](SECURITY.md) instead.

## Code of conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md).
