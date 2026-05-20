# CI uv Setup Plan

## Problem Statement And Scope

PR #272 CI fails before project validation runs. The failing jobs all stop in
workflow setup:

- `lint-and-type`: `astral-sh/setup-uv@v4` with `version: "0.5.x"` reports no
  available version.
- `python-full-coverage`: uv `0.5.6` installs, then `uv python install 3.12`
  tries a stale `python-build-standalone` URL and receives 404.
- `release-artifacts`: uv resolves to `0.5.31`, then the uv artifact download
  returns 404.
- `ci-required` fails because required jobs failed.

Scope is limited to GitHub Actions Python/uv setup. Do not weaken checks, skip
jobs, push, rebase, or switch branches.

## Requirements Checklist

- [ ] Replace the stale uv setup action/range with a current, pinned setup-uv
  action and uv release.
- [ ] Stop relying on `uv python install 3.12` in CI; use GitHub's Python setup
  action for Python 3.12 and keep the existing validation commands intact.
- [ ] Preserve all lint, type, coverage, package, and Docker validation steps.
- [ ] Validate workflow syntax and the local uv/Python dependency setup path.
- [ ] Commit the focused CI fix locally with a conventional commit message.

## Implementation Steps

1. Update `.github/workflows/ci.yml` in the Python jobs only.
2. Add `actions/setup-python` before `setup-uv` in `lint-and-type`,
   `python-full-coverage`, and `release-artifacts`.
3. Update `astral-sh/setup-uv` from `v4` with `0.5.x` to a current pinned
   action/release.
4. Remove the `uv python install 3.12` steps because Python will come from
   `actions/setup-python`.
5. Run focused validation.
6. Write `plans/CI_UV_SETUP_VALIDATION.md` with requirement status and evidence.

## Verification Commands And Pass Criteria

- `uv --version`
  - Pass: local uv is available for dependency setup validation.
- `uv python find 3.12`
  - Pass: Python 3.12 is discoverable without downloading stale standalone
    metadata.
- `uv sync --python 3.12 --extra dev`
  - Pass: project dependencies sync with Python 3.12.
- `uv run --python 3.12 --extra dev python -c "from pathlib import Path; import yaml; yaml.safe_load(Path('.github/workflows/ci.yml').read_text())"`
  - Pass: workflow YAML parses.
- `git diff --check`
  - Pass: no whitespace errors.
