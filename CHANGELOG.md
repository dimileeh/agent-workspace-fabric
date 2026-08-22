# Changelog

## Unreleased

- Fixed Cursor's default runtime policy to use portable `auto` with no generic
  AWF effort mapping. Cursor Auto routing profiles (Cost, Balance, Intelligence)
  remain provider/team policy or explicit official parameterized model
  selectors; AWF no longer hard-codes the removed `sonnet-4-thinking` slug.
- Fixed cancellation of first-run/adopted PR monitors so a monitor coroutine
  originally tracked as a ready execution stops immediately after durable
  workspace cancellation instead of retrying against a removed runtime.
- Fixed PR-monitor verdict parsing for balanced Markdown-emphasized
  `AWF-VERDICT` lines while keeping malformed wrappers and nested containers
  fail-closed.
- Fixed local worker/agent Git configuration loss across Docker Desktop
  rewrites and service restarts with immutable, content-addressed config
  bundles that preserve worker relative includes and native-Linux agent
  ownership without exposing the worker include graph to agents; stale bundles
  are reaped only after live-reference checks.
- Fixed unrecoverable Remonitor requests: incomplete failed workspaces that
  lack persisted recovery metadata now direct operators to Retry.
- **BREAKING: `auto_merge` now defaults to `false` everywhere.** It is one
  uniform, opt-in setting that behaves identically for `awf workspace create`
  and `awf workspace adopt-pr`, resolved once at provision time. `task_kind`
  (including `sync_release_pr`) no longer forces or affects auto-merge, and the
  persisted `workspace.auto_merge` flag is the single authority for monitor
  selection (`true` → squash-merge on green; `false` → report readiness without
  merging). Feature and adopted PRs no longer auto-merge unless you pass
  `--auto-merge` (`auto_merge: true`). Configure a repo-wide default and
  per-base-branch overrides under `monitor.auto_merge` in `workspace.yml`
  (precedence: per-task flag → `by_base_branch[<base>]` → `default` → off).
  Existing workspaces persisted with `auto_merge=true` are grandfathered
  untouched (no data migration).
- Added companion `environment_secrets` for env-backed companion service
  secrets, while clarifying that literal companion `environment` values reject
  Docker Compose interpolation.
- Added companion `compose_up_timeout_seconds` so slow cold-cache companion
  Docker builds can raise the workspace Compose startup timeout.
- Canonicalized the first-run quickstart and added upgrade documentation.
- Improved DX-oriented CLI pretty output for profile preview and Core release
  readiness.
- Added local console discovery to smoke reports through the default
  `http://localhost:3000` URL.

## 0.1.0

- Initial local AWF Core MVP with CLI, REST API, MCP primitives, profile-driven
  workspace execution, local service bootstrap, PR monitor flows, and smoke
  diagnostics.
