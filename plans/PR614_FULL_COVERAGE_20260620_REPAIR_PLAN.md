# PR614 Full Coverage 2026-06-20 Repair Plan

## Problem Statement and Scope

PR #614 CI fails only in `python-full-coverage`: the combined GitHub Actions coverage report is 98.97%, below the required 99.00%. The `ci-required` failure cascades from that failed required job.

Scope is limited to meaningful tests for existing uncovered behavior. Do not edit workflow or coverage threshold configuration, and do not run the broad AWF-owned coverage suite locally.

## Requirements Checklist

- Diagnose the coverage failure from CI evidence or artifacts before editing.
- Add focused behavior assertions for live uncovered paths.
- Do not disable, skip, weaken, or reconfigure the coverage gate.
- Run only narrow local checks for the touched test files.
- Record validation evidence and note that full AWF/GitHub coverage validation is owned by AWF after agent completion.
- Commit the local fix on the current AWF-managed branch without pushing.

## Implementation Steps

1. Use the CI `full-coverage-report` artifact to identify real missing source paths.
2. Add tests for host-port advisory lock no-op and collision behavior in `workspace_repo_host_ports`.
3. Add tests for setup env-migration payload validation in `setup_commands`.
4. Add a small provider-recovery capacity fallback edge test if the first two areas do not provide enough margin over the exact threshold.
5. Keep assertions behavior-focused: returned payload identity/details, advisory lock SQL execution behavior, and provider recovery decisions.
6. Update this plan only if the selected targets cannot be verified narrowly.

## Assumptions/Changes

- Focused coverage evidence showed the setup and lock tests execute the intended reported gaps but are close to the 23-opportunity CI deficit, so provider-recovery capacity fallback edge coverage is included for margin.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_host_port_admission_locks.py tests/unit/cli/test_setup_commands_client.py -q`
  - Passes with all selected focused tests green.
- Full sharded coverage and required CI status are not run locally; AWF/GitHub CI owns that validation after this agent completes.
