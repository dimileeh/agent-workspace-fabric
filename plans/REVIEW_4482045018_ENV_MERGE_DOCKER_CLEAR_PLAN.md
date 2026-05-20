# Review 4482045018 Env Merge Docker Clear Plan

## Problem Statement And Scope

Address the current review-level feedback on PR #264 for compose env seeding and Docker CLI environment propagation. Scope is limited to the reported merge heuristics and the Docker CLI clear-value contract.

## Requirements Checklist

- Add regression coverage for a seed env file that starts with only blank lines before assignments, preserving the overlay file header.
- Add regression coverage for case-insensitive env key matching during seed/overlay merges so lowercase root keys override uppercase template keys without duplicate assignments.
- Replace the `docker_cli_client_environ()` empty-string scrub-marker contract with an explicit cleared-key API and update callers/tests.
- Preserve existing comment-ordering and overlay-only-key behavior.
- Commit only the changed files on the current AWF-managed branch.

## Implementation Steps

1. Add targeted tests in `tests/unit/cli/test_init.py` and `tests/unit/service/test_logs.py` that fail with the current implementation.
2. Update `src/awf/cli/main.py` to detect meaningful seed leading context and normalize env key identity for matching while preserving emitted key spelling.
3. Update `src/awf/service/environment.py` and `src/awf/service/logs.py` so Docker clear operations are returned as explicit scrub keys rather than empty env values.
4. Run the narrow tests for changed areas, then ruff/mypy if time permits.
5. Write `plans/REVIEW_4482045018_ENV_MERGE_DOCKER_CLEAR_VALIDATION.md` with requirement-by-requirement evidence.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py tests/unit/service/test_logs.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf tests`
- `uv run --python 3.12 --extra dev mypy src/awf`

Pass criteria: tests and static checks pass, and validation documents all checklist items as complete or explicitly explains any gap.
