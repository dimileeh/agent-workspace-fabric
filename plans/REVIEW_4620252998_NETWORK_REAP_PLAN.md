# Review 4620252998 Network Reap Plan

## Problem Statement And Scope

Address the current PR review comment `issue:4620252998` findings about
label-scoped compose fallback cleanup:

- `ComposeManager.remove_project_by_label` removes matching networks one at a
  time, so an early `docker network rm` failure skips later networks.
- The preserved-workspace compose teardown fallback allowlist does not make it
  obvious that adding states there permits full runtime side effects after a
  successful teardown, not teardown-only behavior.

Scope is limited to the network removal command shape, focused regression
expectations, and a clarifying comment at the allowlist extension point.

## Requirements Checklist

- Remove matching compose networks in one Docker invocation so all discovered
  network IDs are submitted together.
- Keep existing container and volume cleanup behavior unchanged.
- Clarify at `_PRESERVED_COMPOSE_TEARDOWN_FALLBACK_STATES` that allowed
  preserved states also become eligible for lease revocation and reservation
  release after successful compose teardown.
- Add/update focused regression coverage for the bulk network removal command.
- Do not run broad AWF/GitHub-owned validation; use targeted tests only.

## Implementation Steps

1. Update the focused compose-manager test expectation first to require one
   `docker network rm` invocation containing all discovered network IDs.
2. Confirm the updated expectation fails against the current loop-based
   implementation.
3. Change `remove_project_by_label` to submit all discovered network IDs in a
   single `docker network rm` call.
4. Add the clarifying allowlist comment in `src/awf/service/gc.py`.
5. Run targeted tests and lint for the changed files.
6. Commit the scoped fix locally with a conventional commit message.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_compose_manager.py -q -k remove_project_by_label_removes_containers_networks_and_volumes`
  - Fails before the production change because two network remove calls are
    made instead of one.
  - Passes after the production change.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_compose_manager.py tests/unit/node/test_compose_manager_subprocess.py -q -k remove_project_by_label`
  - Passes for the affected compose-manager cleanup behavior.
- `uv run --python 3.12 --extra dev ruff check src/awf/node/compose_manager.py src/awf/service/gc.py tests/unit/node/test_compose_manager.py tests/unit/node/test_compose_manager_subprocess.py`
  - Passes for the changed files.
- `git diff --check`
  - Passes.

Full AWF/GitHub validation is intentionally left to AWF after agent completion.
