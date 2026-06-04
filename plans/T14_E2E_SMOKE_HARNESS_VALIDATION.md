# T14 E2E Smoke Harness Validation

Plan reference: `plans/T14_E2E_SMOKE_HARNESS_PLAN.md`

## Requirement Status

- Complete: Added a focused regression test in
  `tests/unit/scripts/test_first_run_smoke.py` for `run_command` timeout
  handling.
- Complete: `scripts/first_run_smoke.py::run_command` now catches
  `subprocess.TimeoutExpired` and returns a `CompletedProcess` with return code
  `124`.
- Complete: Captured timeout stdout/stderr are normalized to text, and stderr
  includes a clear timeout message.
- Complete: Changes are limited to the smoke harness, its focused unit test,
  and the required plan/validation files.

## Evidence

- Confirmed pre-fix failure:
  `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_first_run_smoke.py::test_run_command_reports_timeout_as_failed_process -q`
  failed with an uncaught `subprocess.TimeoutExpired`.
- Post-fix targeted regression:
  `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_first_run_smoke.py::test_run_command_reports_timeout_as_failed_process -q`
  passed.
- Focused module test:
  `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_first_run_smoke.py -q`
  passed with `9 passed`.
- File-scoped lint:
  `uv run --python 3.12 --extra dev ruff check scripts/first_run_smoke.py tests/unit/scripts/test_first_run_smoke.py`
  passed.
- File-scoped format check:
  `uv run --python 3.12 --extra dev ruff format --check scripts/first_run_smoke.py tests/unit/scripts/test_first_run_smoke.py`
  passed.

Full AWF/GitHub validation was not run in the agent phase; AWF owns broad
validation, provenance, logs, and merge gating after completion.

## CI Repair Iteration: PR #394 Python Full Coverage

### Requirement Status

- Complete: The failing source smoke integration tests remain active and their
  pass/source-checkout assertions were not weakened.
- Complete: `test_default_profile_preview_direct_call` now uses `tmp_path`
  instead of writing malformed project metadata to global `/tmp`.
- Complete: `scripts/first_run_smoke.py` writes a minimal valid parent
  `pyproject.toml` at each source lane smoke root so uv does not walk up to an
  unrelated `/tmp/pyproject.toml`.
- Complete: `_default_profile_preview` coverage is preserved with an assertion
  that the helper receives the temp project root.
- Complete: Only focused repro and lint commands were run locally.

### Evidence

- Confirmed pre-fix CI-shaped repro:
  a focused command that temporarily wrote malformed `/tmp/pyproject.toml` and
  ran `tests/integration/test_first_run_smoke.py::test_source_uv_run_lane_proves_checkout_from_outside`
  plus `tests/integration/test_first_run_smoke.py::test_source_tool_install_lane_installs_isolated_awf`
  failed with `2 failed`, matching the GitHub Actions `project.version` error.
- Confirmed pre-fix sentinel regression:
  `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_first_run_smoke.py::test_prepare_source_lane_dirs_writes_parent_project_sentinel -q`
  failed because the source lane root had no parent project sentinel.
- Post-fix polluted-temp repro:
  `PATH=/tmp/awf-uv-0.5.31/bin:$PATH uv run --python 3.12 --extra dev pytest tests/unit/service/test_smoke.py::TestCollectSmokeReportExceptionPaths::test_default_profile_preview_direct_call tests/unit/scripts/test_first_run_smoke.py::test_prepare_source_lane_dirs_writes_parent_project_sentinel tests/integration/test_first_run_smoke.py::test_source_uv_run_lane_proves_checkout_from_outside tests/integration/test_first_run_smoke.py::test_source_tool_install_lane_installs_isolated_awf -q`
  passed with `4 passed` while the command temporarily created and then
  restored malformed `/tmp/pyproject.toml`.
- AWF-provided focused repro:
  `uv run --python 3.12 --extra dev pytest tests/integration/test_first_run_smoke.py::test_source_uv_run_lane_proves_checkout_from_outside tests/integration/test_first_run_smoke.py::test_source_tool_install_lane_installs_isolated_awf -q`
  passed with `2 passed`.
