# Merge Origin Development Plan

## Problem Statement and Scope

Resolve the in-progress merge of `origin/development` into the current AWF-managed branch without switching branches or pushing. Preserve the intent of both the base branch and this PR across the listed conflicted files, preferring base-branch semantics when a hunk is ambiguous.

## Requirements Checklist

- Keep the current branch and merge state intact.
- Resolve all conflict markers in the listed files.
- Preserve both sides' behavior where compatible.
- Prefer incoming `origin/development` semantics when a conflict cannot be reconciled confidently.
- Run only focused local checks relevant to the resolved files.
- Create a validation document recording the resolution evidence.
- Stage resolved files and commit locally with a conventional merge-resolution message.

## Implementation Steps

1. Inspect unmerged file stages and conflict markers for all conflicted files.
2. Resolve conflicts file by file, using structured tools for generated JSON where practical.
3. Review the final diff for conflict markers and unintended broad changes.
4. Run targeted tests or focused checks for the touched behavior only.
5. Write `plans/MERGE_ORIGIN_DEVELOPMENT_VALIDATION.md`.
6. Stage all resolved files and commit locally.

## Verification Commands and Pass Criteria

- `git diff --check`: passes without whitespace or conflict-marker errors.
- `rg '<<<<<<<|=======|>>>>>>>' <resolved files>`: returns no conflict markers.
- Targeted tests for affected adapter, provider readiness, smoke, usage, and Dockerfile behavior pass where practical.

Full AWF/GitHub validation remains owned by AWF after agent completion per the workspace contract.
