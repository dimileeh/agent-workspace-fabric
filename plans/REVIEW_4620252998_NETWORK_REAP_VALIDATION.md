# Review 4620252998 Network Reap Validation

Plan reference: `plans/REVIEW_4620252998_NETWORK_REAP_PLAN.md`

## Requirement Status

- Remove matching compose networks in one Docker invocation so all discovered
  network IDs are submitted together: Complete.
- Keep existing container and volume cleanup behavior unchanged: Complete.
- Clarify at `_PRESERVED_COMPOSE_TEARDOWN_FALLBACK_STATES` that allowed
  preserved states also become eligible for lease revocation and reservation
  release after successful compose teardown: Complete.
- Add/update focused regression coverage for the bulk network removal command:
  Complete.
- Do not run broad AWF/GitHub-owned validation; use targeted tests only:
  Complete.

## Evidence

Changed files:

- `src/awf/node/compose_manager.py`
- `src/awf/service/gc.py`
- `tests/unit/node/test_compose_manager.py`
- `plans/REVIEW_4620252998_NETWORK_REAP_PLAN.md`
- `plans/REVIEW_4620252998_NETWORK_REAP_VALIDATION.md`

Focused checks:

- Failing-first check before the production fix:
  `uv run --python 3.12 --extra dev pytest tests/unit/node/test_compose_manager.py -q -k remove_project_by_label_removes_containers_networks_and_volumes`
  failed because `remove_project_by_label` called `docker network rm` once per
  network instead of passing both discovered network IDs in one invocation.
- Passing check after the production fix:
  `uv run --python 3.12 --extra dev pytest tests/unit/node/test_compose_manager.py -q -k remove_project_by_label_removes_containers_networks_and_volumes`
  passed with `1 passed, 45 deselected`.
- Focused cleanup behavior check:
  `uv run --python 3.12 --extra dev pytest tests/unit/node/test_compose_manager.py tests/unit/node/test_compose_manager_subprocess.py -q -k remove_project_by_label`
  passed with `4 passed, 78 deselected`.
- Focused lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/node/compose_manager.py src/awf/service/gc.py tests/unit/node/test_compose_manager.py tests/unit/node/test_compose_manager_subprocess.py`
  passed.
- Diff hygiene:
  `git diff --check` passed.

Full AWF/GitHub validation is managed by AWF after agent completion.

## Gaps

None.
