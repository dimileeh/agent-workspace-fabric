# PRRT_kwDOSJAM6s6DaP8C Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6DaP8C_PLAN.md`

## Requirement Status

- Complete: Added `WorkspaceRepository.create_replacement_from` to centralize replacement workspace creation from a source workspace's request contract.
- Complete: The helper sets request/profile fields through `WorkspaceRepository.create` and intentionally leaves runtime/PR fields off fresh replacements unless a caller explicitly provides `remote_push_branch`.
- Complete: Refactored `ControlWorker._create_preserved_active_replacement` to call the repository helper.
- Complete: Added repository regression coverage for copied request fields and mutable JSON/list isolation.
- Complete: Tightened preserved-active worker salvage coverage for fresh replacement metadata and idempotency key behavior.
- Complete: Prepared the local commit for this review thread.

## Evidence

Files changed:

- `src/awf/db/repositories.py`
- `src/awf/control/worker.py`
- `tests/unit/db/test_workspace_repository.py`
- `tests/unit/control/test_worker.py`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_workspace_repository.py::TestCreate::test_create_replacement_from_copies_request_fields -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_preserved_active_without_usable_work_creates_one_replacement_with_lineage -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_workspace_repository.py::TestCreate::test_create_replacement_from_copies_request_fields tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_preserved_active_without_usable_work_creates_one_replacement_with_lineage -q`
- `uv run --python 3.12 --extra dev ruff check src/awf tests`
- `uv run --python 3.12 --extra dev mypy src/awf`

All listed commands passed after implementation. The new repository test was first run before implementation and failed with `AttributeError: 'WorkspaceRepository' object has no attribute 'create_replacement_from'`, confirming the expected TDD failure.
