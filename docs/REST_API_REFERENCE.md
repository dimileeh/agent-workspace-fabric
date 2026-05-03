# REST API Reference

## API Surface

Run the API locally:

```bash
uv run --python 3.12 --extra dev awf serve --host 127.0.0.1 --port 8000
```

Health check:

```bash
curl http://localhost:8000/healthz
```

Create a v2 workspace:

```bash
curl -X POST http://localhost:8000/v2/workspaces \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: example-task-001" \
  -d '{
    "repo": {
      "url": "git@github.com:example/app.git",
      "base_branch": "main"
    },
    "task": {
      "title": "Implement feature",
      "prompt": "Build the requested feature and commit the result.",
      "kind": "feature_branch_pr",
      "agent": "codex",
      "model": null,
      "task_class": "refactor_task",
      "owned_paths": ["src/**", "tests/**"],
      "auto_merge": true,
      "initial_review_grace_period_seconds": null
    },
    "workspace": {
      "profile_ref": "auto",
      "profile": null
    },
    "validation": {
      "commands": ["pytest -q"],
      "requested_tier": 1
    },
    "resources": {
      "cpu": 4,
      "memory": "8g"
    }
  }'
```

Current local-service behavior: the REST API persists workspace requests and
exposes state, and the always-on worker drives feature PR workspaces through the
full lifecycle: `requested -> provisioning -> ready -> running -> validating ->
pushing -> monitoring_pr -> completed/failed`. Feature PR workspaces created
through the service use the resolved profile's monitor grace window
(`monitor.initial_review_grace_period_seconds`, default `900`) unless the task
sets `initial_review_grace_period_seconds`. `auto_merge: true` routes to the
feature monitor, which may merge after the gates pass. `auto_merge: false`
routes to the manual/release monitor behavior: AWF posts the ready-for-human
comment and keeps polling until a human merge is observed. Release/sync flows
remain available through the compatibility dogfood scripts.

The v2 task object also accepts policy metadata for future deterministic
scheduling:

- `task_class`: optional; one of `docs_task`, `test_task`, `refactor_task`,
  `migration_task`, `dependency_task`, or `build_config_task`.
- `owned_paths`: optional list of path globs/strings the task expects to own;
  omitted values default to `[]`.

AWF persists and returns these fields on workspace, task, overview, and MCP
workspace create/get/list responses. This slice does not enforce locks or
change scheduling behavior yet.

Get one workspace:

```bash
curl http://localhost:8000/v1/workspaces/ws_123
```

List workspaces:

```bash
curl "http://localhost:8000/v1/workspaces?limit=50"
```

List workspaces with dashboard-friendly filters:

```bash
curl "http://localhost:8000/v1/workspaces?status=monitoring_pr&agent=codex&repo_url=git@github.com:example/app.git&limit=25"
```

Poll immutable events:

```bash
curl "http://localhost:8000/v1/events?workspace_id=ws_123&limit=50"
```

Events response shape:

```json
{
  "items": [],
  "next_cursor": null,
  "has_more": false
}
```

List and download workspace artifacts through the protected observability API:

```bash
curl -H "Authorization: Bearer $AWF_API_TOKEN" \
  "http://localhost:8000/v1/workspaces/ws_123/artifacts"

curl -OJ -H "Authorization: Bearer $AWF_API_TOKEN" \
  "http://localhost:8000/v1/workspaces/ws_123/artifacts/download?path=logs/stdout.txt"
```

Artifact downloads are limited to regular files under
`<AWF_WORK_DIR>/artifacts/<workspace_id>` using POSIX-style relative paths.
Absolute paths, traversal segments, backslashes, symlinks, and missing files are
rejected without reading arbitrary host paths.

