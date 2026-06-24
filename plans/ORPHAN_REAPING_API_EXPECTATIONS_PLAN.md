# Orphan Reaping API Expectations Plan

## Problem Statement and Scope

PR review thread `PRRT_kwDOSJAM6s6L7dMw` reports that the orphan resource
summary now returns `ORPHANS_PRESENT_REAPING_ENABLED` when
`auto_cleanup_orphans=True`, but reaping-enabled API tests still expect the old
`ORPHAN_RESOURCES_PRESENT` reason. Scope is limited to verifying that claim and
updating the affected expectations if the source behavior is intentional.

## Requirements Checklist

- Verify the source branch in `src/awf/service/orphan_resources.py` returns the
  reaping-enabled reason when orphan resources are present and cleanup is
  enabled.
- Preserve existing cleanup readiness assertions that prove reaping is enabled
  and not dry-run-only.
- Update only focused regression tests that still assert the old reason for the
  reaping-enabled branch.
- Run targeted tests for the changed expectations only; leave broad AWF/GitHub
  validation to the AWF post-agent phase.
- Commit the scoped fix locally.

## Implementation Steps

1. Inspect the source branch and cited tests.
2. Run the cited tests before editing to confirm the reviewer-reported failure.
3. Change the stale expected reason strings to
   `ORPHANS_PRESENT_REAPING_ENABLED`.
4. Run the same targeted tests again.
5. Record validation evidence and commit the changed files.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_health_parts/test_health_part_002.py::test_readyz_orphan_resources_reflect_auto_cleanup_enabled tests/unit/api/test_metrics_capacity.py::test_default_orphan_resource_summary_propagates_auto_cleanup_setting -q`

Pass criteria: the targeted tests pass after the expectation update. Full
suite, coverage, and CI-equivalent validation are intentionally not run in the
agent phase because AWF owns broad validation after completion.
