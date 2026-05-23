# RELEASE_SYNC_TASK_KIND_PLAN

## Problem statement & scope

Operator resolution of the hunter-report release task-kind bug:

1. `monitor_release_pr` is redundant and must be **deprecated** as a public task
   kind. Monitoring an existing PR (feature→development, branch→branch, or
   development→main/master) should go through AWF's generic PR-adoption path with
   `auto_merge=false`, which already selects the release/manual monitor.
2. `sync_release_pr` must become a **real** task kind: create or reuse a
   source→target release PR, then monitor it with release/manual behavior and
   never auto-merge. No coding agent and no feature PR.
3. Public task-kind admission must be **hardened** so unknown/deprecated kinds
   never fall through to feature provisioning/execution.
4. Remove the **duplicate** `_GITHUB_PULL_HEAD_REF` regex in
   `src/awf/node/git_manager.py`.

Generic AWF core behavior only — no hard-coded repositories or branch names. No
scheduler/cron system. Rely on `.awf/workspace.yml` for validation; no
prompt-specific validation commands.

## Requirements checklist

| # | Requirement |
|---|-------------|
| 1 | Remove `monitor_release_pr` from `TaskKind` and public docs |
| 1 | Legacy `monitor_release_pr` fails fast with a clear deprecated message; never runs as feature work |
| 2 | `sync_release_pr` runs no coding agent and creates no feature PR |
| 2 | Check whether source is ahead of target; default source `development`, default target `repo.base_branch` (falls back to `main`); `master`/explicit target supported |
| 2 | No commits ahead → complete cleanly with a clear reason/event (`NO_CHANGES_TO_SYNC`) |
| 2 | Reuse an existing open source→target PR; otherwise create one with a clear title/body |
| 2 | Transition into `monitoring_pr` with PR metadata recorded and `auto_merge=false`/release monitor behavior |
| 2 | Preserve the generic PR-adoption monitor path for arbitrary existing PRs |
| 3 | REST/MCP workspace creation rejects arbitrary unknown task kinds |
| 3 | Supported direct kinds = `feature_branch_pr` + `sync_release_pr`; `sync_feature_pr` remains adoption-only |
| 3 | Legacy/unsupported values never fall through to feature provisioning/execution |
| 3 | CLI/API/MCP surfaces expose/ document task-kind selection |
| 4 | `_GITHUB_PULL_HEAD_REF` defined exactly once |

## Design / implementation steps

- **`src/awf/db/enums.py`** — drop the `monitor_release_pr` enum member; keep
  `feature_branch_pr`, `sync_release_pr`, `sync_feature_pr` and refresh the
  `sync_release_pr` docstring to the source→target model.
- **`src/awf/runtime/release_pr_sync.py` (new)** — pure, DB-free helpers backing
  the handoff: `count_commits_ahead` (fetch + `rev-list --count`),
  `find_or_create_release_pr` (reuse open source→target PR or `gh pr create`),
  `prepare_release_pr_sync` (returns `ReleasePrSyncNoOp | ReleasePrSyncResult`),
  `release_pr_title`/`release_pr_body`, `ReleasePrSyncError`.
- **`src/awf/common/github_client.py`** — add `GitHubClient.create_pull_request`
  (`gh pr create`), reusing existing `RepoRef`,
  `list_open_pull_requests_for_branch`, `fetch_pull_request_adoption_metadata`,
  `parse_github_pull_request_url`, `PullRequestAdoptionMetadata`.
- **`src/awf/control/executor.py`** — replace the `sync_feature_pr`-only branch
  with `_dispatch_non_feature_task_kind`: `sync_feature_pr`→existing handoff,
  `sync_release_pr`→new `_handoff_sync_release_pr_monitor`, `monitor_release_pr`→
  fail fast (`policy_failure`, `DEPRECATED_TASK_KIND`), unknown→fail fast
  (`policy_failure`, `UNSUPPORTED_TASK_KIND`), `feature_branch_pr`→return `False`
  so the coding-agent path continues. Add the release handoff (no-op completion;
  open/reuse PR; record metadata; `running→validating→monitoring_pr`), a shared
  `_build_handoff_pr_monitor`, no-op completion, and source/target branch +
  policy-metadata helpers.
- **`src/awf/node/provisioner.py`** — `sync_release_pr` provisions a
  `release-sync/<id>` local branch, checks out the source branch (default
  `development`), and sets the source branch as the remote push branch.
- **`src/awf/api/schemas.py`** — `PUBLIC_DIRECT_CREATE_TASK_KINDS =
  {feature_branch_pr, sync_release_pr}`; `WorkspaceTask.kind` field validator
  rejects deprecated `monitor_release_pr`, adoption-only `sync_feature_pr`, and
  unknown kinds; add optional `WorkspaceRepo.source_branch`.
