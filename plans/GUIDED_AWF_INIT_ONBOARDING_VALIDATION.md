# Guided AWF Init Onboarding Validation

Plan: `plans/GUIDED_AWF_INIT_ONBOARDING_PLAN.md`

## Requirement Status

- Extend `awf init <repo>` with guided and silent profile creation: Complete.
- Preserve no-path `awf init` service bootstrap: Complete.
- Add `--guided/--no-guided`, `--write-profile`, `--yes`, `--template`, and
  `--force`: Complete.
- Auto-prompt only for interactive pretty output when no profile exists:
  Complete.
- Keep `--format json` non-interactive and structured: Complete.
- Allow profile creation while local service/doctor checks are unhealthy:
  Complete.
- Keep `awf profile init` supported: Complete.
- Update public onboarding docs and remove stale v2 request language: Complete.

## Evidence

- Updated `src/awf/cli/main.py` for project-mode init flags, JSON output,
  guided prompts, non-blocking local check warnings, and write/force handling.
- Updated `src/awf/profiles/onboarding.py` with typed preview customization
  for guided egress and validation-command choices.
- Added CLI/docs regression coverage in `tests/unit/cli/test_init.py` and
  `tests/unit/docs/test_public_docs_status.py`.
- Updated `docs/QUICKSTART.md`, `docs/GETTING_STARTED.md`, and
  `docs/PROJECT_ONBOARDING.md`.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py tests/unit/cli/test_profile_init.py tests/unit/docs -q`
  - Result: `168 passed`
- `uv run --python 3.12 --extra dev ruff check src/awf tests`
  - Result: passed
- `uv run --python 3.12 --extra dev mypy src/awf`
  - Result: passed
