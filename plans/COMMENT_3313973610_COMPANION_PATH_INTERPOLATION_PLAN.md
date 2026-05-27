# Comment 3313973610 Companion Path Interpolation Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6FPCdN` reports that companion-supplied path
strings accept Docker Compose interpolation syntax such as `${GITHUB_TOKEN}`.
These values are later rendered into Compose, so the public request schema
should reject interpolation in companion path fields before provisioning.

Scope is limited to `WorkspaceCompanionRequest` validation and focused schema
regression coverage for companion path inputs.

## Requirements Checklist

- Reject Docker Compose interpolation in companion `build_context`, `dockerfile`,
  and `env_file` repo-relative paths.
- Reject Docker Compose interpolation in companion volume source paths.
- Reject Docker Compose interpolation in companion volume target paths.
- Preserve existing rejection of unsafe YAML path characters and invalid path
  shape.
- Keep validation evidence narrow; AWF/GitHub owns full validation after this
  agent phase.

## Implementation Steps

1. Add focused regression cases to the companion schema public contract tests.
2. Confirm the new regression fails against the current validator.
3. Update the shared companion path validator to reject Compose interpolation.
4. Run the focused schema test file.
5. Record validation evidence in a matching validation document.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_schema_coverage_edges.py -q`
  - Passes after implementation.
  - Fails before implementation on the new interpolation path regression.

Full AWF/GitHub validation is intentionally not run inside this workspace
agent phase per the workspace contract.

## Assumptions/Changes

- The validator rejects dollar characters in companion path strings rather than
  trying to escape them during Compose rendering. This follows the review
  thread's request to reject or escape `$` in companion-supplied paths and keeps
  the schema contract simple.
