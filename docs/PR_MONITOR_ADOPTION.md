# PR Monitor Adoption

Use PR monitor adoption when a GitHub pull request already exists and the
operator wants AWF to take over review, check, freshness, validation, and merge
monitoring without rerunning the original coding agent.

The supported adoption surfaces are:

- REST: `POST /v1/workspaces/adopt-pr`
- CLI: `awf workspace adopt-pr`
- MCP: `awf_adopt_pull_request_monitor`

The console is an inspection and recovery surface for adopted monitor workspaces.
Start adoption through REST, CLI, or MCP, then use the console to inspect and
recover it.

## Preflight

Start the API and worker through the local service bootstrap, then confirm the
control-plane token and GitHub token are visible to the process that will adopt
the PR:

```bash
awf service status --provider github --format pretty
gh auth status
```

REST and protected CLI/MCP-backed calls use `AWF_API_TOKEN` as the AWF control
plane bearer token. GitHub operations use `AWF_GITHUB_TOKEN`; `GH_TOKEN` and
`GITHUB_TOKEN` are accepted fallbacks. Do not paste token values into commands,
logs, PR comments, or screenshots.

The GitHub credential must be able to read PR metadata, read and write review threads
and issue comments, read checks, push updates to the PR branch when the monitor
has to fix comments or CI failures, and merge only when the adopted workspace
has `auto_merge=true`. Fine-grained tokens, classic tokens, GitHub App tokens,
and `gh` keyring auth expose those capabilities differently, so use
`awf service status --provider github` and `gh auth status` as the readiness
check instead of documenting a secret value.

## Adopt Through CLI

Adopt by repo slug plus PR number:

```bash
awf workspace adopt-pr \
  --repo owner/repo \
  --pr 123 \
  --auto-merge \
  --reason "attach AWF to existing PR"
```

Or adopt by full PR URL:

```bash
awf workspace adopt-pr \
  --pr-url https://github.com/owner/repo/pull/123 \
  --no-auto-merge \
  --initial-review-grace-period-seconds 900
```

`--auto-merge` is the default. It lets the monitor merge after comments,
checks, freshness, validation provenance, merge queue policy, and the initial
review grace window are clean. `--no-auto-merge` keeps AWF monitoring and
repairing the PR, but a human must merge it.

`initial_review_grace_period_seconds` is optional. Omit it to use the resolved
profile monitor policy; the profile default is 900 seconds. Set it to `0` only
for explicit fast-path tests.

Retry with the same raw grace override used by the first adoption request. An
omitted/null value means "use the profile policy" and is stored separately from
an explicit `900`, even when the resolved profile default is also 900 seconds.

## Troubleshooting `Not Found` from `adopt-pr`

If `awf workspace adopt-pr` returns `Not Found` before any workspace appears:

- Verify the CLI is targeting the same API root as your REST calls.
- For plain API roots, `AWF_BASE_URL` can be `http://host:8000`, `http://host:8000/`,
  `http://host:8000/v1`, or `http://host:8000/v1/`.
- For reverse-proxy setups, set `AWF_BASE_URL` to the proxy mount
  (for example, `http://host:8000/awf` or `http://host:8000/awf/v1`).
- `AWF_CLI_BASE_URL` is still honored for compatibility, but is deprecated; use
  `AWF_BASE_URL` for new scripts.
- When neither variable is set, the host CLI derives
  `http://localhost:${AWF_API_HOST_PORT:-8000}`.
- If your base URL already ends in `/v1`, the CLI no longer doubles the prefix.
  In other words, `.../v1` + `POST /v1/workspaces/adopt-pr` now resolves to
  `.../v1/workspaces/adopt-pr`, not `.../v1/v1/workspaces/adopt-pr`.
- If a direct REST request succeeds with the same command payload, compare the
  two URLs in logs; the CLI now emits lines like
  `error: POST <normalized_url> -> HTTP 404` before response output to help
  spot route-level mismatches quickly. URL debug context redacts token-like query
  values to avoid secret leakage.

## Adopt Through REST

REST adoption requires `Authorization: Bearer $AWF_API_TOKEN` but does not
require a caller-supplied `Idempotency-Key`. AWF derives deterministic repo/PR
idempotency from the normalized repository identity and PR number.

