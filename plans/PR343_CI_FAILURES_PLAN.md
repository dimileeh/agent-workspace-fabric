# PR #343 CI Failure Repair Plan

## Problem Statement And Scope

GitHub CI for PR #343 fails in the Python full-coverage job on three focused
unit tests. The local focused repro confirms:

- `/readyz` contract tests still expect the pre-Grok provider readiness set.
- service status provider readiness tests still expect the pre-Grok provider
  readiness set.
- the maintainability guard finds two first-party files over the 1,500-line
  limit after Grok provider readiness/test coverage was added.

Scope is limited to restoring those focused tests without weakening readiness
coverage or maintainability gates.

## Requirements Checklist

- [x] Keep AWF's current branch and do not push.
- [x] Update health/status readiness contract expectations to include Grok.
- [x] Keep Grok provider readiness behavior intact.
- [x] Split first-party code/test files so each stays under 1,500 lines.
- [x] Run the provided focused pytest repro after the fix.
- [x] Record focused validation evidence only; AWF/GitHub own broad validation.

## Implementation Steps

1. Update the stale readiness provider expectations in the failing health and
   service status tests.
2. Move a small group of provider-readiness support helpers out of
   `provider_readiness.py` into the existing helper module so the production
   file stays under the line limit.
3. Move enough provider readiness tests from part 001 into a new shard with
   local/shared fixtures so both test shards stay under the line limit.
4. Run the focused failing pytest node IDs.
5. Create `plans/PR343_CI_FAILURES_VALIDATION.md` with file and command
   evidence, then commit the repair locally.

## Verification Commands And Pass Criteria

Focused repro:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/api/test_health_parts/test_health_part_001.py::test_readyz_response_shape_matches_contract tests/unit/service/test_status_parts/test_status_part_001.py::test_service_status_provider_warnings_do_not_fail_by_default tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q
```

Pass criteria: all three focused tests pass. Broader AWF/GitHub validation is
intentionally left to AWF after agent completion per the workspace contract.