- Focused lint:
  `uv run --python 3.12 --extra dev ruff check scripts/first_run_smoke.py tests/unit/scripts/test_first_run_smoke.py tests/unit/service/test_smoke.py tests/integration/test_first_run_smoke.py`
  passed.
- Focused format check:
  `uv run --python 3.12 --extra dev ruff format --check scripts/first_run_smoke.py tests/unit/scripts/test_first_run_smoke.py tests/unit/service/test_smoke.py tests/integration/test_first_run_smoke.py`
  passed.

Full AWF/GitHub validation was not run in the agent phase; AWF owns broad
validation, provenance, logs, and merge gating after completion.

## CI Repair Iteration: PR #394 Direct Workspace Idempotency

### Requirement Status

- Complete: Preserved production disk-admission behavior and did not change the
  dedicated disk-pressure tests.
- Complete: The direct v1 idempotency replay/conflict test now uses
  `Settings(_env_file=None, min_free_disk_bytes=0)` so it does not depend on
  real host free space after earlier shard tests.
- Complete: Added explicit `WorkspaceAcceptedResponse` assertions before the
  replay ID comparison, so future admission/preflight JSON responses fail at
  the response-shape assertion.
- Complete: Only focused repro and lint commands were run locally.

### Evidence

- AWF-provided focused repro before the fix:
  `uv run --python 3.12 coverage run --parallel-mode -m pytest tests/unit/api/test_workspaces_parts/test_workspaces_part_003.py::TestCreateWorkspacePart002::test_direct_v1_create_replays_same_payload_and_rejects_conflict -q`
  passed with `1 passed`, showing the failure was not reproducible in a clean
  single-node run.
- Focused containing-file repro before the fix:
  `uv run --python 3.12 coverage run --parallel-mode -m pytest tests/unit/api/test_workspaces_parts/test_workspaces_part_003.py -q`
  passed with `18 passed`.
- Confirmed CI-shaped pre-fix repro:
  `AWF_MIN_FREE_DISK_BYTES=999999999999999 uv run --python 3.12 coverage run --parallel-mode -m pytest tests/unit/api/test_workspaces_parts/test_workspaces_part_003.py::TestCreateWorkspacePart002::test_direct_v1_create_replays_same_payload_and_rejects_conflict -q`
  failed with `AttributeError: 'JSONResponse' object has no attribute
  'workspace_id'`, matching the GitHub Actions failure.
- Post-fix forced-threshold repro:
  `AWF_MIN_FREE_DISK_BYTES=999999999999999 uv run --python 3.12 coverage run --parallel-mode -m pytest tests/unit/api/test_workspaces_parts/test_workspaces_part_003.py::TestCreateWorkspacePart002::test_direct_v1_create_replays_same_payload_and_rejects_conflict -q`
  passed with `1 passed`.
- Post-fix containing-file repro:
  `uv run --python 3.12 coverage run --parallel-mode -m pytest tests/unit/api/test_workspaces_parts/test_workspaces_part_003.py -q`
  passed with `18 passed`.
- File-scoped lint:
  `uv run --python 3.12 --extra dev ruff check tests/unit/api/test_workspaces_parts/test_workspaces_part_003.py`
  passed.

Full AWF/GitHub validation was not run in the agent phase; AWF owns broad
validation, provenance, logs, and merge gating after completion.

## Review Repair Iteration: PRRT_kwDOSJAM6s6HCWUe

### Requirement Status

- Complete: Added focused regression coverage in
  `tests/unit/scripts/test_first_run_smoke.py` for duplicate `--lane`
  arguments.
- Complete: `scripts/first_run_smoke.py::_parse_args` now deduplicates parsed
  lanes while preserving first-seen order.
- Complete: `--lane` remains repeatable for selecting multiple different lanes.
- Complete: Broad AWF/GitHub validation was not run in the agent phase.

### Evidence

