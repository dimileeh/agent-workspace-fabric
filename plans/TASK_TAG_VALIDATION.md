# Validation — Task tag (Jira issue key) on branches, PR titles, commits (#526)

Plan contract: `docs/awf-plans/ws_2c1a3aadc16b4a69b729c34d.md`.

## What was implemented

- **New helper** `src/awf/common/task_tag.py`: `TASK_TAG_PATTERN`
  (`^[A-Z][A-Z0-9]+-[0-9]+$`), `BRANCH_SEP="-"`, `MESSAGE_SEP=" "`,
  `normalize_task_tag`, `validate_task_tag`, `branch_with_task_tag`,
  `title_with_task_tag`, `commit_message_with_task_tag` (idempotent). All helpers
  are no-ops when the tag is falsy.
- **Persistence**: nullable `Workspace.task_tag` column (`db/models.py`) +
  Alembic migration `c3e5f7b9d1a2_workspace_task_tag.py`
  (`down_revision = b2d4f6a8c0e1`). `WorkspaceRepository.create` and
  `create_replacement_from` (retry lineage) carry the tag.
- **Schemas** (`api/schemas.py`): `WorkspaceTask.task_tag` and
  `PullRequestMonitorAdoptionRequest.task_tag` with validators delegating to
  `validate_task_tag`; `WorkspaceCreateRequest.task_tag` property;
  `WorkspaceResponse.task_tag` for visibility. `openapi.json` regenerated.
- **Service**: `workspaces_create.create_workspace_row` passes the tag;
  `workspace_create_payload_matches` keys on it (replay with a different tag
  conflicts). `pr_monitor_adoption` persists it.
- **Branch**: `_provision_local_branch_name` prepends `PROJ-123-` on the
  `feature_branch_pr` path only (sync/release untouched); provisioner passes
  `ws.task_tag`.
- **PR title**: `execution_flow.py` assembles `title_with_task_tag(...)`.
- **Commits**: post-agent commit prepends tag before `[:72]` truncation;
  PR-monitor `_commit_dirty_worktree` prepends via `_resolve_task_tag`
  (idempotent; reparent reusing `%B` and autofix retry inherit the tagged
  message). Agent prompt guidance line rendered in `build_agent_task_prompt` /
  `build_execution_prompt`.
- **CLI**: `--task-tag` on `workspace create` and `adopt-pr` with a Typer
  callback that rejects malformed tags locally.

## Focused checks run (AWF/CI owns the full gate)

- `ruff check .` → All checks passed.
- `ruff format --check .` → all formatted.
- `mypy` → Success, no issues in 365 source files.
- `python scripts/generate_openapi.py --check` → matches.
- `pytest tests/unit/common/test_task_tag.py --cov=awf.common.task_tag` →
  100% (36 stmts / 14 branch).
- Targeted suites green (435 + 158 passed): task_tag helper, schema,
  planning prompts, CLI create/adopt-pr, post-agent commit, monitor dirty-commit,
  adoption, idempotency, provisioner branch, repository replacement, api workspaces.
- `alembic upgrade head` / `downgrade -1` / re-`upgrade` against live Postgres → clean.

## Backward compatibility

Every helper and call site is a strict no-op when `task_tag` is absent
(`None`). Existing tests (untagged workspaces) pass unchanged. Column is
nullable; no backfill. Full repo suite, aggregate coverage gate, and merge
gating are owned by AWF/GitHub CI after the agent phase.
