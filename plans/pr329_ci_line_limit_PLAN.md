# PR 329 CI Line Limit Plan

## Problem Statement and Scope

PR #329 fails the CI Python coverage job because the maintainability guardrail
detects `tests/unit/runtime/test_pr_monitor_operator_hints.py` at 1522 lines,
above the 1500-line first-party file limit.

Scope is limited to decomposing the oversized test file without changing the
line-limit guardrail, skipping tests, or altering product behavior.

## Requirements Checklist

- Keep all first-party code files at or below the existing 1500-line limit.
- Preserve the operator hint test coverage by moving tests rather than deleting
  or weakening assertions.
- Do not edit protected workflow, quality-gate, or configuration files.
- Run only focused local verification; AWF/GitHub owns full CI and coverage
  validation after this agent phase.
- Commit the focused CI fix locally on the current AWF-managed branch.

## Implementation Steps

1. Move a semantically distinct monitor-state persistence test from
   `tests/unit/runtime/test_pr_monitor_operator_hints.py` into a new focused
   runtime test module.
2. Trim imports in the original file and add the needed imports/fixture to the
   new test module.
3. Re-run the reported line-limit repro and the affected runtime test modules.
4. Record validation evidence in `plans/pr329_ci_line_limit_VALIDATION.md`.
5. Commit the local fix with a conventional commit message.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  - Passes with no oversized first-party files.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py tests/unit/runtime/test_pr_monitor_operator_hint_state.py -q`
  - Passes, proving the split preserved the affected tests.

Full AWF/GitHub validation is intentionally not run locally per the workspace
contract.

## Assumptions/Changes

- After inspecting the oversized file, the concurrent operator-hint freeze
  persistence case was the smallest coherent split that leaves the original
  module comfortably under the 1500-line guardrail while preserving coverage.
