# PRRT_kwDOSJAM6s6FL6d3 Companion Volume Plan

## Problem Statement and Scope

An unresolved PR review thread reports duplicated companion volume-source
classification between API validation and runtime compose materialization. The
scope is limited to removing that duplicate classification logic and adding a
focused regression test for the shared helper.

## Requirements Checklist

- Add one shared helper in `awf.common.companions` for classifying companion
  volume sources that should be treated as repo-relative paths.
- Update API companion schema validation to use the shared helper.
- Update node companion service runtime resolution to use the same shared
  helper.
- Preserve existing named-volume and repo-relative path behavior.
- Run focused tests only; broad AWF/GitHub validation remains managed by AWF
  after agent completion.

## Implementation Steps

1. Add a failing unit test for the shared companion volume-source classifier.
2. Implement the shared helper in `src/awf/common/companions.py`.
3. Replace duplicated inline classification in
   `src/awf/api/schemas_companions.py` and
   `src/awf/node/companion_services.py`.
4. Run the focused common/API/node tests touched by this change.
5. Record validation evidence in a matching validation document.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_companions.py tests/unit/api/test_schema_coverage_edges.py tests/unit/node/test_companion_services.py -q`
  - Passes with no failures.

Full profile validation, whole-repository test suites, and coverage gates are
intentionally not run during this agent phase; AWF/GitHub own those broad gates.
