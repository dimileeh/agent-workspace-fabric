# PRRT_kwDOSJAM6s6CPIt5 Local Coverage Gate Plan

## Problem Statement

Review thread `PRRT_kwDOSJAM6s6CPIt5` points out that the AWF self-profile
currently enables `validation.strategy.final_gate: coverage` with a 99%
workspace-local coverage command, while the validation artifact for the same
change records that local Docker-unavailable runs reach only 98.82%. That makes
otherwise valid local AWF work fail before PR monitoring can rely on GitHub
Actions as the authoritative full-coverage gate.

## Requirements Checklist

- Keep targeted edit validation local and non-blocking for final coverage.
- Preserve the documented 99% coverage target and coverage command for CI/full
  gate visibility.
- Preserve generic support for explicit profile opt-in local coverage final
  gates.
- Add/update regression coverage proving the AWF self-profile does not trigger
  local final coverage in the documented Docker-unavailable workspace context.
- Update validation/conformance notes so they no longer claim the self-profile
  enforces a local 99% final coverage gate.

## Implementation Steps

1. Update the AWF self-profile regression test first so it expects
   `final_gate: none` while keeping `baseline_coverage: skip`,
   `edit_gate: targeted`, `minimum_percent: 99`, and `parallel_workers: 3`.
2. Confirm that updated test fails against the current profile.
3. Change `.awf/workspace.yml` to disable the local final coverage gate without
   lowering the coverage target or changing the command.
4. Update affected validation/conformance documentation for this review-thread
   iteration.
5. Run focused profile tests and any narrow executor/profile tests needed to
   prove generic explicit local coverage support still behaves as intended.

## Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/profiles/test_profiles.py::test_awf_self_profile_keeps_final_coverage_non_blocking_locally -q
uv run --python 3.12 --extra dev pytest tests/unit/profiles/test_profiles.py::test_awf_self_profile_keeps_final_coverage_non_blocking_locally tests/unit/control/test_executor_coverage_edges.py::test_local_coverage_runs_only_for_explicit_final_gate_with_coverage_command tests/unit/control/test_executor_coverage_edges.py::test_validation_command_records_omit_coverage_without_local_final_gate tests/unit/control/test_executor_coverage_edges.py::test_validation_command_count_ignores_coverage_without_local_final_gate -q
```

Pass criteria: the AWF self-profile resolves with no local final coverage gate,
the 99% coverage target remains declared, and generic explicit local coverage
gate tests continue to pass.
