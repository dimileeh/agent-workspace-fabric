# PRRT_kwDOSJAM6s6GRYVz Host Ports Plan

## Problem Statement and Scope

Review thread `PRRT_kwDOSJAM6s6GRYVz` reports that host-port admission ignores terminal
legacy rows whose `compose_project_name` and `compose_file_path` are both null, even
though terminal cleanup still treats those rows as possible leaked default Compose
projects by deriving `awf_<workspace_id>`.

Scope is limited to repository host-port conflict detection and focused regression
coverage. The change must preserve the modern invariant that pre-launch terminal
workspaces which never started Compose do not block host-port reuse.

## Requirements Checklist

- Add a failing regression for legacy terminal null-runtime rows with declared host ports.
- Preserve tests for modern pre-launch/null-compose rows that did not acquire runtime.
- Update `find_host_port_conflicts` so unreleased terminal candidates include:
  - rows with persisted `compose_project_name`;
  - rows with persisted `compose_file_path`;
  - legacy null-runtime rows that lack modern node/reservation attribution.
- Keep node-scoped admission behavior intact for legacy null-node rows.
- Run only focused tests for the changed behavior; broad AWF/GitHub validation is left to AWF.

## Implementation Steps

1. Add regression coverage in the host-port repository test module for a terminal
   legacy row with null compose metadata, no node, no reservation, and host ports from
   both task policy and resolved profile.
2. Confirm the new regression fails against the current query.
3. Update the terminal unreleased predicate in
   `src/awf/db/repositories/workspace_repo_host_ports.py`.
4. Add retry-source regression coverage for the same legacy null-runtime/no-reservation
   shape, because retry excludes the source workspace from conflict scanning.
5. Update the retry source-runtime guard to use the same modern-versus-legacy
   runtime attribution semantics.
6. Adjust existing direct repository tests if needed so they model modern pre-launch
   rows with modern placement evidence rather than legacy no-node/no-reservation rows.
7. Run focused host-port and retry tests that cover the regression and invariant.

## Assumptions/Changes

- The review location is the repository conflict query, but retry admission has a
  separate source-runtime guard. Since that path excludes the source workspace from
  `find_host_port_conflicts`, the same legacy null-runtime case must be handled there
  to fully close retry admission for the source workspace itself.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_workspace_repository_parts/test_workspace_repository_host_port_collision.py::<node> -q`
  - New legacy null-runtime regression fails before implementation and passes after.
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_workspace_repository_parts/test_workspace_repository_host_port_collision.py -q`
  - Host-port repository behavior remains green.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_retry_port.py::<node> -q`
  - Legacy null-runtime retry source is rejected while modern reservation-backed
    pre-compose retry remains allowed.
- Full AWF/GitHub validation is intentionally not run in-agent per workspace contract.
