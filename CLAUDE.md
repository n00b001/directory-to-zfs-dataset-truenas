# ZFS Migration Tool

## Project
Deterministic, resumable ZFS dataset promotion and migration script for TrueNAS SCALE.

## Tooling
- Package manager: `uv`
- Lint/format: `ruff` (via pre-commit hooks)
- Test: `pytest` (via pre-push hook)

## Commands
- Install dev deps: `uv pip install -e ".[dev,test]"`
- Run pre-commit: `pre-commit run --all-files`
- Run tests: `python -m pytest -q`
- Run ruff check: `ruff check .`
- Run ruff format: `ruff format .`

## Git Hooks
- **pre-commit**: runs `ruff --fix` (auto-fix) then `ruff format`
- **pre-push**: runs `pytest -q` — push aborts if tests fail

## Workflow
- Always commit and push after each change
- Commit messages end with: `Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>`
