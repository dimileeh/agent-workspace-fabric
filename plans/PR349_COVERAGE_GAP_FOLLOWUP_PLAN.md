# PR349 Coverage Gap Follow-Up Plan

## Problem Statement and Scope

PR #349 still has a failing `python-full-coverage` signal from GitHub Actions:
all tests passed, but aggregate coverage was below the required 99% threshold.
The latest completed failure showed the largest changed-code gap in
`src/awf/runtime/validation_worktree.py`, with related gaps in PR monitor
pre-push validation helpers.

This follow-up is scoped to adding focused unit coverage for existing behavior
in the validation worktree and pre-push cleanup paths. It must not weaken,
skip, or disable the CI gate, and it must not run broad repository coverage
locally.

## Requirements Checklist

- Keep all work on the current AWF-managed branch; do not switch branches,
  push, rebase, or force-push.
- Do not edit protected workflow, coverage, or quality-gate configuration.
- Add tests for meaningful uncovered cleanup/result branches without changing
  production behavior unless a real defect is found.
- Use focused local tests and targeted module coverage diagnostics only.
- Record validation evidence and residual risk in a validation document.
- Commit the fix locally with a conventional CI-fix message.

## Implementation Steps

1. Inspect the completed GitHub coverage failure and current PR check status.
2. Run targeted module coverage diagnostics for the changed validation worktree
   tests to identify remaining meaningful uncovered branches.
3. Add focused unit tests for no-op cleanup, skipped guards, serialized details,
   and message rendering branches.
4. Run the touched unit tests and focused lint/format checks.
5. Save `plans/PR349_COVERAGE_GAP_FOLLOWUP_VALIDATION.md` with evidence.
6. Commit the local fix.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py tests/unit/runtime/test_validation_worktree_head_cleanup.py tests/unit/runtime/test_validation_worktree_signatures.py -q`
  passes.
- Targeted diagnostic:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py tests/unit/runtime/test_validation_worktree_head_cleanup.py tests/unit/runtime/test_validation_worktree_signatures.py --cov=awf.runtime.validation_worktree --cov-report=term-missing --cov-fail-under=0 -q`
  shows the touched module coverage moved upward.
- Focused lint/format checks pass for changed tests.

Full AWF/GitHub validation and repository-wide coverage remain owned by AWF
after agent completion.
