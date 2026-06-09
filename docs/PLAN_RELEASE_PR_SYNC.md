# AWF Phase 1.5b — Automatic development → main release-PR sync

> Historical design note: this plan predates the current service-backed
> CLI/API/MCP workflow. References to retired helper scripts are preserved as
> implementation history, not current operator guidance.

## Problem

Today nothing creates the `development → main` PR, so it only happens
when a human remembers to. When it does happen, it accumulates days or
weeks of changes — a huge diff that CodeRabbit / Cursor / Greptile have
to re-read, dozens of comments, a big merge-conflict surface if main
has seen hotfixes, and a painful review cycle that usually ends in "just
merge it, I'll handle comments later."

The release-PR monitor already exists (`build_release_pr_monitor` in
`src/awf/runtime/release_pr_monitor.py`) — it runs the full comment-
resolution + CI + base-sync loop but refuses to auto-merge, posting
"ready to merge at commit X" when all gates are green. What's missing
is the **trigger** that creates the `development → main` PR in the first
place, and a cadence that keeps each sync small.

## Goals

1. Each aira repo (initially aira-agent + aira-web; extensible) gets a
   `development → main` PR opened automatically when there are commits
   on `development` that aren't on `main`.
2. The release-PR monitor drives the PR through comments + CI + base
   sync, then posts "ready to merge" — human clicks merge.
3. Cadence keeps release PRs small so bot reviewers see a narrow diff
   and don't pile up noise.
4. Idempotent: if a release PR is already open, don't open a second one
   — let the existing monitor pick up new commits on next poll.

## Non-goals (explicit)

- **No auto-merge on main.** The `auto_merge=False` flag in
  `MonitorConfig` is non-negotiable here.
- **No hot-patching.** If main needs an urgent fix, humans do the usual
  branch-from-main → PR workflow. The release-PR is for the steady-
  state trunk sync only.
- **No multi-repo atomic releases.** Each repo's dev→main is independent;
  if aira-agent and aira-web need to ship together, the human orders
  the merges.
- **No release-notes generation.** Out of scope for this iteration —
  the PR body carries a "list of merged PRs since last release PR" as a
  starting point; richer changelogs are a follow-up.

## Cadence — recommendation + tradeoffs

The user suggested "quite frequently". Concrete options:

| Cadence | Tradeoffs |
|---|---|
| **On-push (webhook-triggered)** — when any PR merges to `development`, check if a release-PR is already open; if not, open one. | Real-time, wastes no polls. Requires a webhook endpoint on AWF + GitHub webhook config on both repos. Infra lift. |
| **Every 1 h** | 24 new PRs/day worst case if commits are steady. Probably too noisy given CI cost + bot comments per PR. |
| **Every 4 h** | 6 PRs/day worst case. Each PR averages ≤ a handful of commits. Bot review volume per PR is bounded. Good balance for MVP. |
| **Every 12 h** (2× daily) | Predictable: morning + evening sync. Decent diff size for a solo dogfooder. |
| **Daily** | 1 PR/day, large enough to sometimes hit the "too many comments" pain. Simplest cadence. |

**Recommendation for MVP: every 4 hours** via a scheduled AWF workspace
dispatch. Extensible to webhook-triggered (on-merge-to-development) in
Phase 2 once we have confidence. Also add a **minimum-diff threshold**
— don't open a release PR if there are fewer than N commits on
`development` ahead of `main` (default N = 1, so any non-empty diff
triggers; tunable to N = 3 if you want to batch more).

## Approach

### Core idea: the task kind `sync_release_pr`

`sync_release_pr` is a real `TaskKind`. The earlier `monitor_release_pr`
kind is deprecated and removed — monitoring an existing release/manual
PR now goes through the generic PR-adoption flow with `auto_merge=false`,
which selects `build_release_pr_monitor`.

