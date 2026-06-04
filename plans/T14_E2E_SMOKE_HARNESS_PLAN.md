# T14 E2E Smoke Harness Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6G__Cc` reports that
`scripts/first_run_smoke.py` passes a timeout to `subprocess.run` but lets
`subprocess.TimeoutExpired` escape. The fix is scoped to reporting smoke command
timeouts as ordinary failed command results so the harness can continue
formatting diagnostics instead of crashing.

## Requirements Checklist

- Add a focused regression test for `run_command` timeout handling.
- Return a `subprocess.CompletedProcess[str]` with non-zero timeout status when
  `subprocess.run` raises `TimeoutExpired`.
- Preserve captured stdout/stderr, including byte output from timeout
  exceptions, and include a clear timeout message in stderr.
- Keep the change limited to the first-run smoke harness and its focused tests.

## Implementation Steps

1. Add a unit test in `tests/unit/scripts/test_first_run_smoke.py` that
   monkeypatches `subprocess.run` to raise `TimeoutExpired`.
2. Run that single test and confirm it fails before the code change.
3. Catch `TimeoutExpired` in `scripts/first_run_smoke.py::run_command`, convert
   any captured output to text, and return code `124`.
4. Re-run the focused first-run smoke unit tests touched by this change.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_first_run_smoke.py -q`
  must pass after implementation.
- Full AWF/GitHub validation is intentionally not run in the agent phase; AWF
  owns broad validation, provenance, logs, and merge gating after completion.

## CI Repair Iteration: Supported Script Surface

### Problem Statement And Scope

PR #394 CI reports that the docs/API surface cleanup test fails because
`scripts/first_run_smoke.py` now exists in `scripts/` but the supported script
allowlist still only names the older generator and release helper scripts. The
source smoke lanes reported in CI pass in this workspace with the focused repro,
so this repair is scoped to keeping the script-surface guard aligned with the
new first-run smoke harness.

### Requirements Checklist

- Preserve the script-surface guard; do not skip or loosen the test.
- Add `first_run_smoke.py` to the supported script surface because it is an
  intentional T14 smoke harness entrypoint.
- Re-run the focused CI repro command provided by AWF and record that broad
  AWF/GitHub validation remains deferred to AWF.

### Implementation Steps

1. Update `tests/unit/docs/test_api_surface_cleanup_docs.py` so
   `SUPPORTED_SCRIPTS` includes `first_run_smoke.py`.
2. Re-run the AWF-provided focused pytest command covering the script-surface
   guard and both first-run source smoke lanes.
3. Update `plans/T14_E2E_SMOKE_HARNESS_VALIDATION.md` with requirement status
   and focused evidence.

### Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/docs/test_api_surface_cleanup_docs.py::test_scripts_directory_contains_only_supported_generators tests/integration/test_first_run_smoke.py::test_source_uv_run_lane_proves_checkout_from_outside tests/integration/test_first_run_smoke.py::test_source_tool_install_lane_installs_isolated_awf -q`
  must pass after implementation.
- Full AWF/GitHub validation is intentionally not run in the agent phase; AWF
  owns broad validation, provenance, logs, and merge gating after completion.

## Review Repair Iteration: issue 4620148180

### Problem Statement And Scope

Review-level comment `issue:4620148180` flags two clarity issues in
`scripts/first_run_smoke.py`: the tool-install post-install command loop keeps
running after the first installed-command failure, and the setup dry-run result
accepts exit code `1` without documenting that ordinary readiness blockers are
outside the source-checkout proof this smoke lane is meant to perform.

### Requirements Checklist

- Make `run_tool_install_lane` fail fast on post-install command failures, with
  behavior matching the source install lanes.
- Preserve the intentional source-checkout proof behavior where setup dry-run
  exit code `1` can pass only after JSON parsing proves the selected checkout
  and no source-checkout reason codes are present.
- Document why exit code `1` is parsed instead of immediately failing.
- Add focused unit coverage for the fail-fast regression and the documented
  setup dry-run exit-code behavior.

### Implementation Steps

1. Add a unit test that simulates a successful wheel build and tool install,
   then a failing installed `awf --help`, and asserts no later installed
   commands run.
2. Add a focused unit test documenting that setup dry-run return code `1` is
   acceptable for non-source readiness blockers when the JSON payload identifies
   the selected checkout.
3. Reuse `_run_source_command_sequence` in `run_tool_install_lane` for
   post-install commands.
4. Add a short comment above the setup dry-run return-code allowance.

### Verification Commands And Pass Criteria

- Pre-fix targeted regression:
  `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_first_run_smoke.py::test_tool_install_lane_stops_after_first_post_install_failure -q`
  should fail before implementation.
- Post-fix focused command:
  `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_first_run_smoke.py::test_tool_install_lane_stops_after_first_post_install_failure tests/unit/scripts/test_first_run_smoke.py::test_source_setup_result_accepts_non_source_readiness_blocker_exit_one -q`
  must pass.
- Full AWF/GitHub validation is intentionally not run in the agent phase; AWF
  owns broad validation, provenance, logs, and merge gating after completion.

## Review Repair Iteration: PRRT_kwDOSJAM6s6HCQeo

### Problem Statement And Scope

Inline review thread `PRRT_kwDOSJAM6s6HCQeo` reports that source-lane probes
and post-install `awf` command probes classify offline dependency resolution
failures as hard failures through `_basic_result`, while tool-install setup
already treats the same environmental failures as skips. The fix is scoped to
the first-run smoke result classifiers so offline/unreachable indexes do not
produce false harness failures, while non-environmental command regressions and
source-checkout validation failures continue to fail.

