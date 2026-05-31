# COMMENT 4585873052 Review Summary Plan

## Problem Statement and Scope

Review-level comment `issue:4585873052` includes two follow-ups for the
pre-commit autofix retry work:

- `git status --porcelain` rename parsing currently splits on the first
  literal ` -> `, which can occur inside Git C-quoted path names.
- The deterministic hook allowlist intentionally matches exact pre-commit hook
  IDs, but the module does not document that custom wrapper IDs must be
  explicitly opted in.

Scope is limited to the PR monitor porcelain rename parsing helper, the commit
autofix restage path parser, focused regression coverage, and the doc comment.

## Requirements Checklist

- Add regression coverage for a quoted rename path whose old path contains the
  literal ` -> ` separator text.
- Ensure `_changed_paths_from_porcelain` returns the old and new rename paths
  correctly for that quoted case.
- Ensure `_retry_monitor_precommit_autofix_commit_once` restages the rename
  destination rather than skipping the retry when the old path is quoted and
  contains ` -> `.
- Keep the pre-commit hook allowlist exact-match behavior unchanged, and add a
  note explaining that custom deterministic wrapper hook IDs opt in by adding
  their exact ID to the allowlist.
- Run targeted local validation only; full AWF/GitHub validation remains owned
  by AWF after agent completion.

## Implementation Steps

1. Add focused failing tests for quoted rename parsing in the helper coverage
   test and commit autofix unit test.
2. Add a shared porcelain rename splitter that scans for ` -> ` outside Git
   C-quoted path segments.
3. Reuse that splitter from `_changed_paths_from_porcelain` and
   `_worktree_modified_paths_from_porcelain`.
4. Add the allowlist extension-point note without widening the allowlist.
5. Run focused pytest for the new tests and the affected commit-autofix test
   file, plus focused ruff on touched source/tests.

## Verification Commands and Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_007.py::test_git_push_and_porcelain_helpers_cover_clean_rename_and_invalid_lines tests/unit/runtime/test_pr_monitor_commit_autofix.py::test_monitor_precommit_autofix_retry_restages_quoted_rename_destination_with_arrow_in_old_path -q
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_commit_autofix.py -q
uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/helpers.py src/awf/runtime/pr_monitor_runner/commit_autofix.py src/awf/runtime/pr_monitor_runner/precommit_autofix.py tests/unit/runtime/test_pr_monitor_commit_autofix.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_007.py
```

All focused commands must pass. Do not run full coverage, whole-repository unit
suites, or CI-equivalent validation in this workspace phase.
