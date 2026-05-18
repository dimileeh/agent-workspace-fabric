# Comment 4482045018 Init Env Review Validation

Plan reference: `plans/comment_4482045018_init_env_review_PLAN.md`

## Requirement Status

- Complete: Add or use an explicit public bootstrap asset-root helper.
  - Evidence: `src/awf/service/bootstrap.py` now exposes
    `get_bootstrap_asset_root()`.
- Complete: Update CLI env-path resolution to call the public helper.
  - Evidence: `src/awf/cli/main.py` now calls
    `bootstrap_mod.get_bootstrap_asset_root()`.
- Complete: Update tests so asset-root stubbing targets the public helper.
  - Evidence: `_stub_bootstrap_mode()` in `tests/unit/cli/test_init.py` patches
    `get_bootstrap_asset_root`.
- Complete: Add regression coverage for parent-directory creation failure
  messaging.
  - Evidence:
    `test_init_without_path_warns_when_compose_env_parent_creation_fails`.
- Complete: Keep machine-readable `env_action == "write_failed"` for seeding
  failures.
  - Evidence: existing JSON regression
    `test_init_without_path_json_marks_env_write_failed` remains green.
- Complete: Confirm the path-write failure helper compares normalized paths.
  - Evidence: `_fail_path_write_bytes()` resolves both configured and observed
    paths before comparing.

## Verification

- Failed first as expected:
  `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py::test_init_without_path_warns_when_compose_env_parent_creation_fails -q`
  - Failure: `awf.service.bootstrap` had no `get_bootstrap_asset_root`.
- Passed:
  `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py::test_init_without_path_warns_when_compose_env_parent_creation_fails -q`
  - Result: `1 passed`.
- Passed:
  `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py -q`
  - Result: `47 passed`.
- Passed:
  `uv run --python 3.12 --extra dev ruff check src/awf tests/unit/cli/test_init.py`
  - Result: `All checks passed!`
- Passed:
  `uv run --python 3.12 --extra dev ruff format --check src/awf/cli/main.py tests/unit/cli/test_init.py`
  - Result: `2 files already formatted`.
- Passed:
  `uv run --python 3.12 --extra dev mypy src/awf`
  - Result: `Success: no issues found in 155 source files`.
- Passed:
  `git diff --check`

## Gaps

None.