- Confirmed pre-fix focused regression:
  `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_first_run_smoke.py::test_parse_args_deduplicates_repeat_lanes_in_order -q`
  failed because the parsed lane tuple retained the duplicate source lane.
- Post-fix focused command:
  `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_first_run_smoke.py::test_parse_args_deduplicates_repeat_lanes_in_order tests/unit/scripts/test_first_run_smoke.py::test_copy_source_checkout_preserves_markers_and_excludes_dev_state -q`
  passed with `2 passed`.
- File-scoped lint:
  `uv run --python 3.12 --extra dev ruff check scripts/first_run_smoke.py tests/unit/scripts/test_first_run_smoke.py`
  passed.
- File-scoped format check:
  `uv run --python 3.12 --extra dev ruff format --check scripts/first_run_smoke.py tests/unit/scripts/test_first_run_smoke.py`
  passed.

Full AWF/GitHub validation was not run in the agent phase; AWF owns broad
validation, provenance, logs, and merge gating after completion.

## Review Repair Iteration: issue 4620148180

### Requirement Status

- Complete: `run_tool_install_lane` now uses `_run_source_command_sequence` for
  installed post-install command probes, matching the source lanes'
  first-failure stop behavior.
- Complete: Preserved the source-checkout proof behavior where setup dry-run
  return code `1` can pass only after parseable JSON identifies the selected
  checkout and no source-checkout reason codes are present.
- Complete: Added an inline comment explaining that ordinary host-readiness
  blockers can make setup dry-run exit `1` even when source-checkout selection
  is correct.
- Complete: Added focused unit coverage for the fail-fast regression and the
  non-source readiness blocker exit-code behavior.

### Evidence

- Confirmed pre-fix focused regression:
  `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_first_run_smoke.py::test_tool_install_lane_stops_after_first_post_install_failure -q`
  failed because the tool-install lane returned one passed install result plus
  four failed post-install command results.
- Post-fix review-specific tests:
  `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_first_run_smoke.py::test_tool_install_lane_stops_after_first_post_install_failure tests/unit/scripts/test_first_run_smoke.py::test_source_setup_result_accepts_non_source_readiness_blocker_exit_one -q`
  passed with `2 passed`.
- Focused module test:
  `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_first_run_smoke.py -q`
  passed with `14 passed`.
- File-scoped lint:
  `uv run --python 3.12 --extra dev ruff check scripts/first_run_smoke.py tests/unit/scripts/test_first_run_smoke.py`
  passed.

Full AWF/GitHub validation was not run in the agent phase; AWF owns broad
validation, provenance, logs, and merge gating after completion.

## CI Repair Iteration: Supported Script Surface

### Requirement Status

- Complete: Preserved the script-surface guard in
  `tests/unit/docs/test_api_surface_cleanup_docs.py`; the test still asserts
  exact membership for files in `scripts/`.
- Complete: Added `first_run_smoke.py` to `SUPPORTED_SCRIPTS` because the T14
  first-run smoke harness is an intentional supported entrypoint.
- Complete: Re-ran the AWF-provided focused pytest command covering the
  failing script-surface node and both reported source smoke lane nodes.

### Evidence

- Confirmed pre-fix focused repro:
  `uv run --python 3.12 --extra dev pytest tests/unit/docs/test_api_surface_cleanup_docs.py::test_scripts_directory_contains_only_supported_generators tests/integration/test_first_run_smoke.py::test_source_uv_run_lane_proves_checkout_from_outside tests/integration/test_first_run_smoke.py::test_source_tool_install_lane_installs_isolated_awf -q`
  failed with `1 failed, 2 passed`; the failure was the extra
  `first_run_smoke.py` file in the script-surface allowlist assertion.
- Post-fix focused repro:
  `uv run --python 3.12 --extra dev pytest tests/unit/docs/test_api_surface_cleanup_docs.py::test_scripts_directory_contains_only_supported_generators tests/integration/test_first_run_smoke.py::test_source_uv_run_lane_proves_checkout_from_outside tests/integration/test_first_run_smoke.py::test_source_tool_install_lane_installs_isolated_awf -q`
  passed with `3 passed`.
