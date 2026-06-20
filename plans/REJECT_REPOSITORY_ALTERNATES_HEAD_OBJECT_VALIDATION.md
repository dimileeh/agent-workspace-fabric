# Reject Repository Alternates In HEAD Object Verification Validation

Plan reference: `plans/REJECT_REPOSITORY_ALTERNATES_HEAD_OBJECT_PLAN.md`

## Requirement status

- Add a regression test showing a mirror-local `objects/info/alternates` file can otherwise make a missing HEAD object pass verification: Complete.
- Make `verify_head_object_exists()` fail closed when the backing repository declares object alternates: Complete.
- Preserve existing behavior for valid HEADs, missing objects, and inherited object lookup environment variables: Complete.
- Run only focused checks for the changed behavior: Complete.

## Evidence

- Changed `tests/unit/node/test_git_manager_head_object.py` with `test_fails_closed_for_repository_alternates`.
- Changed `src/awf/node/git_manager.py` so HEAD verification rejects repository alternates before trusting `git cat-file`.
- Confirmed the new regression failed before the implementation:
  - `uv run --python 3.12 --extra dev pytest tests/unit/node/test_git_manager_head_object.py::TestVerifyHeadObjectExists::test_fails_closed_for_repository_alternates -q`
  - Result before implementation: failed because `verify_head_object_exists()` returned `True`.
- Focused verification after implementation:
  - `uv run --python 3.12 --extra dev pytest tests/unit/node/test_git_manager_head_object.py -q`
  - Result: `4 passed`.
  - `uv run --python 3.12 --extra dev ruff check src/awf/node/git_manager.py tests/unit/node/test_git_manager_head_object.py`
  - Result: passed.

Full AWF/GitHub validation was not run in the agent phase; AWF owns broad validation, provenance, and merge gating after completion.
