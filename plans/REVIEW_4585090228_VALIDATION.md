# Review 4585090228 Validation

Plan reference: `plans/REVIEW_4585090228_PLAN.md`

## Requirement Status

- Complete: Update the no-source-reservation retry regression so the retried workspace queue decision stores the created reservation summary instead of `{}`.
- Complete: Populate `retry_resource_summary` from the constructed retry reservation regardless of whether the source reservation existed.
- Complete: Update the compose-fail backstop commit regression so `provision_claimed()` returns after recording `COMPOSE_FAIL_COMMIT_FATAL` instead of propagating the original compose error.
- Complete: Adjust the provisioner compose-fail commit failure path to avoid duplicate caller failure handling while preserving the terminal failed row and event.
- Complete: Keep changes scoped and avoid broad AWF/GitHub-owned validation.

## Evidence

Files changed:

- `src/awf/service/workspaces_retry.py`
- `src/awf/node/provisioner.py`
- `tests/unit/service/test_workspace_retry_port.py`
- `tests/unit/node/test_provisioner_parts/test_provisioner_part_003.py`
- `plans/REVIEW_4585090228_PLAN.md`
- `plans/REVIEW_4585090228_VALIDATION.md`

Focused failing-first checks:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_retry_port.py::test_retry_persist_reservation_when_source_has_none -q`
  - Failed before implementation with `KeyError: 'node_id'` because the queue decision resource summary was empty.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_provisioner_parts/test_provisioner_part_003.py::TestFailureHandling::test_compose_fail_backstop_commit_failure_records_terminal_failure_without_reraise -q`
  - Failed before implementation because the original `ComposeOperationError` still propagated from the compose-fail fatal path.

Focused passing checks:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_retry_port.py::test_retry_persist_reservation_when_source_has_none -q`
  - Passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_provisioner_parts/test_provisioner_part_003.py::TestFailureHandling::test_compose_fail_backstop_commit_failure_records_terminal_failure_without_reraise -q`
  - Passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_retry_port.py::test_retry_persist_reservation_when_source_has_none tests/unit/node/test_provisioner_parts/test_provisioner_part_003.py::TestFailureHandling::test_compose_fail_backstop_commit_failure_records_terminal_failure_without_reraise -q`
  - Passed: `2 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/workspaces_retry.py src/awf/node/provisioner.py tests/unit/service/test_workspace_retry_port.py tests/unit/node/test_provisioner_parts/test_provisioner_part_003.py`
  - Passed.

Full AWF/GitHub validation was not run during the agent phase per the workspace contract; AWF owns broad validation, provenance, logs, and merge gating after completion.

## Gaps

None.
