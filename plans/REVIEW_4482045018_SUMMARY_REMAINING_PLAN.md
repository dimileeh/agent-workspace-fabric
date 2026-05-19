# Review 4482045018 Summary Remaining Plan

## Problem Statement And Scope

Address the remaining actionable items from review-level comment `issue:4482045018`.
The scope is limited to:

- Docker CLI environment construction in `src/awf/service/bootstrap.py`.
- Compose env seed merging in `src/awf/cli/main.py`.
- Focused regression tests for both behaviors.

## Requirements Checklist

- Add a regression test proving bootstrap translates mixed-case `AWF_DOCKER_HOST` keys to `DOCKER_HOST`.
- Update bootstrap Docker env construction to find and remove `AWF_DOCKER_HOST` case-insensitively while keeping runtime service env precedence.
- Add a regression test proving leading file-header comments from a root `.env` overlay are preserved at the top of the merged compose env, not moved down to the first matching seed key.
- Update env seed merge behavior without weakening existing context-preservation tests.
- Run the narrow relevant unit tests and lint/type checks for the touched Python code.
- Create a validation document against this plan.
- Commit only the changed files for this review fix.

## Implementation Steps

1. Add failing tests in `tests/unit/service/test_bootstrap.py` and `tests/unit/cli/test_init.py`.
2. Implement case-insensitive AWF Docker host lookup/removal in `src/awf/service/bootstrap.py`.
3. Implement leading overlay header preservation in `src/awf/cli/main.py`.
4. Run focused tests for the modified areas.
5. Run lint and type validation.
6. Record validation in `plans/REVIEW_4482045018_SUMMARY_REMAINING_VALIDATION.md`.
7. Stage and commit the scoped changes.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_bootstrap.py tests/unit/cli/test_init.py -q`
  - Passes, including the new regression tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/bootstrap.py src/awf/cli/main.py tests/unit/service/test_bootstrap.py tests/unit/cli/test_init.py`
  - Passes with no lint violations.
- `uv run --python 3.12 --extra dev mypy src/awf`
  - Passes with no type errors.
