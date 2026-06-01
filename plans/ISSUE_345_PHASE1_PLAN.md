# Issue #345 Phase 1 — Forge provider-abstraction seam + detection (GitHub-only)

Tracking issue: GitHub #345 (BitBucket provider support) — **Phase 1 only**.
Workspace: `ws_7ccab9fb002345c48b70e305`. Full design contract:
`docs/awf-plans/ws_7ccab9fb002345c48b70e305.md`.

## Goal (locked — do not expand)

"Make the change easy, then make the easy change." Extract a provider-neutral
`ForgeClient` interface + forge **detection** so Phase 2 can drop in a
`BitBucketClient` without touching consumers. **GitHub stays the only
implementation.** A BitBucket repo must be *detected* and then *fail fast* with
a clear reason-coded error (`FORGE_NOT_SUPPORTED`) — never crash, never silently
mis-route to GitHub. **Zero behavior change for GitHub repos.**

### Locked decisions
1. Phase 1 = abstraction seam + detection only. No BitBucket REST/httpx client,
   no BitBucket auth. BitBucket → detected → fail fast with `FORGE_NOT_SUPPORTED`.
2. workspace.yml field is **`forge:`** (`github | bitbucket | auto`, default
   `auto`). Precedence: explicit `forge:` > URL-host detection
   (github.com / bitbucket.org) > default `github`. Resolved value persisted ONCE
   in `resolved_profile.forge`; later stages read it, they do NOT re-resolve.

## Changes (TDD: tests first)

1. `src/awf/db/enums.py` — add `ForgeKind = Literal["github", "bitbucket"]`.
2. `src/awf/common/forge.py` (new) — `ForgeClient` Protocol (runtime_checkable,
   structural mirror of the 10 public `GitHubClient` methods), `make_forge_client`,
   `ensure_forge_supported`, `ForgeNotSupportedError`,
   `FORGE_NOT_SUPPORTED_REASON_CODE`, `detect_forge_from_url`, ASCII diagram.
   `BranchOpenPullRequestResolver` stays a separate collaborator (documented).
3. `src/awf/common/github_client.py` — forge-aware `RepoRef`: `forge` field,
   host detection in `from_url`, host-aware `https_url`/`ssh_url`/`clone_url_like`.
   No Protocol/factory here (1500-LOC guard).
4. `src/awf/profiles/models.py` — `WorkspaceProfile.forge: ForgeKind | "auto" = "auto"`.
5. `src/awf/profiles/resolver.py` — `repo_url` param; stamp resolved concrete forge
   (explicit > URL host > github) onto the profile via `model_copy`.
6. `src/awf/node/provisioner.py`, `src/awf/control/executor/helpers.py` — pass
   `repo_url=ws.repo_url`.
7. `src/awf/service/worker.py`, `src/awf/control/executor/monitor_handoff.py` —
   build `gh` via `make_forge_client(resolved_forge, runner)`.
8. `src/awf/control/executor/execution_flow.py` — early `ensure_forge_supported`
   gate placed BEFORE non-feature dispatch (covers feature_branch_pr,
   sync_release_pr, sync_feature_pr uniformly), reading the persisted
   `resolved_profile.forge`. Fails fast before any agent run / push / `gh pr
   create` / monitor-handoff `gh` build, so an unsupported forge can never
   strand a workspace on an uncaught error deeper in a handoff.
9. Consumer type hints `GitHubClient` → `ForgeClient` in
   `runtime/pr_monitor_runner/{runner,types}.py`, `runtime/release_pr_monitor.py`,
   `runtime/release_pr_sync.py` (type-only; `GitHubClientError` untouched).
10. `src/awf/service/doctor/reasons.py` — add `FORGE_NOT_SUPPORTED` entry; regen
    `docs/REASON_CATALOG.md`. Regenerate `openapi.json` (WorkspaceProfile embeds in
    API schemas).

## NOT in scope (Phase 1)
- BitBucket API client / read or write path (Phase 2).
- BitBucket auth/credentials/token plumbing (Phase 2/3).
- Self-hosted GHE / BitBucket Data Center host detection.
- Replacing the `gh` CLI for GitHub.
- git clone/push transport changes in `node/git_manager`.
- Moving `BranchOpenPullRequestResolver` onto the Protocol.

## Validation
Focused tests on touched modules, then the full local gate
(`ruff check`, `ruff format --check`, `mypy`, `pytest`) plus the reason-catalog
and OpenAPI drift gates. AWF/GitHub CI own the 99% coverage gate after the agent
exits.
