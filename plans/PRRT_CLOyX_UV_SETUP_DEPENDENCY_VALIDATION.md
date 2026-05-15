# PRRT_CLOyX UV Setup Dependency Validation

Plan reference: `plans/PRRT_CLOyX_UV_SETUP_DEPENDENCY_PLAN.md`

## Requirement Status

- Complete: Added a regression test proving `uv run python scripts/bootstrap.py`
  with a generic DNS failure is not classified as setup dependency network
  failure.
- Complete: Preserved classification for `uv sync --extra dev`; the existing
  classifier test still passes.
- Complete: Removed broad `uv` command-token classification and added
  install/sync-style `uv` subcommand detection.
- Complete: Kept output-context fallback behavior unchanged.
- Complete: Ran targeted unit, lint, and type validation.
- Complete: Prepared the change for a local conventional commit tied to
  `PRRT_kwDOSJAM6s6CLOyX`.

## Evidence

Files changed:

- `src/awf/runtime/validation.py`
- `tests/unit/runtime/test_validation.py`
- `plans/PRRT_CLOyX_UV_SETUP_DEPENDENCY_PLAN.md`
- `plans/PRRT_CLOyX_UV_SETUP_DEPENDENCY_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py::test_setup_dependency_network_classifier_skips_uv_run_script_dns_failure -q`
  failed before the code change with the expected over-broad classification.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py::test_setup_dependency_network_classifier_skips_uv_run_script_dns_failure tests/unit/runtime/test_validation.py::test_setup_dependency_network_classifier_extracts_uv_pypi_dns_failure -q`
  passed after the fix.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q`
  passed: 149 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/validation.py tests/unit/runtime/test_validation.py`
  passed.
- `uv run --python 3.12 --extra dev mypy src/awf/runtime/validation.py`
  passed.

## Gaps

No remaining gaps.
