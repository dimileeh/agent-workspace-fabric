# Plan: Provider Readiness Observability for Local Service Mode

## Goal

Surface missing or unusable GitHub and agent-provider credentials before AWF
schedules provider-specific work in local service mode.

Expected operator-visible outcome:

- `/readyz` and `awf service status` include an `agent_readiness` section.
- GitHub reports whether `gh` auth is usable for PR create/comment/merge flows
  without exposing token values.
- Claude Code, Gemini, and OpenCode/Ollama report readiness from the same
  env/file signals that the service worker can see and later mount into
  workspace containers.
- Missing optional providers are warnings by default and do not make the whole
  service unhealthy.
- A provider-specific strict check converts the selected provider's warning into
  a readiness failure with stable reason codes.
- All details are actionable and redacted.

## Current Code Context

The existing observability surfaces are:

- `src/awf/api/routes/health.py`
  - owns `/healthz` and `/readyz`
  - currently reports DB, Docker CLI/daemon/Compose, and runtime image checks
  - uses stable reason codes and `CheckResult`/`ReadyResponse` Pydantic models

- `src/awf/service/status.py`
  - owns `collect_service_status(...)` for `awf service status`
  - currently reports API, DB, Docker, runtime image, disk, and orphan workspace
    checks
  - already supports injected subprocess, HTTP, socket, disk, and workspace
    lookup seams for focused tests

- `src/awf/cli/main.py`
  - exposes `awf service status --format json|pretty`
  - exits non-zero only when the collected top-level status is not `ok`

- `src/awf/service/config.py`
  - resolves local service settings, including `AWF_GITHUB_TOKEN` with
    `GH_TOKEN` and `GITHUB_TOKEN` fallbacks
  - redacts tokens in `service_config_payload(...)`

- `src/awf/node/auth_mounts.py`
  - defines the file signals the service worker can see and copy into workspace
    auth directories:
    `~/.config/gh`, `~/.claude`, `~/.claude.json`, `~/.gemini`,
    `~/.config/opencode`, and selected `~/.ollama` auth files

- `docker/compose/local-service.yml` and `.env.example`
  - already pass GitHub and provider env vars into the API/worker containers
  - already mount the narrow host credential paths into the service containers

This task should extend those surfaces instead of adding a parallel diagnostic
command.

## Intended Files And Modules To Touch

Production code:

- `src/awf/service/provider_readiness.py` (new)
  - Shared provider-readiness collector used by CLI/service status and API
    readiness.
  - Defines stable provider names, result payload shape, reason codes, redaction
    helpers, and injected seams for subprocess, HTTP, env, and filesystem checks.

- `src/awf/service/status.py`
  - Call the shared collector from `collect_service_status(...)`.
  - Add `agent_readiness` to the returned payload.
  - Keep the top-level status `ok` when only optional providers are missing.
  - Accept a strict provider set and fail only when a selected provider is not
    ready.

- `src/awf/api/routes/health.py`
  - Extend `ReadyResponse` with `agent_readiness`.
  - Run provider readiness alongside existing dependency checks.
  - Add a repeatable query parameter such as `provider=github` /
    `provider=claude_code` / `provider=gemini` / `provider=opencode`; when
    present, those providers are strict and can make `/readyz` return 503.
  - Preserve existing `/readyz` response shape for `checks`.

- `src/awf/cli/main.py`
  - Add a repeatable `--provider` option to `awf service status`.
  - Passing one or more providers requests strict readiness for those providers.
  - JSON and pretty output should naturally include the new section through the
    existing emitter.

- `src/awf/service/config.py`
  - Only touch if the provider collector needs a small typed provider enum or
    helper to share the GitHub token fallback contract.
  - Do not add new secret-bearing fields unless required.

- `README.md`
  - Document local service credential propagation through `.env` and
    `docker/compose/.env`.
  - Explicitly call out that macOS keychain-backed `gh` auth is not visible
    inside Compose containers; operators should set `AWF_GITHUB_TOKEN` or
    `GH_TOKEN`, commonly from `gh auth token`.
  - Show how to run default warning-mode status and provider-specific strict
    status.

- `.env.example`
  - Add or tighten comments for GitHub, Claude, Gemini, OpenCode/Ollama, and
    Ollama host/base URL variables.

- `docker/compose/local-service.yml`
  - Only touch if tests expose a missing env var needed by the readiness
    contract. Existing provider env pass-through should be preserved.

Tests:

- `tests/unit/service/test_provider_readiness.py` (new)
- `tests/unit/service/test_status.py`
- `tests/unit/api/test_health.py`
- `tests/unit/cli/test_service_cli.py` or `tests/unit/cli/test_cli.py`
- `tests/integration/test_local_service_compose.py` only if Compose env/docs
  changes need contract coverage

