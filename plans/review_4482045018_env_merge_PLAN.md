# Review 4482045018 Env Merge Plan

## Problem Statement And Scope

Address the review-level feedback for PR comment `issue:4482045018` without changing branch state or pushing. The scope is limited to the env seeding merge helper in the CLI and the Compose interpolation key cache used by service helpers.

## Requirements Checklist

- Surface invalid UTF-8 in seed or overlay dotenv bytes as a merge failure instead of silently returning the unmodified seed.
- Preserve comments and blank context before the first occurrence of duplicate overlay-only keys when appending the final overlay-only value.
- Avoid using full Compose YAML contents as the interpolation-key cache key while preserving cache hits for unchanged files and reloads for changed files.
- Add focused regression coverage for the fixed edge cases.
- Run the narrow relevant tests and record validation evidence.

## Implementation Steps

1. Add regression tests in `tests/unit/cli/test_init.py` for invalid UTF-8 overlay merge failure and duplicate overlay-only key context preservation.
2. Add or adjust regression coverage in `tests/unit/service/test_logs.py` for the digest-keyed Compose interpolation cache.
3. Update `src/awf/cli/main.py` so UTF-8 decode failures raise `_EnvSeedMergeError` and duplicate overlay-only contexts are accumulated before the final value.
4. Update `src/awf/service/environment.py` so the cache key stores file path, content digest, and size rather than the full YAML text.
5. Run the targeted unit tests, then lint the touched Python files if practical.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py tests/unit/service/test_logs.py -q`
  - Passes with the new regressions.
- `uv run --python 3.12 --extra dev ruff check src/awf/cli/main.py src/awf/service/environment.py tests/unit/cli/test_init.py tests/unit/service/test_logs.py`
  - Passes with no lint errors.

## Assumptions/Changes

- Existing regression coverage requires dropping stale single-value comments tied to overwritten duplicate overlay-only assignments. The duplicate overlay-only fix therefore preserves header-style documentation blocks before the first duplicate and still drops stale value-specific comments.
