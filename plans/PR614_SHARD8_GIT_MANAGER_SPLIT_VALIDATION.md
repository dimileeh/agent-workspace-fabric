# PR614 Shard 8 Git Manager Split Validation

## Result

Implemented. The current HEAD CI failure was `python-coverage-shards (8)`, where
`test_first_party_code_files_stay_under_line_limit` reported
`tests/unit/node/test_git_manager.py` at 1526 lines.

## Requirement Status

- [x] Preserved the current AWF-owned branch; did not switch branches or push.
- [x] Reproduced the line-limit failure locally with the focused guard.
- [x] Moved `TestVerifyHeadObjectExists` into
  `tests/unit/node/test_git_manager_head_object.py`.
- [x] Kept moved tests behaviorally identical.
- [x] Ran focused verification for the moved tests, original git-manager module,
  line-limit guard, and touched-file lint.
- [x] Left broad AWF/GitHub validation to AWF after agent completion.

## Evidence

- Failed before fix:
  `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  - Failed with `tests/unit/node/test_git_manager.py: 1526`.
- File sizes after fix:
  - `tests/unit/node/test_git_manager.py`: 1449 lines.
  - `tests/unit/node/test_git_manager_head_object.py`: 119 lines.
- Passed after fix:
  `uv run --python 3.12 --extra dev pytest tests/unit/node/test_git_manager_head_object.py -q`
  - `3 passed`.
- Passed after fix:
  `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  - `1 passed`.
- Passed after fix:
  `uv run --python 3.12 --extra dev pytest tests/unit/node/test_git_manager.py -q`
  - `48 passed`.
- Passed after fix:
  `uv run --python 3.12 --extra dev ruff check tests/unit/node/test_git_manager.py tests/unit/node/test_git_manager_head_object.py`
  - `All checks passed!`

## Notes

No workflow, quality-gate, or product-code files were changed. Full PR validation
and coverage aggregation remain owned by AWF/GitHub CI after this local fix.
