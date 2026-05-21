# REVIEW_PRRT_kwDOSJAM6s6Di9gL Missing Path Recovery Plan

## Problem Statement and Scope

`git_show_text` treats failed `git cat-file -e <ref>:<path>` lookups as recoverable
whenever the ref resolves to a commit. That can hide object-read or corruption
failures for paths that really exist. Scope is limited to the protected file diff
loader and focused unit coverage for distinguishing absent paths from other git
failures.

## Requirements Checklist

- Add a regression test proving `git_show_text` raises when `cat-file` fails for a
  path that is still present in the target ref tree.
- Preserve recovery for genuinely missing ref paths without relying on English git
  stderr text.
- Preserve recovery for genuinely missing index paths.
- Keep unexpected git failures surfaced as `RuntimeError` with original error
  details.

## Implementation Steps

1. Add the failing regression in `tests/unit/control/test_protected_file_diffs.py`.
2. Replace commit-existence-only recovery with a tree/index membership probe.
3. Update existing call expectations for the new probe command.
4. Run focused unit tests for protected file diffs.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_protected_file_diffs.py -q`
  passes.
