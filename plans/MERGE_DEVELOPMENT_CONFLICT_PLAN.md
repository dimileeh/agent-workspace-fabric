# Merge Development Conflict Plan

## Problem Statement and Scope

Resolve the paused `git merge origin/development` conflict in `src/awf/cli/main.py`
for PR #282 without switching branches, pushing, or running broad AWF/GitHub-owned
validation.

## Requirements Checklist

- Preserve the base branch's guided project-onboarding behavior.
- Preserve the PR branch's unrelated CLI additions already merged into the file.
- Remove all conflict markers from `src/awf/cli/main.py`.
- Stage the resolved file and local plan/validation evidence.
- Commit locally with a conventional merge-resolution message.
- Use only focused checks; leave broad validation to AWF/GitHub after agent completion.

## Implementation Steps

1. Inspect merge status and conflict hunks.
2. Resolve `src/awf/cli/main.py` by selecting the base branch's onboarding
   imports, signatures, and descriptive docstrings where the hunks overlap.
3. Confirm no conflict markers remain and the file parses.
4. Record validation evidence.
5. Stage all merge-resolution changes and create a local commit.

## Verification Commands and Pass Criteria

- `rg -n "^(<<<<<<<|=======|>>>>>>>)" src/awf/cli/main.py` returns no matches.
- `uv run --python 3.12 --extra dev python -m py_compile src/awf/cli/main.py`
  exits successfully.
- `git status --short` shows no unresolved `UU` entries before commit.

Full AWF/GitHub validation is intentionally not run inside this agent phase.