- **`src/awf/service/workspaces.py`** —
  `_assert_supported_direct_create_task_kind` defense-in-depth re-check;
  `_effective_auto_merge` forces `auto_merge=False` for `sync_release_pr`;
  `workspace_create_task_policy_snapshot` writes a `release_sync` policy block
  (`source_branch`, `target_branch`); drop `monitor_release_pr` from the retry
  preserve set.
- **`src/awf/control/worker.py`, `src/awf/service/provider_recovery.py`** — drop
  `monitor_release_pr` from the preserved-remote-push-branch task-kind sets.
- **`src/awf/node/git_manager.py`** — remove the duplicate
  `_GITHUB_PULL_HEAD_REF` definition.
- **`src/awf/cli/main.py`** — `workspace create` gains `--task-kind` and
  `--source-branch`.
- **`src/awf/mcp/server.py`** — update the `task_kind` field description.
- **Docs/schema** — `src/awf/runtime/pr_monitor.py`, `docs/PLAN_PR_MONITOR.md`,
  `docs/PLAN_RELEASE_PR_SYNC.md`, `skills/awf-scheduler/SKILL.md`, `openapi.json`
  aligned to the new model.

No new state-machine transitions: `running→validating`,
`validating→completed` (no-op), and `validating→monitoring_pr` (handoff) already
exist.

## Tests (TDD)

- `tests/unit/runtime/test_release_pr_sync.py` (new) — ahead-count parse,
  fetch/rev-list/non-numeric failures, find-or-create reuse vs create,
  unparseable-URL error, prepare no-op/create/reuse, title/body.
- `tests/unit/control/test_executor_error_paths.py` — legacy
  `monitor_release_pr`→`DEPRECATED_TASK_KIND` (no validation/gh calls); unknown→
  `UNSUPPORTED_TASK_KIND`; release handoff no-op completion; create→
  `monitoring_pr` with `auto_merge=False` + open MergeCandidate; reuse; invalid
  repo URL; fetch failure; stale-skip; recheck blocks monitor run; helper
  resolution.
- `tests/unit/api/test_schema_coverage_edges.py` — enum drops
  `monitor_release_pr`; accepts public kinds; rejects unknown/deprecated/direct
  `sync_feature_pr`; optional `source_branch`; create-request rejects deprecated.
- `tests/unit/node/test_provisioner.py` — `sync_release_pr` checks out source +
  `release-sync/<id>`; `feature_branch_pr` keeps the prefix branch.
- `tests/unit/node/test_git_manager.py` — `_GITHUB_PULL_HEAD_REF` defined once.
- `tests/unit/common/test_github_client.py` — `create_pull_request` argv/URL +
  error.
- `tests/unit/service/test_workspace_retry.py`,
  `tests/unit/service/test_workspaces_observability.py` — new kind set; retry/
  observability; `sync_feature_pr` seeded via adoption, not direct create.

## Verification commands & pass criteria

Focused only — AWF/GitHub CI owns the full suite, coverage gates, and
`.awf/workspace.yml` validation post-agent.

```bash
uv run ruff format <changed files>
uv run ruff format --check <changed files>   # must report nothing to reformat
uv run ruff check <changed src files>
uv run pytest -q \
  tests/unit/runtime/test_release_pr_sync.py \
  tests/unit/control/test_executor_error_paths.py \
  tests/unit/api/test_schema_coverage_edges.py \
  tests/unit/node/test_provisioner.py \
  tests/unit/node/test_git_manager.py \
  tests/unit/common/test_github_client.py \
  tests/unit/service/test_workspace_retry.py \
  tests/unit/service/test_workspaces_observability.py
```

Pass criteria: `ruff format --check` reports nothing to reformat; focused
`ruff`/`pytest` are green.

## Assumptions / Changes

- Mode is **salvage continuation**: AWF restored a near-complete implementation
  diff from a prior run that timed out at the post-agent commit step (only the
  `ruff format --check` pre-commit hook failed; `ruff check` had passed). Code
  and tests were recovered; this plan documents the implemented design, and the
  execution phase applies formatting, runs the focused checks, and adds the
  required `plans/` process docs.
- Default source `development`; default target `repo.base_branch` (falls back to
  `main`); `master`/explicit target via the `release_sync` policy block.
- `auto_merge=false` is the single switch selecting `build_release_pr_monitor`
  (release/manual behavior), enforced at the service boundary for
  `sync_release_pr`.
