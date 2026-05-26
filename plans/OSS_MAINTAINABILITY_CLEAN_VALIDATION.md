# OSS Maintainability Clean Validation

Plan reference: `plans/OSS_MAINTAINABILITY_CLEAN_PLAN.md`

## Status

Complete.

The repo-wide first-party maintainability guard is green. All included `.py`,
`.ts`, `.tsx`, `.js`, and `.jsx` files under `src/`, `tests/`, `scripts/`, and
the first-party console app folders are at or below the hard 1,500-line limit.

## Baseline

The repo-wide maintainability scan found 60 first-party code files above the
1,500-line limit before this cleanup pass.

Largest baseline offenders:

- `tests/unit/control/test_worker.py`: 21,818 lines
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py`: 8,578 lines
- `tests/unit/control/test_quality_gates.py`: 6,735 lines
- `tests/unit/control/test_executor_coverage_edges.py`: 6,063 lines
- `tests/unit/runtime/test_pr_monitor_runner.py`: 5,769 lines
- `apps/console/components/console-dashboard.tsx`: 4,933 lines
- `src/awf/cli/main.py`: 3,656 lines
- `src/awf/service/workspaces.py`: 3,174 lines
- `src/awf/control/quality_gates.py`: 3,009 lines
- `src/awf/runtime/validation.py`: 2,811 lines

## Implemented

- Created local checkpoint branch `codex/oss-maintainability-cleanup`.
- Committed the prior core decomposition checkpoint as
  `refactor: split core AWF monoliths for maintainability`.
- Replaced core catch-all shared modules with focused config, constants, types,
  protocols, and helper modules:
  - removed `src/awf/control/executor/shared.py`;
  - removed `src/awf/control/worker/shared.py`;
  - removed `src/awf/runtime/pr_monitor_runner/shared.py`.
- Split oversized production/source files by domain while preserving public
  facades:
  - CLI command groups and init/service/profile/workspace helpers;
  - REST schemas and service workspaces/controls/metrics/provider/supply-chain
    helpers;
  - GitHub client parsing/adoption helpers;
  - runtime validation setup/coverage/runner/types;
  - MCP workspace/metrics/control tool modules;
  - diff-aware quality-gate helpers;
  - repository callback/secret helpers;
  - console dashboard overview/capacity/log/security/shared components.
- Split oversized unit and integration tests into scenario part files under
  explicit `*_parts/` packages.
- Updated tests to patch/import implementation modules directly instead of
  package-root private helpers or namespace proxy shims.
- Strengthened `tests/unit/test_core_decomposition_maintainability.py` to fail
  on:
  - any first-party code file above 1,500 lines;
  - `_hydrate` helpers, `globals()` copying, `__dict__.update(...)`, broad
    file-level suppressions, and test namespace proxy helpers;
  - dynamic package facades;
  - implementation imports from orchestrator modules;
  - catch-all core shared barrels;
  - broad orchestrator `__all__` private export barrels.
- Serialized the full Alembic partial-upgrade migration chains in the migration
  graph tests so parallel xdist runs cannot interleave migration subprocesses.

## Final Line Count Evidence

Largest first-party files after cleanup:

- `tests/unit/profiles/test_profiles.py`: 1,500 lines
- `src/awf/api/schemas.py`: 1,498 lines
- `src/awf/service/supply_chain_policy.py`: 1,497 lines
- `tests/unit/service/test_provider_recovery_parts/test_provider_recovery_part_002.py`: 1,496 lines
- `tests/unit/api/test_workspaces_parts/test_workspaces_part_002.py`: 1,496 lines
- `tests/unit/service/test_callbacks_parts/test_callbacks_part_001.py`: 1,494 lines
- `tests/unit/control/test_executor_monitor_recovery_parts/test_executor_monitor_recovery_part_001.py`: 1,494 lines
- `tests/unit/service/test_workspaces_observability_parts/test_workspaces_observability_part_001.py`: 1,493 lines
- `tests/unit/cli/test_init_parts/test_init_part_003.py`: 1,493 lines
- `tests/unit/mcp/test_mcp_operator_surfaces_parts/test_mcp_operator_surfaces_part_002.py`: 1,490 lines
- `tests/unit/cli/test_service_cli_parts/test_service_cli_part_001.py`: 1,488 lines
- `tests/unit/service/test_config_parts/test_config_part_001.py`: 1,487 lines
- `tests/unit/control/test_executor_post_agent_commit_parts/test_executor_post_agent_commit_part_001.py`: 1,487 lines
- `src/awf/service/gc.py`: 1,487 lines

No included first-party code file exceeds 1,500 lines.

## Validation Evidence

Targeted fixes after the full-unit run:

```bash
uv run --python 3.12 --extra dev pytest \
  tests/unit/cli/test_cli_parts/test_cli_part_002.py::TestCliHelp::test_workspace_help_contains_dx_guidance \
  tests/unit/cli/test_init_parts/test_init_part_001.py::test_init_guided_writes_answers_into_workspace_yml \
  tests/unit/cli/test_init_parts/test_init_part_001.py::test_init_guided_accepts_multiple_validation_commands \
  tests/unit/cli/test_init_parts/test_init_part_001.py::test_init_write_profile_guided_declined_confirmation_does_not_write \
  tests/unit/contracts/test_registry_smoke.py::test_mcp_implemented_matrix_rows_have_executable_coverage_reference \
  tests/unit/db/test_migration_graph.py::test_workspace_event_order_migration_reruns_after_column_exists \
  tests/unit/db/test_migration_graph.py::test_workspace_event_order_migration_backfills_existing_events \
  tests/unit/db/test_task_attempts.py::TestTaskAttemptMigration::test_task_attempt_migration_creates_tables \
  -q -n 8
# 8 passed
```

Full unit validation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit -q -n 20
# 7786 passed in 1668.70s (0:27:48)
```

Maintainability guard:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py -q
# 9 passed in 2.58s
```

Static and drift validation:

```bash
uv run --python 3.12 --extra dev ruff check src/awf tests scripts
# All checks passed!

uv run --python 3.12 --extra dev ruff format --check src/awf tests scripts
# 766 files already formatted

uv run --python 3.12 --extra dev mypy src/awf
# Success: no issues found in 271 source files

uv run --python 3.12 --extra dev python scripts/generate_openapi.py --check
# OK: openapi.json matches the current app spec.
```

Integration validation:

```bash
uv run --python 3.12 --extra dev pytest tests/integration -q -n 20
# 88 passed, 1 skipped in 193.60s (0:03:13)
```

Console validation:

```bash
npm --prefix apps/console run lint
# passed

npm --prefix apps/console run typecheck
# passed

npm --prefix apps/console run build
# passed
```

## Remaining Gaps

None for this plan.

Large first-party files outside the original core decomposition scope were
included in this cleanup pass and are now covered by the repo-wide guard. Future
maintainability work should focus on domain clarity and reviewability of the
new split modules, not on emergency line-count reduction.
