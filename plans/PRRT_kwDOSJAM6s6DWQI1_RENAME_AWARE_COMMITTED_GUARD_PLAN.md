# PRRT_kwDOSJAM6s6DWQI1 Rename-Aware Committed Guard Plan

## Problem Statement And Scope

The executor's committed-output protected quality-gate guard currently derives
committed paths with `git diff --name-only`. For a committed rename from a
protected path to an unprotected path, Git reports only the destination path,
which can hide the protected source path from
`find_protected_quality_gate_changes`.

This plan addresses only the executor committed-output guardrail for PR review
thread `PRRT_kwDOSJAM6s6DWQI1`.

## Requirements Checklist

- Add or reuse a rename-aware committed-diff path loader that includes both
  source and destination paths for renames and copies.
- Use the rename-aware path list in
  `_fail_if_protected_quality_gate_committed_output`.
- Add a regression test proving a committed rename such as
  `.github/workflows/ci.yml -> docs/ci.yml` is detected as a protected
  quality-gate violation.
- Keep existing staged-path and non-protected committed-path behavior intact.
- Do not push or switch branches.

## Implementation Steps

1. Add a failing unit test for the protected committed-output rename case.
2. Implement a rename-aware committed path helper using
   `git diff --name-status -z`.
3. Update `_fail_if_protected_quality_gate_committed_output` to consume the new
   helper.
4. Run the focused regression test, then the relevant unit test file.

## Assumptions/Changes

- Fake runner fixtures that exercise the pre-push committed-output guard need
  NUL-delimited `--name-status -z` output to match the new Git command.
- Existing executor coverage tests still assert the private `_git_show_text`
  wrapper, so keep that thin wrapper over the shared `git_show_text` loader.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges.py -q`
  passes.
- If time permits, run a narrower ruff check for touched Python files.
