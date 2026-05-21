# PR274 CI Script Surface Fix Validation

Plan reference: `PR274_CI_SCRIPT_SURFACE_FIX_PLAN.md`

## Requirement Status

- Complete: Kept all work on the current AWF-managed branch; no push or branch switch was
  performed.
- Complete: Left the failing docs cleanup check unchanged.
- Complete: Removed the CI-only retry helper from root `scripts/` by moving it to
  `.github/scripts/ci_docker_build_retry.py`.
- Complete: Preserved the retry helper implementation and its focused regression tests by loading
  the helper directly from the GitHub Actions-owned path.
- Complete: Ran the provided focused repro before and after the fix.
- Complete: Ran narrow lint and format checks for the moved helper and affected tests.
- Complete: Prepared this validation record before the local commit.

## Evidence

Files changed:

- `.github/scripts/ci_docker_build_retry.py`
- `scripts/ci_docker_build_retry.py`
- `tests/unit/test_ci_docker_build_retry.py`
- `plans/PR274_CI_SCRIPT_SURFACE_FIX_PLAN.md`
- `plans/PR274_CI_SCRIPT_SURFACE_FIX_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/docs/test_api_surface_cleanup_docs.py::test_scripts_directory_contains_only_supported_generators -q`
  - Initial red run: failed because `scripts/ci_docker_build_retry.py` was an extra root script.
- `uv run --python 3.12 --extra dev pytest tests/unit/docs/test_api_surface_cleanup_docs.py::test_scripts_directory_contains_only_supported_generators -q`
  - Passed: `1 passed in 0.39s`.
- `uv run --python 3.12 --extra dev pytest tests/unit/test_ci_docker_build_retry.py -q`
  - Passed: `2 passed in 0.38s`.
- `uv run --python 3.12 --extra dev ruff check .github/scripts/ci_docker_build_retry.py tests/unit/test_ci_docker_build_retry.py tests/unit/docs/test_api_surface_cleanup_docs.py`
  - Passed.
- `uv run --python 3.12 --extra dev ruff format --check .github/scripts/ci_docker_build_retry.py tests/unit/test_ci_docker_build_retry.py tests/unit/docs/test_api_surface_cleanup_docs.py`
  - Passed.

## Remaining Gaps

None.