No planned edits to migrations, DB models, workspace state machine, scheduler,
executor, PR monitor, merge queue, adapters, lockfiles, coverage thresholds, or
console code.

## Provider Readiness Contract

Use a top-level section like:

```json
{
  "agent_readiness": {
    "status": "ok",
    "strict_providers": [],
    "providers": {
      "github": {
        "status": "ok",
        "ok": true,
        "severity": "ok",
        "reason": "GITHUB_AUTH_OK",
        "capabilities": ["pr_create", "comment", "merge"],
        "signals": ["AWF_GITHUB_TOKEN", "gh auth status"]
      }
    }
  }
}
```

Statuses:

- `ok`: provider has a usable signal.
- `warn`: provider is optional and missing, incomplete, or not cheaply
  reachable.
- `fail`: provider was requested strictly and is not ready.

The `agent_readiness.status` should be:

- `ok` when every provider is ok or only optional warnings exist.
- `fail` when any strict provider fails.

Reason-code examples:

- GitHub:
  - `GITHUB_AUTH_OK`
  - `GITHUB_TOKEN_ENV_MISSING`
  - `GITHUB_KEYRING_ONLY_NOT_VISIBLE_IN_COMPOSE`
  - `GITHUB_CLI_NOT_FOUND`
  - `GITHUB_AUTH_UNUSABLE`
  - `GITHUB_AUTH_TIMEOUT`

- Claude Code:
  - `CLAUDE_ENV_AUTH_PRESENT`
  - `CLAUDE_FILE_AUTH_PRESENT`
  - `CLAUDE_AUTH_MISSING`

- Gemini:
  - `GEMINI_ENV_AUTH_PRESENT`
  - `GEMINI_FILE_AUTH_PRESENT`
  - `GEMINI_AUTH_MISSING`

- OpenCode/Ollama:
  - `OPENCODE_FILE_AUTH_PRESENT`
  - `OLLAMA_FILE_AUTH_PRESENT`
  - `OLLAMA_ENV_AUTH_PRESENT`
  - `OLLAMA_HOST_REACHABLE`
  - `OLLAMA_HOST_UNREACHABLE`
  - `OPENCODE_OLLAMA_AUTH_MISSING`

Signal rules:

- GitHub is ready when an env token is present and `gh auth status` succeeds
  under the service-visible env. Do not print or return the token. If no env
  token exists but `~/.config/gh` exists, report the keyring/file-only warning
  with an action to set `AWF_GITHUB_TOKEN` or `GH_TOKEN` in Compose.
- Claude Code is ready when any supported Claude/Anthropic env auth signal is
  present, or when `~/.claude` or `~/.claude.json` is visible under
  `AWF_HOST_HOME`.
- Gemini is ready when Gemini/Google env auth is present, `GOOGLE_APPLICATION_CREDENTIALS`
  points to a visible file, or `~/.gemini` is visible under `AWF_HOST_HOME`.
- OpenCode/Ollama is ready when OpenCode config or Ollama auth files are
  visible and, when an Ollama URL/host is configured or implied by the adapter
  default, a cheap `GET /api/version` style probe succeeds. A failed reachability
  probe is a warning unless `opencode` is requested strictly.
- Every detail string must pass a redaction helper before returning. Do not
  include raw env values, token substrings, credential URLs, private key
  contents, or file contents.

## Tests To Write First

Write these as red tests before implementation, using temp homes and injected
fakes. No test should require real credentials, real `gh`, real Ollama, or
network access.

1. `test_provider_readiness_all_green`

   Location: `tests/unit/service/test_provider_readiness.py`

   Setup:
   - Temp `host_home` with Claude, Gemini, OpenCode, and Ollama auth files.
   - Env contains a fake GitHub token and provider env variables.
   - Fake `gh auth status` returns success.
   - Fake Ollama HTTP probe returns a version response.

   Assertions:
   - `agent_readiness.status == "ok"`
   - every provider has `ok is True`
   - GitHub capabilities include PR create/comment/merge
   - no raw fake secret appears anywhere in the serialized payload

2. `test_provider_readiness_missing_github_token_warns_by_default`

   Setup:
   - No `AWF_GITHUB_TOKEN`, `GH_TOKEN`, or `GITHUB_TOKEN`.
   - No usable `gh` env token.
   - Other providers can be ready or omitted.

   Assertions:
   - GitHub provider has `status == "warn"` and `ok is False`
   - reason is `GITHUB_TOKEN_ENV_MISSING`
   - overall `agent_readiness.status == "ok"`
   - the service status top-level status remains `ok` when only this optional
     warning exists

