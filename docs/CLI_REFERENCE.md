# CLI Reference

## CLI Surface

The CLI is intentionally thin and JSON-first.

Start the API:

```bash
uv run --python 3.12 --extra dev awf serve --host 127.0.0.1 --port 8000
```

Run the provisioning worker:

```bash
uv run --python 3.12 --extra dev awf worker
```

Run the local MCP server for Claude Code, Codex, or another MCP client:

```bash
uv run --python 3.12 --extra dev awf mcp serve --env-file .env
```

The MCP command uses stdio, so stdout is reserved for the MCP protocol. See
[MCP Setup](MCP_SETUP.md) for copy-paste client configuration.

Local service mode uses a stable worker node id, `local`, so active rows
survive API/worker/migrate container rebuilds without becoming owned by a dead
container hostname. Multi-node deployments should set a unique
`AWF_WORKER_NODE_ID` per node; stale active-execution recovery remains scoped to
the current node id and does not recover rows owned by unrelated nodes.

Inspect local service settings and dependency status:

```bash
uv run --python 3.12 --extra dev awf service config
uv run --python 3.12 --extra dev awf service status
uv run --python 3.12 --extra dev awf service status --format pretty
```

Start local AWF Core and the web console:

```bash
uv run --python 3.12 --extra dev awf start
uv run --python 3.12 --extra dev awf start --console-port 3333
uv run --python 3.12 --extra dev awf start --headless
```

`awf start` starts the API, worker, database, and console through the same
bootstrap path as service mode. The console is published on
<http://127.0.0.1:3000> by default. Use `--headless` to skip the console, or
`--console-port` to change the localhost port.

`awf service status` reports `orphan_workspaces` and `workspace_cleanup` checks
alongside the existing API / DB / Docker / image / disk checks. It reads
Docker Compose labels for containers, networks, and volumes, and scans
`<work_dir>/git/worktrees/ws_*` for managed worktrees. Resources for active
workspaces are expected; completed workspaces still inside the service GC
retention window are reported as retained instead of unsafe. Resources tied to
missing workspace rows or terminal rows past retention are reported with
structured counts, examples, reason codes, and suggested follow-up actions.
The check returns structured `unavailable`/`unknown` warnings (rather than
raising) when Docker or the database is offline.

Run the AWF Core release-readiness gate:

```bash
uv run --python 3.12 --extra dev awf service readiness --format pretty
uv run --python 3.12 --extra dev awf service release-readiness --format pretty
```

This is separate from local health. It includes historical PRD SLO metrics,
failure taxonomy, demo-project evidence, doctor diagnostics, provider
readiness, and cleanup posture. A healthy local stack can still fail this gate
when the 168-hour SLO window lacks enough passing evidence.

Inspect the local service Compose logs without writing Docker commands:

```bash
uv run --python 3.12 --extra dev awf service logs
uv run --python 3.12 --extra dev awf service logs --tail 200 --service worker
uv run --python 3.12 --extra dev awf service logs --follow --service api --service worker
```

`awf service logs` is a read-only wrapper around
`docker compose logs` from the AWF install/source root. By default it tails the
`api` and `worker` services. Repeat `--service` to select `api`,
`worker`, `migrate`, or `postgres`.

Run a DX smoke proof from any project:

```bash
uv run --python 3.12 --extra dev awf smoke run
uv run --python 3.12 --extra dev awf smoke run --mocked-local --format pretty
```

`awf smoke run` validates service readiness, auth/provider readiness, profile
preview, validation commands, workspace request shape, PR/monitor path (mocked
in `--mocked-local` mode), and console links. It is safe to run repeatedly and
produces structured reason codes with next actions on failure. Without
`AWF_CONSOLE_URL`, it probes `http://localhost:3000` as the default local
console. See
[SMOKE_COMMAND.md](SMOKE_COMMAND.md) for the full phase reference.

Run one target-branch reconciliation pass:

```bash
uv run --python 3.12 --extra dev awf service reconcile-target \
  --repo-url git@github.com:owner/repo.git \
  --branch development
```

The service worker also invokes this reconciliation hook after a monitored PR
reaches `completed`. The first resolver is Python/Alembic-specific: if several
merged workspace PRs leave the integrated target branch with multiple Alembic
heads, AWF writes an empty Alembic merge revision and pushes it as a follow-up
commit to the target branch. Use `--dry-run` to inspect the resolver result
without committing or pushing.