```python
class TaskKind(StrEnum):
    feature_branch_pr = "feature_branch_pr"   # everyday coding-agent PR
    sync_release_pr = "sync_release_pr"        # open/reuse source→target release PR
    sync_feature_pr = "sync_feature_pr"        # adopt an existing feature PR
```

A `sync_release_pr` workspace is short-lived and semantically distinct
from `feature_branch_pr`:

- **No coding agent.** No `running` state. No feature branch.
- **Input**: repo_url, source branch (default `development`), target
  branch (default `main`).
- **Flow**:
  1. `provisioning` — clone mirror, fetch origin.
  2. **Check: commits ahead?** `git rev-list --count origin/target..origin/source`. If 0, transition directly to `completed` with `reason_code=NO_CHANGES_TO_SYNC`.
  3. **Check: open release PR already?** Query GitHub:
     `gh pr list --repo <slug> --base <target> --head <source> --state open --json number,url`.
     If one exists, **do not** create a second one — instead, transition to `monitoring_pr` and attach that existing PR's number. The release-PR monitor (already implemented) picks up new commits via its normal poll.
  4. **Else, open the PR**: `gh pr create --base <target> --head <source> --title "release: development → main (<short-sha>)" --body "<auto-generated>"`. The body is auto-generated from the list of PRs merged into `development` since the last release PR merged to `main`: `git log <last-release-sha>..HEAD --first-parent --grep "Merge pull request"` or equivalent. If we can't compute "last release sha", fall back to listing commits since main's tip.
  5. Transition to `monitoring_pr`. The existing release-PR monitor runs with `auto_merge=False`.

- **Terminal states**: `completed` (either no-op, or monitor ran and posted "ready to merge"), `failed` (any step erred).

### What's shared with existing code

- `build_release_pr_monitor(...)` already exists — just invoke it.
- `WorkspaceStatus.monitoring_pr` state + state machine transitions already exist.
- `PullRequestMonitorRunner.run(...)` — already handles the comment + CI + base-sync cycle.
- `MonitorConfig(auto_merge=False)` — the one knob that prevents auto-merge.

### What's new (the actual implementation scope)

