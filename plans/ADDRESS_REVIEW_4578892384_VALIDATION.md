# Address Review Comment 4578892384 Validation

Plan reference: `plans/ADDRESS_REVIEW_4578892384_PLAN.md`

## Requirement Status

- Preserve stricter custom-profile artifact filtering: Complete.
  Existing tests still assert shorthand custom paths such as
  `docs/alternate/ws_123.md` remain inter-workspace owned paths when no concrete
  workspace ID is known.
- Keep default `docs/awf-plans/ws_*` classification broad: Complete.
  Existing tests still cover broad default reserved-directory matching.
- Share or test-back generated workspace ID format with custom artifact matching:
  Complete.
  `awf.common.ids` now exposes the generated workspace ID prefix, suffix regex,
  and full regex, and `awf.common.owned_paths` consumes that suffix/prefix
  contract.
- Add detached ORM mutation warning: Complete.
  `_profile_from_resolved_profile_snapshot` now documents that the realigned
  detached `Workspace` object must not be reattached with `session.add()` or
  `session.merge()`.
- Run focused tests only: Complete.
  No broad AWF/GitHub validation, full coverage gate, or whole-repository suite
  was manually run in the agent phase. The repository's local `git commit`
  hook ran its configured pre-commit checks during commit and passed.

## Evidence

Files changed:

- `src/awf/common/ids.py`
- `src/awf/common/owned_paths.py`
- `src/awf/control/executor/helpers.py`
- `tests/unit/common/test_owned_paths.py`
- `plans/ADDRESS_REVIEW_4578892384_PLAN.md`
- `plans/ADDRESS_REVIEW_4578892384_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_owned_paths.py::test_custom_profile_workspace_id_glob_tracks_generated_id_contract -q`
  - First run failed as expected before implementation because
    `awf.common.ids` did not expose `WORKSPACE_ID_PATTERN`.
  - Final run passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/common/ids.py src/awf/common/owned_paths.py src/awf/control/executor/helpers.py tests/unit/common/test_owned_paths.py`
  - Passed after test import/comparison style cleanup.
- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_owned_paths.py tests/unit/control/test_executor_runtime_profile_snapshot.py -q`
  - Passed: `57 passed`.
- `git commit -m "fix: address review comment 4578892384 — align workspace id artifacts"`
  - Local commit hook passed: trailing whitespace, EOF, large-file,
    merge-conflict, private-key, ruff check, ruff format check, and mypy.

Full AWF/GitHub validation remains managed after agent completion by AWF and CI.
