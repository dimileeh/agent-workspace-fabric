# PR329 CI Name-Only Z Line Limit Plan

## Problem Statement and Scope

PR #329 CI fails in the Python full-coverage job because:

- `_changed_paths_from_name_only_z` accepts malformed `git diff --name-only -z`
  output instead of failing closed.
- `src/awf/runtime/pr_monitor_runner/helpers.py` exceeds the first-party
  maintainability line limit of 1,500 lines.

Scope is limited to the PR monitor runner helper surface needed to restore the
focused failing checks. This plan does not weaken tests, coverage, or
maintainability gates.

## Requirements Checklist

- Preserve valid NUL-delimited `--name-only -z` parsing and de-duplication.
- Reject newline-delimited output that is not NUL-delimited.
- Reject truncated NUL output missing the final terminator.
- Reject empty path records.
- Bring `helpers.py` to 1,500 lines or fewer without changing public helper
  compatibility names.
- Run only focused local checks; AWF/GitHub own broad validation after agent
  completion.

## Implementation Steps

1. Route the `helpers.py` compatibility export for
   `_changed_paths_from_name_only_z` to the existing strict implementation in
   `src/awf/runtime/pr_monitor_runner/path_helpers.py`, which validates the
   malformed cases already covered by the tests.
2. Reduce `src/awf/runtime/pr_monitor_runner/helpers.py` line count with a
   scoped import-format cleanup that preserves the re-exported helper names.
3. Re-run the focused failing pytest nodes.
4. Run focused lint for the touched PR monitor runner files.
5. Record evidence and requirement status in the validation document.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest '<failing-node-1>' '<failing-node-2>' tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  - Passes with all selected tests green.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/path_helpers.py src/awf/runtime/pr_monitor_runner/helpers.py`
  - Passes with no lint errors.
- `wc -l src/awf/runtime/pr_monitor_runner/helpers.py`
  - Reports 1,500 lines or fewer.

## Assumptions/Changes

- `src/awf/runtime/pr_monitor_runner/path_parsing.py` is root-owned in this
  workspace and cannot be edited by the agent user. The effective runtime and
  test surface imports `_changed_paths_from_name_only_z` through
  `helpers.py`; no source or test file imports `path_parsing.py` directly.
  The fix therefore updates `helpers.py` to re-export the existing strict
  parser from `path_helpers.py`.
