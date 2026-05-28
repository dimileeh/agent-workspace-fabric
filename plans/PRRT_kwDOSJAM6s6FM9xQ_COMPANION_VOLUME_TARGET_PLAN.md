# PRRT_kwDOSJAM6s6FM9xQ Companion Volume Target Plan

## Problem Statement and Scope

An unresolved PR review thread reports that public companion volume requests
validate the source path but not the target path. Because companion volumes are
rendered with Docker Compose short syntax, a target such as `cache` or
`/cache:ro` can either fail later during compose handling or change the
source:target structure after companion worktrees have already been created.

Scope is limited to public companion request schema validation and focused
regression coverage for companion volume target paths.

## Requirements Checklist

- Reject companion volume targets that are not absolute POSIX container paths.
- Reject companion volume targets containing `:` so callers cannot smuggle
  short-syntax mount options into the target field.
- Preserve existing valid named-volume and repo-relative source behavior.
- Add regression coverage for the examples from the review thread.
- Run focused checks only; broad AWF/GitHub validation remains managed by AWF
  after agent completion.

## Implementation Steps

1. Add failing API schema tests for invalid companion volume targets.
2. Add a small schema helper for validating companion volume targets.
3. Apply the target helper inside the existing companion volume validator.
4. Run focused API schema tests and a focused lint check for touched files.
5. Record validation evidence in the matching validation document.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_schema_coverage_edges.py -q`
  - Passes with no failures.
- `uv run --python 3.12 --extra dev ruff check src/awf/api/schemas_companions.py tests/unit/api/test_schema_coverage_edges.py`
  - Passes with no findings.

Full repository test suites, coverage gates, frontend builds, and CI-equivalent
AWF validation are intentionally not run during this agent phase.
