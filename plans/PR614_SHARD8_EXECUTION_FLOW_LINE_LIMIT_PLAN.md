# PR614 Shard 8 Execution Flow Line Limit Plan

## Problem Statement and Scope

CI shard 8 fails only the maintainability line-limit guard because
`src/awf/control/executor/execution_flow.py` is 1504 lines, above the 1500-line
first-party file cap.

Scope is limited to reducing that module below the cap without changing runtime
behavior, CI configuration, or protected workflow files. Broad AWF/GitHub
validation remains owned by AWF after this agent phase.

## Requirements Checklist

- Preserve AWF branch ownership: do not switch branches, push, rebase, or run
  broad CI-equivalent validation.
- Reduce `execution_flow.py` to at most 1500 lines.
- Avoid behavior changes; use only formatting/documentation compaction.
- Verify with the focused maintainability test and a direct `wc -l` check.
- Record validation evidence in a matching validation document.
- Commit the scoped fix locally with a conventional commit message.

## Implementation Steps

1. Compact non-behavioral module text in `execution_flow.py` enough to satisfy
   the existing line-limit test.
2. Run the focused line-limit test from
   `tests/unit/test_core_decomposition_maintainability.py`.
3. Confirm `execution_flow.py` line count with `wc -l`.
4. Record evidence in `plans/PR614_SHARD8_EXECUTION_FLOW_LINE_LIMIT_VALIDATION.md`.
5. Commit the plan, validation, and code change locally.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  passes.
- `wc -l src/awf/control/executor/execution_flow.py` reports 1500 or fewer
  lines.
- Full AWF/GitHub validation is not run locally; AWF owns broad validation,
  provenance, timeouts, and merge gating after agent completion.
