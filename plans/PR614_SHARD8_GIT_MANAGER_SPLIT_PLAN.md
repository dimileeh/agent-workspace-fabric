# PR614 Shard 8 Git Manager Split Plan

## Problem Statement and Scope

Current PR #614 HEAD fails GitHub Actions `python-coverage-shards (8)` because
`tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit`
reports `tests/unit/node/test_git_manager.py` at 1526 lines, above the 1500-line
first-party file limit.

Scope is limited to splitting a coherent group of existing git-manager tests into
a new small test module. Do not edit the maintainability guardrail, workflow
configuration, or product code.

## Requirements Checklist

- [ ] Preserve the current AWF-owned branch; do not switch branches or push.
- [ ] Reproduce the line-limit failure with the focused maintainability test.
- [ ] Move an existing cohesive test group out of `test_git_manager.py`.
- [ ] Keep moved tests behaviorally identical.
- [ ] Run focused tests for the moved group and the line-limit guard.
- [ ] Record validation evidence in a validation document.
- [ ] Commit the local fix with a conventional commit message.

## Implementation Steps

1. Run the focused line-limit test to confirm the current failure.
2. Move `TestVerifyHeadObjectExists` from `tests/unit/node/test_git_manager.py`
   into a new `tests/unit/node/test_git_manager_head_object.py` module with only
   the local fixtures/imports it needs.
3. Re-run the moved tests and the line-limit guard.
4. Write `plans/PR614_SHARD8_GIT_MANAGER_SPLIT_VALIDATION.md` with the observed
   CI failure and focused local verification.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_git_manager.py tests/unit/node/test_git_manager_head_object.py -q`

Full AWF/GitHub validation remains owned by AWF after agent completion.