- File-scoped lint:
  `uv run --python 3.12 --extra dev ruff check tests/unit/docs/test_api_surface_cleanup_docs.py`
  passed.

Full AWF/GitHub validation was not run in the agent phase; AWF owns broad
validation, provenance, logs, and merge gating after completion.

## Review Repair Iteration: PRRT_kwDOSJAM6s6HCQeo

### Requirement Status

- Complete: Added focused regression coverage in
  `tests/unit/scripts/test_first_run_smoke.py` for environmental failures in
  source `uv run` probes.
- Complete: Added focused regression coverage for environmental failures in
  installed `awf` post-install command probes.
- Complete: `scripts/first_run_smoke.py` now classifies recognized network or
  offline failures from non-zero source/post-install probes as `skipped`.
- Complete: Preserved hard-failure behavior for ordinary non-environmental
  probe failures and source-checkout reason-code failures; existing focused
  tests still cover those paths.

### Evidence

- Confirmed pre-fix focused regressions:
  `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_first_run_smoke.py::test_source_command_result_skips_environmental_dependency_failures tests/unit/scripts/test_first_run_smoke.py::test_source_setup_result_skips_unparseable_environmental_failure -q`
  failed with status assertions; the classifier returned `failed` instead of
  `skipped`.
- Post-fix review-specific tests:
  `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_first_run_smoke.py::test_source_command_result_skips_environmental_dependency_failures tests/unit/scripts/test_first_run_smoke.py::test_source_setup_result_skips_unparseable_environmental_failure tests/unit/scripts/test_first_run_smoke.py::test_tool_install_lane_stops_after_first_post_install_failure tests/unit/scripts/test_first_run_smoke.py::test_source_setup_result_accepts_non_source_readiness_blocker_exit_one -q`
  passed with `6 passed`.
- Focused module test:
  `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_first_run_smoke.py -q`
  passed with `18 passed`.
- File-scoped lint:
  `uv run --python 3.12 --extra dev ruff check scripts/first_run_smoke.py tests/unit/scripts/test_first_run_smoke.py`
  passed.
- File-scoped format check:
  `uv run --python 3.12 --extra dev ruff format --check scripts/first_run_smoke.py tests/unit/scripts/test_first_run_smoke.py`
  passed.

Full AWF/GitHub validation was not run in the agent phase; AWF owns broad
validation, provenance, logs, and merge gating after completion.

## Review Repair Iteration: PRRT_kwDOSJAM6s6HCi01

### Requirement Status

- Complete: Added focused regression coverage in
  `tests/unit/scripts/test_first_run_smoke.py` showing an early environmental
  skip still allows the setup dry-run JSON proof command to run.
- Complete: `_run_source_command_sequence` now stops only on hard `failed`
  results, preserving reported `skipped` results while continuing to the
  source-checkout proof attempt.
- Complete: Preserved fail-fast behavior for hard failures; the existing
  post-install failure regression still passes.
- Complete: Environmental skip classification remains unchanged.

### Evidence

- Confirmed pre-fix focused regression:
  `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_first_run_smoke.py::test_source_command_sequence_runs_setup_proof_after_environmental_skip -q`
  failed because the setup dry-run JSON command was never invoked after the
  early environmental skip.
- Post-fix focused command:
  `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_first_run_smoke.py::test_source_command_sequence_runs_setup_proof_after_environmental_skip tests/unit/scripts/test_first_run_smoke.py::test_tool_install_lane_stops_after_first_post_install_failure -q`
  passed with `2 passed`.
- File-scoped lint:
  `uv run --python 3.12 --extra dev ruff check scripts/first_run_smoke.py tests/unit/scripts/test_first_run_smoke.py`
  passed.
- File-scoped format check:
  `uv run --python 3.12 --extra dev ruff format --check scripts/first_run_smoke.py tests/unit/scripts/test_first_run_smoke.py`
  passed.

Full AWF/GitHub validation was not run in the agent phase; AWF owns broad
validation, provenance, logs, and merge gating after completion.