### Requirements Checklist

- Add focused regression coverage for environmental source command failures.
- Add focused regression coverage for environmental installed `awf` probe
  failures.
- Treat non-zero source/post-install probe results with recognized network or
  offline signatures as `skipped`.
- Preserve hard failures for ordinary non-environmental probe failures and
  source-checkout reason-code failures.

### Implementation Steps

1. Add unit tests in `tests/unit/scripts/test_first_run_smoke.py` for
   environmental failures in non-setup source/post-install probes and setup
   dry-run probes that fail before emitting JSON.
2. Run the focused new tests and confirm they fail before implementation.
3. Update `scripts/first_run_smoke.py` result classification to reuse the
   environmental failure signatures for source/post-install probes.
4. Re-run the focused smoke harness unit tests touched by this repair and
   file-scoped lint.

### Verification Commands And Pass Criteria

- Pre-fix targeted regressions:
  `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_first_run_smoke.py::test_source_command_result_skips_environmental_dependency_failures tests/unit/scripts/test_first_run_smoke.py::test_source_setup_result_skips_unparseable_environmental_failure -q`
  should fail before implementation.
- Post-fix focused command:
  `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_first_run_smoke.py::test_source_command_result_skips_environmental_dependency_failures tests/unit/scripts/test_first_run_smoke.py::test_source_setup_result_skips_unparseable_environmental_failure tests/unit/scripts/test_first_run_smoke.py::test_tool_install_lane_stops_after_first_post_install_failure tests/unit/scripts/test_first_run_smoke.py::test_source_setup_result_accepts_non_source_readiness_blocker_exit_one -q`
  must pass.
- Full AWF/GitHub validation is intentionally not run in the agent phase; AWF
  owns broad validation, provenance, logs, and merge gating after completion.

## Review Repair Iteration: PRRT_kwDOSJAM6s6HCWUe

### Problem Statement And Scope

Inline review thread `PRRT_kwDOSJAM6s6HCWUe` reports that passing the same
source lane more than once on the CLI can derive the same lane root twice. The
first source checkout copy succeeds, while the second hits `FileExistsError` and
crashes the harness. The fix is scoped to preserving repeatable lane selection
while making duplicate lane arguments idempotent before execution.

### Requirements Checklist

- Add focused regression coverage for duplicate `--lane` arguments.
- Deduplicate parsed lanes while preserving the caller's first-seen order.
- Keep `--lane` repeatable for selecting multiple different lanes.
- Avoid broad validation in the agent phase; AWF owns full validation after
  completion.

### Implementation Steps

1. Add a unit test for `_parse_args` showing duplicate lane arguments collapse
   to one lane in first-seen order.
2. Run the focused test and confirm it fails before implementation.
3. Deduplicate parsed lanes in `scripts/first_run_smoke.py::_parse_args`.
4. Re-run the focused new test and a small smoke-harness unit subset.

### Verification Commands And Pass Criteria

- Pre-fix targeted regression:
  `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_first_run_smoke.py::test_parse_args_deduplicates_repeat_lanes_in_order -q`
  should fail before implementation.
- Post-fix focused command:
  `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_first_run_smoke.py::test_parse_args_deduplicates_repeat_lanes_in_order tests/unit/scripts/test_first_run_smoke.py::test_copy_source_checkout_preserves_markers_and_excludes_dev_state -q`
  must pass.
- Full AWF/GitHub validation is intentionally not run in the agent phase; AWF
  owns broad validation, provenance, logs, and merge gating after completion.

## Review Repair Iteration: PRRT_kwDOSJAM6s6HCi01

### Problem Statement And Scope

Inline review thread `PRRT_kwDOSJAM6s6HCi01` reports that an environmental
skip from an early source-lane command probe stops `_run_source_command_sequence`
before the final setup dry-run JSON command can prove the selected
`--source-checkout`. The fix is scoped to the source/post-install command
sequence control flow in `scripts/first_run_smoke.py`.

### Requirements Checklist

- Add focused regression coverage showing an early environmental skip still
  allows the setup dry-run JSON proof command to run.
- Preserve fail-fast behavior for hard command failures.
- Keep environmental skip classification unchanged.
- Avoid broad AWF/GitHub validation in the agent phase; AWF owns full
  validation after completion.

### Implementation Steps

1. Add a unit test in `tests/unit/scripts/test_first_run_smoke.py` for
   `_run_source_command_sequence` continuing after an environmental skip and
   reaching a passing setup dry-run JSON command.
2. Run the focused new test and confirm it fails before implementation.
3. Update `_run_source_command_sequence` to stop only on hard failures.
4. Re-run the focused new test plus the existing post-install fail-fast
   regression.

### Verification Commands And Pass Criteria

- Pre-fix targeted regression:
  `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_first_run_smoke.py::test_source_command_sequence_runs_setup_proof_after_environmental_skip -q`
  should fail before implementation.
- Post-fix focused command:
  `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_first_run_smoke.py::test_source_command_sequence_runs_setup_proof_after_environmental_skip tests/unit/scripts/test_first_run_smoke.py::test_tool_install_lane_stops_after_first_post_install_failure -q`
  must pass.
- Full AWF/GitHub validation is intentionally not run in the agent phase; AWF
  owns broad validation, provenance, logs, and merge gating after completion.