| Surface | Change |
|---|---|
| `src/awf/db/enums.py` | `TaskKind.sync_release_pr = "sync_release_pr"` |
| `src/awf/runtime/release_pr_sync.py` | NEW module. `async def sync_release_pr(workspace_id: str, ...)` — does steps 2–4 above; transitions workspace to `monitoring_pr`; then delegates to `build_release_pr_monitor(...).run(...)`. |
| `src/awf/control/executor.py` or a new `src/awf/control/sync_executor.py` | Dispatch: when a workspace's `task_kind == sync_release_pr`, the driver routes to `release_pr_sync()` instead of the full `WorkspaceExecutor.execute()`. Simplest: add a `task_kind` check at the top of `execute()` and branch. |
| `scripts/run_awf.py` | Accept `task_kind` from the task spec JSON. If `sync_release_pr`, provision a lightweight workspace (no worktree for a feature branch — we operate directly on the mirror for the PR-open step; the release-PR monitor uses the mirror's existing fetch path). |
| Task spec schema | New field `task_kind` in the JSON. `source_branch` + `target_branch` optional; default `development` + `main`. |
| **Scheduler** | NEW. See next section. |

### The scheduler

Two layers. For MVP:

1. **`scripts/schedule_release_pr.py`** — one-shot script that takes a
   repo_url + source/target branches, constructs the task spec, and
   invokes `run_awf.py` with it. No daemon; fires and exits.

2. **Host cron entry** (manual setup, documented in README):
   ```
   0 */4 * * *  cd ~/Projects/agent-workspace-fabric && .venv/bin/python scripts/schedule_release_pr.py --repo git@github.com:dimileeh/aira-agent.git >> ~/.awf/release-pr.log 2>&1
   0 */4 * * *  cd ~/Projects/agent-workspace-fabric && .venv/bin/python scripts/schedule_release_pr.py --repo git@github.com:dimileeh/aira-web.git >> ~/.awf/release-pr.log 2>&1
   ```
   Every 4 hours, for each repo, fire a `sync_release_pr` workspace.

3. **In-AWF scheduler** (Phase 2, out of scope here): a long-running
   background task inside the AWF control plane that reads a scheduler
   config table and dispatches release-PR syncs on cadence without
   needing host cron. Cleaner ops, but adds complexity we don't need
   for MVP.

### Idempotency guard

The scheduler may fire while a previous `sync_release_pr` workspace is
still active (e.g., the monitor loop for an existing open release PR
is still running). To avoid piling workspaces:

- At the top of `release_pr_sync()`, check the DB for an existing
  workspace with `task_kind=sync_release_pr` + `repo_url=<this>` +
  `status IN (provisioning, monitoring_pr)`. If one exists, exit early
  with `reason_code=ALREADY_SYNCING`.
- Human can manually clean up stuck syncs if needed.

This lets the cron fire every 4 h but only actually creates a new
workspace when the previous one has terminated.

## Wiring through the existing plumbing — what we DON'T need

- ❌ New state machine states — `provisioning → monitoring_pr → completed/failed` uses existing transitions.
- ❌ New Docker image — same agent-runtime image (the `sync_release_pr` workspace doesn't run a coding CLI initially, but will when the monitor needs to invoke one for comment/conflict resolution, so image stays).
- ❌ New adapter — the monitor uses the existing codex/claude/gemini adapters for comment resolution.
- ❌ Schema changes beyond the `TaskKind` enum value.

## Tests

Unit tests:
- `test_sync_release_pr_no_commits_ahead_completes_noop`
- `test_sync_release_pr_opens_pr_with_correct_base_and_head`
- `test_sync_release_pr_reuses_existing_open_pr_instead_of_duplicating`
- `test_sync_release_pr_with_active_workspace_exits_already_syncing`
- `test_sync_release_pr_body_lists_merged_feature_prs_since_last_release`

Integration tests (fake gh + fake command runner):
- `test_end_to_end_sync_open_pr_then_enters_monitoring_pr`
- `test_monitor_runs_with_auto_merge_false_and_posts_notify_human_on_clean`

No Playwright — no UI in this flow.

## Cost + blast radius

- **CI cost**: 6 release PRs/day × ~20 min each = ~2 CI-hours/day
  extra across both repos. Manageable.
- **Bot-comment cost**: CodeRabbit charges per comment usually small
  per PR. 6 PRs with tiny diffs each should be cheaper than 1 weekly
  PR with a massive diff that hits comment caps.
- **Worst-case bug**: AWF opens a malformed release PR. Worst
  consequence is a PR sitting open that a human can close. No merge
  happens without human clicking, so no bad code lands on `main`.

## Open questions (decide before implementation)

1. **Cadence**: confirm every 4h, or different? (see tradeoff table
   above).
2. **Minimum-diff threshold** (commits ahead before opening a PR):
   default 1, or 3, or a "wait for at least one merged-PR-to-development
   before opening"?
3. **Release PR title/body conventions**: any preferred format? My
   suggestion: title `release: development → main (<short-sha>)`, body
   enumerates merged PRs since last release.
4. **Auto-close stale release PRs**: if a release PR sits open for
   > N days without being merged, should AWF close it and open a fresh
   one against the current dev HEAD? My recommendation: yes, N = 7,
   because at that point the diff has drifted enough that humans will
   want a fresh view. Out of scope for MVP but worth flagging.

## Estimate

~2-3 engineering days:
- Day 1: `TaskKind.sync_release_pr` + `release_pr_sync.py` + unit tests.
- Day 2: `schedule_release_pr.py` + executor dispatch + integration tests.
- Day 3: Real-repo smoke + cron setup + README docs.
