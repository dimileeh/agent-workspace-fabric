# Review 4482045018 Env Heuristics Fastpath Plan

## Problem Statement And Scope

Address the actionable review-level feedback from PR comment `issue:4482045018`
around dotenv merge comment heuristics and service environment lookup cost.

Scope is limited to:

- `_env_comment_looks_key_specific` and `_split_env_file_header_context` in
  `src/awf/cli/main.py`.
- `env_lookup` in `src/awf/service/environment.py`.
- Focused unit tests proving the reported edge cases.

## Requirements Checklist

- Add a regression proving a single significant key word, such as `token` from
  `AWF_TOKEN`, can match a descriptive comment like `provider access token`.
- Add a regression proving leading overlay file-header comments are not all
  reclassified as assignment context when only the final adjacent comment is
  key-specific.
- Add a regression proving `env_lookup` uses a direct lookup fast path before
  iterating a mapping when the exact key exists.
- Keep existing dotenv merge behavior, ordering, and service environment
  semantics intact.
- Commit the local fix with a conventional commit message for comment
  `4482045018`.

## Implementation Steps

1. Add focused tests in `tests/unit/cli/test_init.py` and a dedicated
   environment-helper test file.
2. Run the new focused tests before implementation and confirm they fail.
3. Make the smallest code changes in `src/awf/cli/main.py` and
   `src/awf/service/environment.py`.
4. Re-run focused tests, broader impacted unit tests, and ruff for touched
   files.
5. Write
   `plans/REVIEW_4482045018_ENV_HEURISTICS_FASTPATH_VALIDATION.md` with
   requirement-by-requirement evidence.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py::test_merge_env_seed_treats_single_word_key_comment_as_assignment_context tests/unit/cli/test_init.py::test_merge_env_seed_splits_header_at_last_adjacent_key_comment tests/unit/service/test_environment.py::test_env_lookup_uses_exact_key_fast_path -q`
  fails before implementation and passes after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py tests/unit/service/test_environment.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/cli/main.py src/awf/service/environment.py tests/unit/cli/test_init.py tests/unit/service/test_environment.py`
  passes.
