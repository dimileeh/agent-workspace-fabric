# RELEASE_SYNC_TASK_KIND_VALIDATION

Plan reference: [RELEASE_SYNC_TASK_KIND_PLAN.md](RELEASE_SYNC_TASK_KIND_PLAN.md)

Mode: salvage continuation. AWF restored a near-complete implementation diff from
a prior run that timed out at the post-agent git commit step (only the
`ruff format --check` pre-commit hook failed; `ruff check` had already passed).
This phase verified the recovered code against the focused checks, applied the
missing formatting, and added the required `plans/` process docs. No behavioral
defects surfaced, so no source logic was changed during this phase.

## Requirement-by-requirement status

| # | Requirement | Status | Evidence |
|---|-------------|--------|----------|
| 1 | Remove `monitor_release_pr` from `TaskKind` + public docs | Complete | `src/awf/db/enums.py` (member removed; `sync_release_pr` docstring rewritten); docs in `docs/PLAN_PR_MONITOR.md`, `docs/PLAN_RELEASE_PR_SYNC.md`, `skills/awf-scheduler/SKILL.md`, `src/awf/runtime/pr_monitor.py`, `openapi.json` |
| 1 | Legacy `monitor_release_pr` fails fast; never feature work | Complete | `src/awf/control/executor.py` `_dispatch_non_feature_task_kind` → `policy_failure` / `DEPRECATED_TASK_KIND`; `src/awf/api/schemas.py` `WorkspaceTask._validate_kind` rejects it. Tests: `test_executor_error_paths.py::TestTaskKindFailFast`, `test_schema_coverage_edges.py` |
| 2 | `sync_release_pr` runs no coding agent / no feature PR | Complete | `_handoff_sync_release_pr_monitor` returns before the agent CLI step; dispatch returns `True` to short-circuit. Test: `TestSyncReleasePrHandoff` |
| 2 | Check source ahead of target; default source `development`, target `repo.base_branch`/`main`, master/explicit supported | Complete | `src/awf/runtime/release_pr_sync.py` `count_commits_ahead`; `executor._release_sync_source_branch`/`_release_sync_target_branch`; `service/workspaces.py` `release_sync` policy block. Tests: `test_release_pr_sync.py`, `TestReleaseSyncHelpers` |
| 2 | No commits ahead → complete cleanly with reason/event | Complete | `prepare_release_pr_sync` → `ReleasePrSyncNoOp`; `executor._complete_release_pr_sync_no_op` emits `NO_CHANGES_TO_SYNC` and completes. Tests: `test_release_pr_sync.py` (prepare no-op), `TestSyncReleasePrHandoff` (no-op completes w/o PR or monitor) |
| 2 | Reuse existing open source→target PR; else create | Complete | `find_or_create_release_pr` (reuse vs `gh pr create`). Tests: `test_release_pr_sync.py` (reuse/create), `TestSyncReleasePrHandoff` (reuse existing PR) |
| 2 | Enter `monitoring_pr` with PR metadata + `auto_merge=false`/release monitor | Complete | `executor._handoff_sync_release_pr_monitor` records pr_url/number/sha + `release_sync.pr` policy, transitions `running→validating→monitoring_pr`; `service._effective_auto_merge` forces `False`. Tests: `TestSyncReleasePrHandoff` (monitoring_pr, auto_merge=False captured, MergeCandidate open) |
| 2 | Preserve generic PR-adoption monitor path | Complete | `src/awf/service/pr_monitor_adoption.py` untouched; `sync_feature_pr` handoff retained via shared `_build_handoff_pr_monitor`. Tests: `test_workspaces_observability.py`, `test_workspace_retry.py` adoption cases pass |
| 3 | REST/MCP reject arbitrary unknown task kinds | Complete | `schemas.WorkspaceTask._validate_kind` (REST + MCP funnel through it). Tests: `test_schema_coverage_edges.py` (rejects unknown/deprecated/direct `sync_feature_pr`) |
| 3 | Direct kinds = `feature_branch_pr` + `sync_release_pr`; `sync_feature_pr` adoption-only | Complete | `schemas.PUBLIC_DIRECT_CREATE_TASK_KINDS`; `service._assert_supported_direct_create_task_kind`. Tests: `test_schema_coverage_edges.py`, `test_workspaces_observability.py` |
| 3 | Legacy/unsupported never fall through to feature provisioning | Complete | executor dispatch fails fast for deprecated/unknown; service re-check raises. Tests: `TestTaskKindFailFast` |
| 3 | Update CLI/API/MCP surfaces | Complete | `cli/main.py` `--task-kind`/`--source-branch`; `mcp/server.py` description; `schemas.WorkspaceRepo.source_branch`; `openapi.json` |
| 4 | `_GITHUB_PULL_HEAD_REF` defined once | Complete | `src/awf/node/git_manager.py` (duplicate removed). Test: `test_git_manager.py` |

All planned requirements: **Complete**. No `Partial`/`Missing` items, so no
iteration section is required.

## Commands run (focused; AWF/CI owns broad gating)

- `uv run pytest -q` over the focused set (plan §"Tests") →
  **496 passed in 279.90s**:
  - `tests/unit/runtime/test_release_pr_sync.py`
  - `tests/unit/control/test_executor_error_paths.py`
  - `tests/unit/api/test_schema_coverage_edges.py`
  - `tests/unit/node/test_provisioner.py`
  - `tests/unit/node/test_git_manager.py`
  - `tests/unit/common/test_github_client.py`
  - `tests/unit/service/test_workspace_retry.py`
  - `tests/unit/service/test_workspaces_observability.py`
- `uv run ruff format <changed files>` → 5 files reformatted
  (`src/awf/node/provisioner.py`, `src/awf/runtime/release_pr_sync.py`,
  `tests/unit/api/test_schema_coverage_edges.py`,
  `tests/unit/control/test_executor_error_paths.py`,
  `tests/unit/node/test_provisioner.py`) — the original post-agent timeout cause.
- `uv run ruff format --check <all changed files>` → **21 files already
  formatted** (clean).
- `uv run ruff check <changed source files>` → **All checks passed!**
- `uv run mypy src/awf/runtime/release_pr_sync.py src/awf/control/executor.py
  src/awf/node/provisioner.py src/awf/api/schemas.py
  src/awf/service/workspaces.py` → **Success: no issues found in 5 source files**.

Broad validation (full suite, coverage gates, `.awf/workspace.yml`) is owned by
AWF and GitHub CI after agent completion and was intentionally not run here.

## Files changed

New: `src/awf/runtime/release_pr_sync.py`,
`tests/unit/runtime/test_release_pr_sync.py`,
`plans/RELEASE_SYNC_TASK_KIND_PLAN.md`,
`plans/RELEASE_SYNC_TASK_KIND_VALIDATION.md`.

Modified: `src/awf/db/enums.py`, `src/awf/control/executor.py`,
`src/awf/common/github_client.py`, `src/awf/node/provisioner.py`,
`src/awf/node/git_manager.py`, `src/awf/api/schemas.py`,
`src/awf/service/workspaces.py`, `src/awf/control/worker.py`,
`src/awf/service/provider_recovery.py`, `src/awf/cli/main.py`,
`src/awf/mcp/server.py`, `src/awf/runtime/pr_monitor.py`,
`docs/PLAN_PR_MONITOR.md`, `docs/PLAN_RELEASE_PR_SYNC.md`,
`skills/awf-scheduler/SKILL.md`, `openapi.json`, and the corresponding test
modules under `tests/unit/`.
