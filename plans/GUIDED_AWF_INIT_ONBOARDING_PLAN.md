# Guided AWF Init Onboarding Plan

## Problem

`awf init <repo>` currently performs a read-only readiness preview and points users
to `awf profile init <repo> --write` to create `.awf/workspace.yml`. Fresh-project
onboarding should instead be centered on `awf init <repo>` with guided interactive
setup and silent automation-friendly profile creation.

## Requirements

- Extend `awf init <repo>` with guided and silent project profile creation.
- Keep `awf init` without a path as the existing local service bootstrap.
- Support `--guided/--no-guided`, `--write-profile`, `--yes`, `--template`, and
  `--force` in project mode.
- Only auto-prompt in interactive pretty output when no profile exists.
- Ensure `--format json` never prompts and returns structured preview/write state.
- Allow `.awf/workspace.yml` creation even when local service/doctor checks fail,
  while still surfacing those failures as warnings.
- Keep `awf profile init` supported as a lower-level expert command.
- Update public docs to make `awf init .` the primary onboarding path and remove
  stale v2 request language.

## Implementation Steps

1. Add/adjust CLI tests for silent write, JSON no-prompt, existing profile force
   behavior, unhealthy local checks, and guided input.
2. Add a small onboarding helper that can re-render a preview after applying
   egress and validation-command choices through the typed profile model.
3. Extend `awf init` project-mode flags and route only path-mode flags to project
   onboarding.
4. Update project onboarding output for preview/write/guided modes and preserve
   no-path bootstrap behavior.
5. Update Quickstart, Getting Started, Project Onboarding, and docs tests for the
   canonical `awf init .` flow.

## Verification

- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py tests/unit/cli/test_profile_init.py tests/unit/docs -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/cli/main.py src/awf/profiles/onboarding.py tests/unit/cli/test_init.py tests/unit/cli/test_profile_init.py tests/unit/docs`
- `uv run --python 3.12 --extra dev mypy src/awf`
