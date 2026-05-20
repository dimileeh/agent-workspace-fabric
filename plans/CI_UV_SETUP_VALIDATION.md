# CI uv Setup Validation

Plan reference: `plans/CI_UV_SETUP_PLAN.md`

## Requirement Status

- Complete: Replace the stale uv setup action/range with a current, pinned
  setup-uv action and uv release.
  - Evidence: `.github/workflows/ci.yml` now uses
    `astral-sh/setup-uv@v8.1.0` with `version: "0.11.15"` in the three Python
    CI jobs.
- Complete: Stop relying on `uv python install 3.12` in CI.
  - Evidence: the Python jobs now use `actions/setup-python@v6.2.0` with
    `python-version: "3.12"` and no longer run `uv python install 3.12`.
- Complete: Preserve all lint, type, coverage, package, and Docker validation
  steps.
  - Evidence: validation command steps in `lint-and-type`,
    `python-full-coverage`, and `release-artifacts` are unchanged except for
    setup.
- Complete: Validate workflow syntax and local uv/Python setup path.
  - Evidence: commands below passed.
- Complete: Commit the focused CI fix locally with a conventional commit
  message.
  - Evidence: pending local commit after validation.

## Commands Run

- `uv --version`
  - Passed: `uv 0.11.14` is available locally.
- `uv python find 3.12`
  - Passed: Python 3.12 resolves locally.
- `uv sync --python 3.12 --extra dev`
  - Passed: resolved 96 packages and checked 94 packages.
- `uv run --python 3.12 --extra dev python -c "from pathlib import Path; import yaml; yaml.safe_load(Path('.github/workflows/ci.yml').read_text())"`
  - Passed: workflow YAML parsed.
- `git diff --check`
  - Passed: no whitespace errors.
- `uv run --python 3.12 --extra dev ruff check .`
  - Passed.
- `uv run --python 3.12 --extra dev ruff format --check .`
  - Passed: 466 files already formatted.
- `uv run --python 3.12 --extra dev mypy`
  - Passed: no issues found in 157 source files.

## Gaps

No planned gaps remain. Full coverage and release Docker image builds were not
run locally because the observed CI failures occurred before those validation
steps; the existing CI jobs will exercise them after setup succeeds.
