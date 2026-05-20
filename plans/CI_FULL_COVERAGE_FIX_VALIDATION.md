# CI Full Coverage Fix Validation

Plan reference: `CI_FULL_COVERAGE_FIX_PLAN.md`

## Requirement Status

- Identify uncovered behavior in the touched protected-file classifier surface:
  Complete. Focused coverage showed missing paths in `quality_gates.py` and
  `protected_file_diffs.py`.
- Add focused regression tests for meaningful uncovered branches instead of
  changing coverage configuration: Complete. Added tests for protected diff
  parsing, conservative TOML/YAML classification, workflow shape validation,
  shell/validation-command classifiers, and small existing config/schema/log
  edge cases that contributed to the global threshold.
- Preserve protected quality-gate behavior already introduced in PR #268:
  Complete. Existing protected-file, executor, and PR monitor tests pass.
- Keep branch management and pushing under AWF control: Complete. No branch
  switch, rebase, push, or force-push was performed.
- Commit the fix locally with a conventional commit message: Complete. This
  validation file is included in the same local fix commit.

## Evidence

Changed files:

- `tests/unit/control/test_quality_gates.py`
- `tests/unit/control/test_protected_file_diffs.py`
- `tests/unit/service/test_config.py`
- `tests/unit/common/test_common_polish.py`
- `tests/unit/service/test_logs.py`
- `tests/unit/profiles/test_profiles.py`
- `tests/unit/profiles/test_project_onboarding.py`
- `tests/unit/api/test_schema_coverage_edges.py`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py tests/unit/control/test_protected_file_diffs.py --cov=awf.control.quality_gates --cov=awf.control.protected_file_diffs --cov-report=term-missing --cov-fail-under=99 -q`
  - Passed: 226 tests; focused protected-file coverage reached 99.50%.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py tests/unit/control/test_protected_file_diffs.py tests/unit/control/test_executor_validation_fix_cycle.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py -q`
  - Passed: 402 tests.
- `uv run --python 3.12 --extra dev pytest -n 8 --dist=loadscope --timeout=300 --cov=awf --cov-report=term-missing --cov-fail-under=99`
  - Completed with exit code 0: 6918 passed, 7 skipped; total coverage displayed at the 99% gate.
- `uv run --python 3.12 --extra dev coverage run --append -m pytest tests/unit/common/test_common_polish.py::TestSettings::test_discard_settings_constructor_fields_ignores_plain_weakrefs tests/unit/service/test_config.py::test_populate_compose_postgres_password_ignores_urls_without_password tests/unit/profiles/test_project_onboarding.py::test_preview_workspace_profile_falls_back_to_generated_yaml_for_non_repo_source -q && uv run --python 3.12 --extra dev coverage report --fail-under=99`
  - Passed: appended the final margin tests to the completed full-run coverage data and enforced the same 99% threshold.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py src/awf/control/protected_file_diffs.py tests/unit/control/test_quality_gates.py tests/unit/control/test_protected_file_diffs.py tests/unit/service/test_config.py tests/unit/common/test_common_polish.py tests/unit/service/test_logs.py tests/unit/profiles/test_profiles.py tests/unit/api/test_schema_coverage_edges.py tests/unit/profiles/test_project_onboarding.py`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf`
  - Passed.

## Gaps

No planned requirement remains missing. The local Docker daemon is unavailable
in this AWF workspace, so the full coverage run skipped the same Docker-marked
integration tests that explicitly self-skip when Docker is absent.