```bash
curl -X POST "http://localhost:8000/v1/workspaces/adopt-pr" \
  -H "Authorization: Bearer $AWF_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "repo_slug": "owner/repo",
    "pr_number": 123,
    "auto_merge": true,
    "initial_review_grace_period_seconds": 900,
    "reason": "attach AWF to existing PR"
  }'
```

The response includes `workspace_id`, PR refs and SHAs, `monitor_policy`,
`validation_provenance`, `status_url`, `events_url`, `logs_url`, and
`attached_existing`.

## Adopt Through MCP

Use the MCP tool when an agent orchestrator should make a typed control-plane
call instead of shelling out:

```json
{
  "tool": "awf_adopt_pull_request_monitor",
  "arguments": {
    "repo_slug": "owner/repo",
    "pr_number": 123,
    "auto_merge": true,
    "initial_review_grace_period_seconds": 900,
    "reason": "attach AWF to existing PR"
  }
}
```

The MCP response mirrors `PullRequestMonitorAdoptionResponse`. Structured
terminal PR errors include `PR_ALREADY_CLOSED`, `PR_ALREADY_MERGED`,
`PR_NOT_FOUND`, `INVALID_GITHUB_REPO`, `PR_ADOPTION_INPUT_REQUIRED`,
`PR_METADATA_FETCH_FAILED`, `PR_METADATA_INVALID`, and
`PR_ADOPTION_POLICY_CONFLICT`.

## Idempotency And Retries

AWF normalizes the GitHub repository identity and PR number, then stores a
deterministic repo/PR adoption key. A repeat adoption for the same repo/PR and
the same monitor policy returns the existing live or otherwise resumable
workspace with `attached_existing=true`; it does not fetch metadata again or
create another monitor workspace.

Policy changes on an existing live adoption return
`PR_ADOPTION_POLICY_CONFLICT`. That includes changes to `repo_url`, `agent`,
`model`, `effort`, `profile_ref`, inline profile, `auto_merge`, or
`initial_review_grace_period_seconds`.

For idempotent retries, omitted/null and explicit `900` are different adoption
policies. If the first request omitted the grace override, later REST or MCP
retries must also omit it or send `null`; if the first request set `900`,
retries must set `900`.

The same raw policy rule applies to agent selection and agent overrides, but
`agent` has a default rather than null/no-override semantics. For retries,
omitting `agent` requests the default `codex` agent policy; it is not a no-op
for conflict detection, and conflicts with an existing live adoption for another
agent. For overrides, request `model` and `effort` inputs persist as
`agent_model` and `agent_effort` in `task_policy`; omitting `model` or `effort`
requests the default/no-override policy, so a replay that omits those fields
conflicts with an existing live adoption pinned to explicit `agent_model` or
`agent_effort` values from prior `model` or `effort` inputs.

Terminal adoption retries: destroyed, destroying, cancelled, failed, completed,
and superseded adoption rows are not reused as live monitor attachments. AWF
fetches fresh PR metadata, creates a fresh monitor workspace with
`attached_existing=false`, supersedes the previous canonical adoption row when
it still owns the deterministic key, and records previous terminal adoption
lineage on the new workspace. If stale task idempotency data remains from an
older adoption, AWF allocates a generated task key so the fresh monitor is not
linked back to stale task scope or title.

Closed or merged GitHub PRs are rejected before workspace creation with
structured errors such as `PR_ALREADY_CLOSED` and `PR_ALREADY_MERGED`.

## Inspect The Adopted Monitor

Open the console after adoption and select the returned workspace. The useful
panels are workspace details, events, operations, logs, validation provenance,
runtime, and the Merge Queue view. The details and timeline show the adoption
event, PR URL, monitor policy, current status, and recovery operations. The log
panel shows durable monitor streams after the worker starts the PR monitor.

CLI inspection:

```bash
awf workspace show ws_123 --format pretty
awf workspace events ws_123 --limit 50 --format pretty
awf workspace operations ws_123 --limit 25 --format pretty
awf workspace logs ws_123 --format pretty
awf workspace log ws_123 agent.stdout --offset 0 --limit-bytes 65536
```

REST inspection:

Route equivalents for automation include `GET /v1/workspaces/{workspace_id}`,
`GET /v1/workspaces/{workspace_id}/events`, `GET /v1/workspaces/{workspace_id}/operations`,
`GET /v1/workspaces/{workspace_id}/logs`, `GET /v1/workspaces/{workspace_id}/validation`,
and `GET /v1/merge-queue`.

