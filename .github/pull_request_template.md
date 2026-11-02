## What and why

<!-- What does this change, and what problem does it solve? Link the issue: "Fixes #123". -->

## How it was tested

<!-- New or updated tests, commands you ran, platforms. -->

## Checklist

- [ ] `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy` and `uv run pytest` pass locally
- [ ] Tests cover the change, including failure cases; no test accesses the network
- [ ] Public API changes have type hints and docstrings, and the docs are updated
- [ ] `CHANGELOG.md` has an entry under `[Unreleased]` for user-visible changes
- [ ] Fixture format changes: `schemas/fixture.schema.json` regenerated with `python scripts/gen_schema.py`
- [ ] Committed fixtures were reviewed for secrets and personal data