3. `test_provider_readiness_github_strict_missing_token_fails`

   Setup:
   - Same as the missing-token case, but request strict provider `github`.

   Assertions:
   - GitHub provider has `status == "fail"`
   - overall `agent_readiness.status == "fail"`
   - `collect_service_status(..., strict_providers={"github"})` returns
     top-level `status == "fail"`
   - `/readyz?provider=github` returns HTTP 503

4. `test_provider_readiness_keyring_only_github_warning_is_actionable`

   Setup:
   - Temp `host_home/.config/gh/hosts.yml` exists.
   - No GitHub env token.
   - Fake `gh auth status` returns a keyring-style auth failure or is not run
     because there is no env token.

   Assertions:
   - reason is `GITHUB_KEYRING_ONLY_NOT_VISIBLE_IN_COMPOSE`
   - message/action mentions `AWF_GITHUB_TOKEN` or `GH_TOKEN`
   - no token-like file contents are returned

5. `test_provider_readiness_claude_env_present`

   Setup:
   - Env contains `ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN`.
   - No Claude auth files.

   Assertions:
   - Claude provider is `ok`
   - reason is `CLAUDE_ENV_AUTH_PRESENT`
   - returned signals name env variable names only, not values

6. `test_provider_readiness_claude_file_present`

   Setup:
   - Temp `host_home/.claude.json` or `host_home/.claude` exists.
   - No Claude env.

   Assertions:
   - Claude provider is `ok`
   - reason is `CLAUDE_FILE_AUTH_PRESENT`
   - payload names the path category, not file content

7. `test_provider_readiness_gemini_file_present`

   Setup:
   - Temp `host_home/.gemini` exists.
   - No Gemini/Google env.

   Assertions:
   - Gemini provider is `ok`
   - reason is `GEMINI_FILE_AUTH_PRESENT`

8. `test_provider_readiness_opencode_ollama_file_present`

   Setup:
   - Temp `host_home/.config/opencode` and selected `host_home/.ollama`
     auth files exist.
   - Fake Ollama probe succeeds or is explicitly not required for this file-only
     readiness assertion.

   Assertions:
   - OpenCode provider is `ok`
   - reason indicates OpenCode/Ollama file auth is present
   - model blobs under `~/.ollama/models` are not inspected or copied

9. `test_provider_readiness_redacts_secret_values_from_details`

   Setup:
   - Env includes distinctive fake values like `ghp_super_secret`,
     `anthropic_secret`, and `gemini_secret`.
   - Fake subprocess stderr includes a credential URL such as
     `https://user:ghp_super_secret@github.com/org/repo`.

   Assertions:
   - serialized payload does not contain any fake secret
   - detail contains a redacted placeholder
   - reason code remains actionable

10. API and CLI integration tests

   Locations:
   - `tests/unit/api/test_health.py`
   - `tests/unit/cli/test_service_cli.py` or `tests/unit/cli/test_cli.py`

   Assertions:
   - `/readyz` includes `agent_readiness` while preserving existing `checks`
     keys and 200 behavior for optional warnings.
   - `/readyz?provider=github` returns 503 for missing GitHub auth.
   - `awf service status --format pretty` prints nested provider reason codes.
   - `awf service status --provider github` exits non-zero when GitHub is not
     ready.

## Implementation Steps

1. Add `src/awf/service/provider_readiness.py`.

   Keep it mostly pure and injectable:

   - input: `ServiceSettings`, `environ`, `strict_providers`, optional command
     runner, optional HTTP probe, optional filesystem/path helpers
   - output: JSON-serializable dict with `status`, `strict_providers`, and
     `providers`
   - stable provider names: `github`, `claude_code`, `gemini`, `opencode`
   - strict provider validation rejects unknown provider names with a clear CLI
     error or API 422

2. Implement redaction centrally.

   Redact:

   - exact configured secret values from known env keys
   - URL credentials matching `https://user[:password]@host`
   - token-looking substrings with common prefixes such as `ghp_`, `github_pat_`,
     `sk-ant-`, and long bearer-style strings

   Prefer returning signal names and path categories so redaction is a last
   guard, not the primary safety mechanism.

3. Implement GitHub readiness.

   - Build the GitHub env from service settings and the current environment.
   - If no token is visible:
     - if `host_home/.config/gh` exists, return keyring-only Compose warning
     - otherwise return missing-token warning
   - If a token is visible, run a bounded `gh auth status` check with the token
     in env and no token in args.
   - Map missing binary, timeout, non-zero exit, and success to stable reason
     codes.
   - Report capabilities `pr_create`, `comment`, and `merge` as the AWF uses
     those through `gh`.