Plan terminal workspace filesystem garbage collection:

```bash
uv run --python 3.12 --extra dev awf service gc
uv run --python 3.12 --extra dev awf service gc --format pretty
uv run --python 3.12 --extra dev awf service gc --min-age-hours 336 --limit 20
```

`awf service gc` defaults to a dry-run JSON plan. Without `--status` filters it
selects only completed PR workspaces whose retention window has expired
(`AWF_COMPLETED_WORKSPACE_RETENTION_HOURS`, default `168`). Recent completed PR
workspaces and failed workspaces are reported in the `preserved` section with
reason codes such as `WORKSPACE_WITHIN_RETENTION` and
`FAILED_WORKSPACE_TRIAGE_PRESERVED`. Use `--retention-hours` or the compatible
`--min-age-hours` flag to override the retention window for one run. Each
candidate reports the worktree, compose, and auth paths plus estimated bytes;
missing paths are reported as zero bytes.

Execute the same filesystem-only cleanup with:

```bash
uv run --python 3.12 --extra dev awf service gc --execute
```

Execution deletes only `<work_dir>/git/worktrees/<workspace>`,
`<work_dir>/compose/<workspace>` or the stored compose-file parent, and
`<work_dir>/auth/<workspace>`. It does not delete control-plane database rows,
workspace events, log streams, or files under `<work_dir>/logs` and
`<work_dir>/artifacts`; durable logs and artifacts remain available for audit
and postmortem inspection. Repeated runs are idempotent: missing pressure
directories are reported as `already_removed`, and partial failures return a
structured `partial` result with reason codes instead of deleting unsafe paths.

Create a workspace:

```bash
uv run --python 3.12 --extra dev awf workspace create \
  --repo git@github.com:example/app.git \
  --base main \
  --profile auto \
  --agent codex \
  --title "Implement feature" \
  --prompt "Build the requested feature and commit the result." \
  --test "pytest -q"
```

*Note: `--effort` is optional. When omitted, AWF resolves the provider-specific
default from the workspace profile or adapter defaults.*

For cross-repo E2E work, add one or more managed companion services with
repeatable `--companion-json`. AWF clones each companion repo into a managed
worktree, resolves `build_context`, `dockerfile`, `env_file`, and relative
volume sources inside that checkout, and renders the companion into the same
workspace Compose stack:

```bash
uv run --python 3.12 --extra dev awf workspace create \
  --repo git@github.com:example/web.git \
  --base development \
  --profile auto \
  --agent codex \
  --title "Exercise web against backend" \
  --prompt "Update the web app and validate against the live backend companion." \
  --companion-json '{"name":"backend","repo_url":"git@github.com:example/api.git","base_branch":"development","build_context":".","dockerfile":"Dockerfile","env_file":"config/dev.env","compose_up_timeout_seconds":900,"depends_on":["docker"],"healthcheck_cmd":"curl -fsS http://localhost:8000/health"}' \
  --test "npm test"
```

Companion JSON must be one object per flag. Paths are repo-relative to the
companion checkout; absolute paths and `..` escapes are rejected. Companion env
files are repo files, not generated local secret files. Use
`compose_up_timeout_seconds` inside the companion JSON when a companion image
needs a longer cold-cache build/start budget than the profile default.

Add `--no-auto-merge` to keep monitoring after AWF posts the ready-for-human
comment, and `--initial-review-grace-period-seconds 0` only for explicit
fast-path tests.

Adopt an already-open GitHub PR into monitoring without rerunning the original
coding agent:

```bash
uv run --python 3.12 --extra dev awf workspace adopt-pr \
  --repo owner/repo \
  --pr 123 \
  --auto-merge \
  --reason "attach AWF to existing PR"
```

Equivalent PR URL form:

```bash
uv run --python 3.12 --extra dev awf workspace adopt-pr \
  --pr-url https://github.com/owner/repo/pull/123 \
  --no-auto-merge \
  --initial-review-grace-period-seconds 900
```

