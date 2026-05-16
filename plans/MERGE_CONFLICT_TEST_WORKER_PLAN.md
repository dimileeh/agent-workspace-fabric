# Merge Conflict Test Worker Plan

## Problem Statement and Scope

Resolve the merge conflict left by merging `origin/codex/awf-post-merge-fixes`
into the current AWF-managed branch. The known conflicted file is
`tests/unit/control/test_worker.py`.

Scope is limited to resolving the conflict, preserving the intent of both the
current PR and the base branch, validating the resolved test surface, and
committing the merge result locally without pushing or changing branches.

## Requirements Checklist

- Preserve the current PR test that verifies preserved active execution events
  keep primary failure evidence.
- Preserve the base-branch test that verifies expired preserved active
  executions fail and clean up runtime resources.
- Remove all conflict markers from `tests/unit/control/test_worker.py`.
- Do not stage unrelated unstaged or untracked user changes outside the merge
  resolution.
- Run a narrow verification command for the touched worker tests.
- Create a validation document for this plan.
- Commit the merge resolution locally with a conventional commit message.

## Implementation Steps

1. Inspect the conflict and both sides of the conflicted hunk.
2. Resolve by retaining both independently named tests in the stale active
   execution recovery test class.
3. Check for hidden import/helper requirements from either side.
4. Run conflict-marker and narrow pytest verification.
5. Stage the resolved conflict, plan, and validation document, then commit.

## Verification Commands and Pass Criteria

- `rg -n "<<<<<<<|=======|>>>>>>>" tests/unit/control/test_worker.py` produces
  no matches.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q`
  passes, or any failure is documented if it is unrelated to the resolution.
- `git status` shows no unmerged paths before committing.
