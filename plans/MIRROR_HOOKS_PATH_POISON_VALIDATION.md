# Mirror Hooks Path Poison Validation

Plan reference: `plans/MIRROR_HOOKS_PATH_POISON_PLAN.md`

## Requirement Status

- Confirm the review against the current implementation: Complete. The original
  implementation probed `core.hooksPath` and then ran `git config --unset-all
  core.hooksPath`, which removed legitimate hook directories as reported.
- Add focused regression coverage for legitimate hook paths: Complete. Added
  tests for preserving a legitimate hooks path by itself and preserving it while
  removing `/dev/null`.
- Remove only known poisoned `core.hooksPath` values: Complete. Repair now reads
  all local hooks path values and unsets only exact known poison values.
- Preserve current success and error handling: Complete. Existing focused repair
  tests for no config, duplicate poison values, concurrent cleanup, environment
  cleanup, and failure paths pass.
- Run only focused tests: Complete. Broad AWF/GitHub validation is managed after
  agent completion.

## Evidence

Files changed:

- `src/awf/node/git_manager.py`
- `tests/unit/node/test_git_manager.py`
- `plans/MIRROR_HOOKS_PATH_POISON_PLAN.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_git_manager.py::TestRepairMirrorHooksPath -q`
  before implementation: failed on the two new regression tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_git_manager.py::TestRepairMirrorHooksPath -q`
  after implementation: passed, `9 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/node/git_manager.py tests/unit/node/test_git_manager.py`
  passed.

## Gaps

None for the scoped review thread. Full repository validation and merge-gate
provenance remain with AWF/GitHub after agent completion.