`awf workspace adopt-pr` posts to `POST /v1/workspaces/adopt-pr` and uses
`AWF_API_TOKEN` or `--api-token` for AWF API auth. GitHub PR metadata and later
monitor actions use the service-visible `AWF_GITHUB_TOKEN`, with `GH_TOKEN` and
`GITHUB_TOKEN` accepted as fallbacks. AWF derives deterministic repo/PR
idempotency for adoption; do not pass an adoption idempotency key. See
[PR Monitor Adoption](PR_MONITOR_ADOPTION.md) for GitHub readiness, permissions,
terminal adoption retry behavior, console inspection, REST/MCP examples, and the
mocked-local docs-tested demo path.
`awf` applies `/v1/` path normalization to all workspace calls that go through
this CLI, so reverse-proxy prefixes (for example `/awf`) are preserved and any
duplicate `/v1` is suppressed.
On non-2xx responses, the CLI prints request context in stderr before the
response payload, for example: `error: POST <normalized_url> -> HTTP 404`.
Sensitive query values in that URL context are redacted.

`awf workspace adopt-pr` accepts `AWF_BASE_URL`/`--base-url` in any of these
equivalent API-root forms. `AWF_CLI_BASE_URL` is still honored for
compatibility, but is deprecated. When neither variable is set, the host CLI
derives `http://localhost:${AWF_API_HOST_PORT:-8000}`.

```bash
awf workspace adopt-pr --base-url http://host:8000 --repo ...
awf workspace adopt-pr --base-url http://host:8000/ --repo ...
awf workspace adopt-pr --base-url http://host:8000/v1 --repo ...
awf workspace adopt-pr --base-url http://host:8000/v1/ --repo ...
```

If your API is behind a reverse proxy prefix, use the prefix and keep `/v1`
in the path:

```bash
awf workspace adopt-pr --base-url http://host:8000/awf --repo ...
awf workspace adopt-pr --base-url http://host:8000/awf/v1 --repo ...
```

Show a workspace:

```bash
uv run --python 3.12 --extra dev awf workspace show ws_123
```

List workspaces:

```bash
uv run --python 3.12 --extra dev awf workspace list --limit 25
```

Request workspace control actions:

```bash
uv run --python 3.12 --extra dev awf workspace cancel ws_123 --reason "No longer needed"
uv run --python 3.12 --extra dev awf workspace stop ws_123 --reason "Stack unstable"
uv run --python 3.12 --extra dev awf workspace refresh ws_123 --reason "Target branch advanced"
uv run --python 3.12 --extra dev awf workspace validate ws_123 --requested-tier 2
uv run --python 3.12 --extra dev awf workspace rebase ws_123 --reason "Recover merge conflicts"
uv run --python 3.12 --extra dev awf workspace destroy ws_123 --if-match 7
```

Control commands send an `Idempotency-Key` header. The CLI generates one when
`--idempotency-key` is omitted, which is convenient for one-off operator
commands. If a request times out or the response is dropped, rerun the command
with an explicit `--idempotency-key <stable-key>` value so AWF can replay the
same operation instead of starting a fresh one. Pass `--if-match <version>` when
you want optimistic concurrency against a workspace version or ETag.

Inspect workspace observability data:

```bash
uv run --python 3.12 --extra dev awf workspace events ws_123 --limit 50
uv run --python 3.12 --extra dev awf workspace events ws_123 --event-type workspace.created
uv run --python 3.12 --extra dev awf workspace runtime ws_123
uv run --python 3.12 --extra dev awf workspace operations ws_123 --limit 25
uv run --python 3.12 --extra dev awf workspace operations ws_123 --cursor "$NEXT_CURSOR"
uv run --python 3.12 --extra dev awf operations list --workspace-id ws_123 --limit 25
uv run --python 3.12 --extra dev awf operations list --cursor "$NEXT_CURSOR"
uv run --python 3.12 --extra dev awf workspace logs ws_123
uv run --python 3.12 --extra dev awf workspace log ws_123 agent.stdout --offset 0 --limit-bytes 65536
```

For protected observability endpoints, set `AWF_API_TOKEN` or pass
`--api-token` on the command. The CLI sends it as a bearer token and never
prints it.

Pretty output:

```bash
uv run --python 3.12 --extra dev awf workspace show ws_123 --format pretty
uv run --python 3.12 --extra dev awf workspace events ws_123 --format pretty
```

Preview profile resolution:

```bash
uv run --python 3.12 --extra dev awf profile preview ~/Projects/example-repo --profile auto
```
