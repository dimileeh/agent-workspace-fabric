# Merge Conflict Resolution Plan

## Problem Statement and Scope

Resolve the in-progress merge of `origin/codex/awf-post-merge-fixes` into the current AWF-managed feature branch without switching branches or pushing. Conflicts are limited to `openapi.json`, `src/awf/api/routes/callbacks.py`, and `tests/unit/api/test_callbacks.py`.

## Requirements Checklist

- Preserve the feature branch intent and base branch semantics in each conflict.
- Resolve all conflict markers in the three conflicted files.
- Regenerate or reconcile `openapi.json` so it reflects the resolved API code.
- Stage the touched conflict files and commit the merge resolution locally.
- Do not switch branches, rebase, push, or rewrite user work.

## Implementation Steps

1. Inspect the conflict hunks and the stage 2/stage 3 versions of each conflicted source/test file.
2. Resolve `src/awf/api/routes/callbacks.py` by combining callback rate limiting and replay semantics from the feature branch with bearer authentication and callback target policy error handling from the base branch.
3. Resolve `tests/unit/api/test_callbacks.py` so the callback tests exercise both authenticated access and rate-limit/idempotent replay behavior.
4. Generate or check `openapi.json` from the resolved application.
5. Run focused verification for conflict markers, OpenAPI drift, and affected tests.
6. Stage the merge resolution files and create a conventional local merge commit.

## Verification Commands and Pass Criteria

- `rg -n "<<<<<<<|=======|>>>>>>>" openapi.json src/awf/api/routes/callbacks.py tests/unit/api/test_callbacks.py` returns no matches.
- `python scripts/generate_openapi.py --check` passes.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_callbacks.py tests/unit/api/test_openapi_artifact.py -q` passes.
