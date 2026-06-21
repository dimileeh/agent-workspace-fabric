# PRRT_kwDOSJAM6s6KxBgI Plan

## Problem Statement and Scope

Review thread `PRRT_kwDOSJAM6s6KxBgI` reports that post-validation conformance report cleanup failures are returned as `PLAN_CONFORMANCE_UNSATISFIED`, allowing `run_validation_and_fix_cycle` to treat AWF/git cleanup residue as an agent-correctable plan gap and launch a post-validation conformance fix pass.

Scope is limited to distinguishing report cleanup failures from ordinary plan conformance gaps and ensuring the validation loop fails immediately for that cleanup failure.

## Requirements Checklist

- Verify whether `_build_report_cleanup_failure` uses the same reason code as real conformance gaps.
- Introduce a cleanup-specific failure reason for report-path cleanup residue.
- Ensure `run_validation_and_fix_cycle` does not launch an agent conformance fix pass for this cleanup-specific reason, even when conformance iterations remain.
- Preserve ordinary `PLAN_CONFORMANCE_UNSATISFIED` fix-pass behavior.
- Add focused regression coverage for the terminal cleanup-failure path.

## Implementation Steps

1. Add a dedicated post-validation conformance report cleanup reason code.
2. Return that reason code from `_build_report_cleanup_failure`.
3. Update `run_validation_and_fix_cycle` to treat that reason as terminal and classify it as infrastructure failure.
4. Update existing focused expectations and add a regression test that proves no agent fix pass is launched for cleanup failure.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_002.py::test_satisfied_post_validation_conformance_report_fails_when_unlink_leaves_dirty_index tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_009.py::test_post_validation_conformance_report_cleanup_failure_skips_fix_pass -q`
  - Passes with both targeted regression tests green.

Full AWF/GitHub validation is intentionally not run during this agent phase; AWF owns broad validation after completion.