```bash
curl -H "Authorization: Bearer $AWF_API_TOKEN" \
  "http://localhost:8000/v1/workspaces/ws_123"
curl -H "Authorization: Bearer $AWF_API_TOKEN" \
  "http://localhost:8000/v1/workspaces/ws_123/events?limit=50"
curl -H "Authorization: Bearer $AWF_API_TOKEN" \
  "http://localhost:8000/v1/workspaces/ws_123/operations?limit=25"
curl -H "Authorization: Bearer $AWF_API_TOKEN" \
  "http://localhost:8000/v1/workspaces/ws_123/logs"
curl -H "Authorization: Bearer $AWF_API_TOKEN" \
  "http://localhost:8000/v1/workspaces/ws_123/validation"
curl -H "Authorization: Bearer $AWF_API_TOKEN" \
  "http://localhost:8000/v1/merge-queue"
```

MCP inspection tools:

- `awf_get_workspace`
- `awf_list_workspace_events`
- `awf_list_workspace_operations`
- `awf_list_workspace_logs`
- `awf_read_workspace_log`
- `awf_list_workspace_validation`
- `awf_list_workspace_stale_reasons`
- `awf_list_merge_queue`

## Recovery Operations

Use recovery controls when the monitor is stale, stranded, blocked on validation
freshness, or needs an operator-initiated refresh. Mutating controls require
auditable reasons and, where the REST API requires it, an `Idempotency-Key`.

CLI supports remonitor and retry:

```bash
awf workspace remonitor ws_123 \
  --idempotency-key remonitor-ws-123-001 \
  --reason "worker restarted"

awf workspace retry ws_123 --provider-readiness-override-reason "provider recovered"
```

REST exposes the broader recovery surface:

```bash
curl -X POST "http://localhost:8000/v1/workspaces/ws_123/remonitor" \
  -H "Authorization: Bearer $AWF_API_TOKEN" \
  -H "Idempotency-Key: remonitor-ws-123-001" \
  -H "Content-Type: application/json" \
  -d '{"reason": "worker restarted"}'

curl -X POST "http://localhost:8000/v1/workspaces/ws_123/refresh" \
  -H "Authorization: Bearer $AWF_API_TOKEN" \
  -H "Idempotency-Key: refresh-ws-123-001" \
  -H "Content-Type: application/json" \
  -d '{"reason": "refresh GitHub state"}'

curl -X POST "http://localhost:8000/v1/workspaces/ws_123/validate" \
  -H "Authorization: Bearer $AWF_API_TOKEN" \
  -H "Idempotency-Key: validate-ws-123-001" \
  -H "Content-Type: application/json" \
  -d '{"reason": "prove current head", "requested_tier": 2}'

curl -X POST "http://localhost:8000/v1/workspaces/ws_123/rebase" \
  -H "Authorization: Bearer $AWF_API_TOKEN" \
  -H "Idempotency-Key: rebase-ws-123-001" \
  -H "Content-Type: application/json" \
  -d '{"reason": "target branch advanced"}'
```

MCP recovery tools are `awf_remonitor_workspace`,
`awf_request_workspace_validation`, `awf_refresh_workspace`,
`awf_rebase_workspace`, and `awf_retry_workspace`.

## Mocked Local Demo

The docs-tested demo path validates adoption examples without mutating a live PR.
Run the mocked unit tests that exercise REST, CLI, and MCP adoption with
in-process metadata fetchers:

```bash
uv run --python 3.12 --extra dev pytest \
  tests/unit/api/test_pr_monitor_adoption.py \
  tests/unit/cli/test_cli.py::TestWorkspaceAdoptPr \
  tests/unit/mcp/test_mcp_server.py::TestToolRegistration::test_adopt_pull_request_monitor_tool_creates_adoption \
  tests/unit/mcp/test_mcp_server.py::TestToolRegistration::test_adopt_pull_request_monitor_tool_returns_terminal_pr_error_result \
  -q
```

Those tests prove the documented REST route, CLI command, MCP tool name, auth
header handling, policy flags, deterministic replay, and terminal PR errors
without needing a live GitHub PR. For broader first-run diagnostics that also
avoid live PR mutation, run `awf smoke run --mocked-local`.
