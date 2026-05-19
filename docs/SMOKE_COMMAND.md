# `awf smoke run` — DX Smoke Proof

`awf smoke run` proves that AWF is correctly installed and configured from any
project with a workspace profile (`.awf/workspace.yml`, `.awf/workspace.yaml`, `awf.workspace.yml`, or `awf.workspace.yaml`) or the built-in demo project.

When a profile file is present, smoke uses it directly.
If no profile file exists, it falls back to auto-detection.
If a profile file is present but unreadable or invalid, smoke fails with `SMOKE_PROFILE_PREVIEW_FAILED` instead of silently falling back.

It validates seven phases in order:

| Phase | Reason codes |
|---|---|
| **service_readiness** | `SMOKE_SERVICE_READY` / `SMOKE_SERVICE_UNREACHABLE` |
| **auth_readiness** | `SMOKE_AUTH_READY` / `SMOKE_AUTH_PARTIAL` / `SMOKE_AUTH_UNAVAILABLE` |
| **profile_preview** | `SMOKE_PROFILE_READY` / `SMOKE_PROFILE_NOT_DETECTED` / `SMOKE_PROFILE_PREVIEW_FAILED` |
| **validation** | `SMOKE_VALIDATION_READY` / `SMOKE_VALIDATION_MISSING` |
| **workspace_request** | `SMOKE_WORKSPACE_REQUEST_READY` / `SMOKE_WORKSPACE_REQUEST_FAILED` |
| **pr_monitor** | `SMOKE_PR_MOCKED_LOCAL` / `SMOKE_PR_UNAVAILABLE` |
| **console_links** | `SMOKE_CONSOLE_READY` / `SMOKE_CONSOLE_UNAVAILABLE` |

Each phase returns `status` (`ok` / `warn` / `fail`), a `reason_code`, a
human-readable `message`, `evidence`, and an `action` string.

The overall report includes `status` (ok/warn/fail), `project`, `mode`
(live/mocked_local), `phases`, `console_links`, and `next_actions`.

Smoke reports probe configured console URLs before reporting
`SMOKE_CONSOLE_READY`. When `AWF_CONSOLE_URL` is not set, they infer and probe
the default local console URL, `http://localhost:3000`. Start it with
`npm --prefix apps/console run dev` from an AWF source checkout when the
`console_links` phase reports `SMOKE_CONSOLE_UNAVAILABLE`. Installed-CLI users
should start the console through their installed setup rather than this
source-checkout-specific npm command.

## Options

| Flag | Default | Description |
|---|---|---|
| `--project` | `.` | Path to the project to smoke |
| `--format` | `json` | Output format: `json` or `pretty` |
| `--mocked-local` | off | Run without live external services; all phases become advisory |
| `--demo-path` | `examples/awf-core-demo` | Fallback when `--project` has no profile |

## Examples

```bash
# JSON output (default)
awf smoke run

# Human-readable
awf smoke run --format pretty

# Mocked-local mode (no live GitHub/providers needed)
awf smoke run --mocked-local

# Smoke a specific project
awf smoke run --project ~/projects/my-app --format pretty
```

## Mocked-local mode

In `--mocked-local` mode, the PR/monitor phase reports `SMOKE_PR_MOCKED_LOCAL`
instead of failing. Auth and service phases still report their actual status but
do not block overall success. This makes `awf smoke run` safe to run repeatedly
without side effects, even without live GitHub credentials or a running service
stack.
