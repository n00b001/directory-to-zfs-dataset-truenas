# ZFS Migration Tool

## Project
Deterministic, resumable ZFS dataset promotion and migration script for TrueNAS SCALE.

## Tooling
- Package manager: `uv` (always use `uv` — never `python`, `pip`, or `pyenv` directly)
- Lint/format: `ruff` (via pre-commit hooks)
- Test: `pytest` with `pytest-xdist` + `pytest-cov` (via pre-push hook)

## Commands
- Install deps: `uv sync`
- Run tests: `uv run pytest`
- Run pre-commit: `uv run pre-commit run --all-files`
- Run ruff check: `uv run ruff check .`
- Run ruff format: `uv run ruff format .`
- Run script: `uv run zfs-migrate`

## Git Hooks
- **pre-commit**: runs `ruff --fix` (auto-fix) then `ruff format`
- **pre-push**: runs `uv run pytest` — push aborts if tests fail

## Workflow
- Always commit and push after each change
- Commit messages end with: `Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>`
