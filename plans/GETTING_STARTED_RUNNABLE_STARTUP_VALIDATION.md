# Getting Started Runnable Startup Validation

Plan reference: `plans/GETTING_STARTED_RUNNABLE_STARTUP_PLAN.md`

## Requirement Status

- Complete: Replaced the Getting Started copy-paste startup block with
  `awf service bootstrap` followed by `awf service status --format pretty`.
- Complete: Kept `awf setup` and `awf start` as reserved future command
  surfaces and documented `AWF_SETUP_PLACEHOLDER` / `AWF_START_PLACEHOLDER`.
- Complete: Preserved `awf init <path>` project onboarding guidance.
- Complete: Added focused docs regression coverage for the Getting Started
  runnable startup path and adjacent environment note.
- Complete: Ran only targeted local docs validation. Full AWF/GitHub
  validation remains owned by AWF after agent completion.

## Evidence

Changed files:

- `docs/GETTING_STARTED.md`
- `tests/unit/docs/test_public_docs_status.py`
- `plans/GETTING_STARTED_RUNNABLE_STARTUP_PLAN.md`
- `plans/GETTING_STARTED_RUNNABLE_STARTUP_VALIDATION.md`

Focused checks:

- Failing-first confirmation:
  `uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_getting_started_uses_runnable_startup_path -q`
  failed before the docs update because Getting Started did not contain the
  runnable `awf service bootstrap` startup sequence.
- Final targeted validation:
  `uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_getting_started_uses_runnable_startup_path tests/unit/docs/test_public_docs_status.py::test_quickstart_uses_runnable_startup_path tests/unit/docs/test_public_docs_status.py::test_project_onboarding_docs_make_awf_init_primary -q`
  passed with `3 passed`.

## Gaps

No known gaps.