4. Implement Claude/Gemini/OpenCode file/env checks.

   - Reuse the same host-home path assumptions as `auth_mounts.py`.
   - Check presence and readability/type only; never read secret file contents.
   - For `GOOGLE_APPLICATION_CREDENTIALS`, only report that the path exists.
   - For Ollama, normalize `OLLAMA_HOST`, `AWF_OPENCODE_OLLAMA_BASE_URL`, or the
     adapter default into a cheap version endpoint probe. Use a short timeout and
     treat probe failures as optional warnings unless strict.

5. Wire service status.

   - Extend `collect_service_status(...)` with `strict_providers` and provider
     test seams.
   - Gather provider readiness concurrently with existing checks where practical.
   - Compute top-level status from hard dependency checks plus strict-provider
     failures only. Optional provider warnings should not fail the service.

6. Wire API readiness.

   - Add `agent_readiness` to `ReadyResponse`.
   - Add a repeatable `provider` query parameter.
   - Convert unknown provider names to a structured 422 response if using string
     validation, or use an enum-like literal to let FastAPI validate.
   - Include provider readiness in the 503 decision only for strict provider
     failures.

7. Wire CLI.

   - Add `--provider` as a repeatable option on `service status`.
   - Pass the selected providers to `collect_service_status(...)`.
   - Existing JSON/pretty output should require little or no custom formatting.

8. Update docs.

   - README setup section: show `.env` and optional `docker/compose/.env`
     patterns for credential propagation.
   - Explain that Docker Compose interpolation cannot see tokens stored only in
     macOS Keychain-backed `gh` auth; set `AWF_GITHUB_TOKEN=$(gh auth token)` in
     the environment or Compose env file.
   - List Claude, Gemini, OpenCode/Ollama env and file signals used by readiness.
   - Show commands:
     - `awf service status --format pretty`
     - `awf service status --provider github --format pretty`
     - `curl 'http://localhost:8000/readyz?provider=github'`

## Validation Commands

Run focused red/green tests first:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_provider_readiness.py -q
uv run --python 3.12 --extra dev pytest tests/unit/service/test_status.py -q
uv run --python 3.12 --extra dev pytest tests/unit/api/test_health.py -q
uv run --python 3.12 --extra dev pytest tests/unit/cli/test_service_cli.py tests/unit/cli/test_cli.py -q
```

If `docker/compose/local-service.yml` changes:

```bash
uv run --python 3.12 --extra dev pytest tests/integration/test_local_service_compose.py -q
```

Then run the Python/control-plane checks from `AGENTS.md`:

```bash
uv run --python 3.12 --extra dev ruff check src/awf tests
uv run --python 3.12 --extra dev mypy src/awf
uv run --python 3.12 --extra dev pytest tests/unit -q
```

Because this touches service readiness and shared observability, run coverage
before final delivery:

```bash
uv run --python 3.12 --extra dev pytest --cov=awf --cov-report=term-missing
```

Console validation is not needed unless console files are unexpectedly touched.

## Risks And Assumptions

- `gh auth status` can require network or a keyring helper depending on the
  auth mode. Keep it bounded and map failures to reason codes instead of raw
  stderr.
- File presence is a readiness signal, not proof that the provider account can
  use a specific model. Model entitlement remains a runtime/provider concern.
- `~/.config/gh` may contain a usable token on Linux but only keychain metadata
  on macOS. In local service Compose, an explicit env token is the reliable
  signal AWF can propagate.
- Ollama host probing must be cheap. Do not pull models, list large model data,
  run inference, or read `~/.ollama/models`.
- The API and CLI currently use different command-runner abstractions. The new
  shared collector should accept small adapters rather than coupling API
  readiness to the CLI's synchronous subprocess seam.
- Optional-provider warnings should be visible but should not take down generic
  local service readiness. Strict provider checks are the scheduling gate for
  provider-specific work.
- Existing pretty output recursively prints dicts. Deep provider payloads should
  stay compact so terminal output remains readable.

## Explicit Non-Goals

- Do not schedule, block, or mutate existing workspaces in this slice.
- Do not add real secret leasing or cloud secret broker behavior.
- Do not validate model entitlement, Anthropic/Gemini billing, or Ollama Cloud
  account permissions by making paid/model calls.
- Do not read or return credential file contents.
- Do not add retries that hide auth failures.
- Do not lower coverage, `fail_under`, `.awf` coverage config, `pyproject`
  coverage config, or quality gates.
- Do not push, rebase, switch branches, or manually open/merge PRs.
