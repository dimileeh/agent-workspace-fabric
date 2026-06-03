"""Operator-facing doctor reason text."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from awf.runtime.ownership import AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE
from awf.service.doctor.models import DiagnosticStatus


@dataclass(frozen=True)
class _ReasonText:
    """Operator-facing reason text and remediation guidance."""

    message: str
    action: str
    likely_cause: str
    related_command: str
    docs_link: str


def _reason_catalog_link(reason_code: str) -> str:
    """Return the local reason catalog anchor for a stable reason code."""
    return f"docs/REASON_CATALOG.md#{reason_code.lower()}"


def reason_text_for_code(reason_code: str) -> _ReasonText | None:
    """Return operator-facing reason catalog text for a stable reason code."""
    return _REASON_TEXT.get(reason_code)


_REASON_TEXT: dict[str, _ReasonText] = {
    AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE: _ReasonText(
        (
            "AWF could not repair workspace runtime file ownership before "
            "running setup or committing monitor fixes."
        ),
        (
            "Inspect worker logs, verify the AWF work directory is writable by "
            "the control-plane container, then remonitor or recreate the "
            "workspace after fixing permissions."
        ),
        (
            "The local control-plane container could not chown the workspace "
            "worktree to the agent runtime UID/GID, often because the worktree "
            "or nested runtime state such as `.venv` is missing, read-only, or "
            "mounted with incompatible permissions."
        ),
        "awf service logs --service worker",
        "docs/REASON_CATALOG.md#agent_runtime_ownership_repair_failed",
    ),
    "FORGE_NOT_SUPPORTED": _ReasonText(
        (
            "AWF detected a code forge it does not yet support. Only GitHub is "
            "implemented; BitBucket is detected but not yet available."
        ),
        (
            "Use a GitHub-hosted repository for now, or track BitBucket support in "
            "issue #345 (Phase 1 adds detection only). Recreate the workspace "
            "against a github.com remote."
        ),
        (
            "The workspace repository URL resolved to a non-GitHub forge (for "
            "example bitbucket.org), or the workspace profile set `forge: "
            "bitbucket`. Phase 1 of issue #345 adds forge detection without a "
            "BitBucket client, so the workspace fails fast instead of mis-routing "
            "to GitHub."
        ),
        "awf workspace create",
        _reason_catalog_link("FORGE_NOT_SUPPORTED"),
    ),
    "AWF_SETUP_PLACEHOLDER": _ReasonText(
        "`awf setup` is registered but the first-run setup implementation is not active yet.",
        (
            "Use the current service bootstrap path for local Core startup, then "
            "use project init for repository onboarding until the setup slice lands."
        ),
        "This build contains the stable command surface before the setup checks are implemented.",
        "awf service bootstrap",
        _reason_catalog_link("AWF_SETUP_PLACEHOLDER"),
    ),
    "AWF_START_PLACEHOLDER": _ReasonText(
        "`awf start` is registered but the local Core start wrapper is not active yet.",
        (
            "Use the current service bootstrap path for local Core startup, then "
            "use project init for repository onboarding until the start slice lands."
        ),
        "This build contains the stable command surface before the start wrapper is implemented.",
        "awf service bootstrap",
        _reason_catalog_link("AWF_START_PLACEHOLDER"),
    ),
    "HOST_SETUP_CONFIG_CORRUPT": _ReasonText(
        "AWF could not read the host setup config.",
        (
            "Inspect `~/.awf/config.yml`, remove invalid YAML or unsupported "
            "fields, then rerun `awf setup`."
        ),
        (
            "The config file is malformed, has duplicate keys, uses unsupported "
            "schema versions, or cannot be interpreted safely."
        ),
        "awf setup",
        _reason_catalog_link("HOST_SETUP_CONFIG_CORRUPT"),
    ),
    "HOST_SETUP_CONFIG_SECRET_VALUE": _ReasonText(
        "AWF rejected a host setup config value that looks like a secret.",
        (
            "Replace raw credentials in `~/.awf/config.yml` with keyring, env, "
            "or approved plain-file refs, then rerun setup."
        ),
        "Host setup config stores references and metadata only, never raw provider credentials.",
        "awf setup --provider <provider>",
        _reason_catalog_link("HOST_SETUP_CONFIG_SECRET_VALUE"),
    ),
    "HOST_SETUP_CONFIG_WRITE_FAILED": _ReasonText(
        "AWF could not write the host setup config.",
        (
            "Verify the `~/.awf` directory exists, is writable by your user, and "
            "is not blocked by filesystem permissions, then rerun setup."
        ),
        "The host config path or parent directory is missing, read-only, or refused atomic writes.",
        "awf setup",
        _reason_catalog_link("HOST_SETUP_CONFIG_WRITE_FAILED"),
    ),
    "SOURCE_CHECKOUT_INVALID": _ReasonText(
        "AWF could not verify the source checkout assets needed for first-run setup/start.",
        (
            "Run from the AWF repository root or pass `--source-checkout` pointing "
            "at a complete checkout with pyproject, docs, migrations, and Docker assets."
        ),
        "The selected path is missing required AWF source markers or contains unreadable assets.",
        "awf setup --source-checkout .",
        _reason_catalog_link("SOURCE_CHECKOUT_INVALID"),
    ),
    "SOURCE_CHECKOUT_ASSETS_STALE": _ReasonText(
        "AWF source checkout metadata is stale or no longer matches the current asset contract.",
        (
            "Re-run setup with the current checkout path so AWF can verify and "
            "record fresh source asset metadata."
        ),
        "A previously recorded source checkout was moved, changed, or verified against an older asset list.",
        "awf setup --source-checkout .",
        _reason_catalog_link("SOURCE_CHECKOUT_ASSETS_STALE"),
    ),
    "SETUP_READINESS_FAILED": _ReasonText(
        "AWF setup found one or more machine readiness blockers.",
        (
            "Fix the reported blockers such as Docker, Compose, Git, disk, port, "
            "or PATH issues, then rerun `awf setup --dry-run`."
        ),
        "The local machine does not currently satisfy the prerequisites for first-run AWF Core.",
        "awf setup --dry-run",
        _reason_catalog_link("SETUP_READINESS_FAILED"),
    ),
    "SETUP_PROVIDER_UNKNOWN": _ReasonText(
        "AWF setup was asked to configure an unsupported provider.",
        (
            "Use a supported provider name from setup help, or omit `--provider` "
            "to evaluate all configured providers."
        ),
        "The provider selector does not match a provider known to this AWF build.",
        "awf setup --help",
        _reason_catalog_link("SETUP_PROVIDER_UNKNOWN"),
    ),
    "INTERACTIVE_INPUT_REQUIRED": _ReasonText(
        "AWF setup needs interactive input that is unavailable in this run.",
        (
            "Run setup interactively, or provide safe env/keychain references "
            "for the requested provider before retrying non-interactive setup."
        ),
        "A non-interactive setup path reached a prompt that would collect required settings or credentials.",
        "awf setup --non-interactive",
        _reason_catalog_link("INTERACTIVE_INPUT_REQUIRED"),
    ),
    "CREDENTIAL_BACKEND_UNAVAILABLE": _ReasonText(
        "AWF could not access a usable credential storage backend.",
        (
            "Configure the OS keychain backend, provide env refs, or explicitly "
            "choose the approved plain-file fallback when that policy applies."
        ),
        "The preferred keychain backend is unavailable and no safe fallback was selected.",
        "awf setup --provider <provider>",
        _reason_catalog_link("CREDENTIAL_BACKEND_UNAVAILABLE"),
    ),
    "CREDENTIAL_PLAIN_FILE_CONSENT_REQUIRED": _ReasonText(
        "AWF refused to store a provider credential in a plain file without explicit consent.",
        (
            "Use keyring or env refs, or rerun setup with the explicit plain-file "
            "consent flag only on an approved headless Linux path."
        ),
        "Plain-file provider secrets are an opt-in fallback and cannot be selected silently.",
        "awf setup --allow-plain-secrets",
        _reason_catalog_link("CREDENTIAL_PLAIN_FILE_CONSENT_REQUIRED"),
    ),
    "CREDENTIAL_REF_INVALID": _ReasonText(
        "AWF rejected a malformed provider credential reference.",
        (
            "Use a `keyring://`, `env://`, or approved `plain-file://` reference "
            "and keep raw token values out of config and JSON payloads."
        ),
        "The credential reference uses an unsupported scheme, unsafe path, or raw secret value.",
        "awf setup --provider <provider>",
        _reason_catalog_link("CREDENTIAL_REF_INVALID"),
    ),
    "PROVIDER_SETUP_AUTH_INVALID": _ReasonText(
        "AWF could not authenticate one provider during setup.",
        (
            "Refresh that provider's credential or login state, then rerun setup "
            "for the provider; other providers may remain usable."
        ),
        "The provider token, CLI login, or credential reference is missing, expired, or rejected.",
        "awf setup --provider <provider>",
        _reason_catalog_link("PROVIDER_SETUP_AUTH_INVALID"),
    ),
    "SETUP_CLIENT_UNKNOWN": _ReasonText(
        "AWF setup was asked to configure an unsupported client integration.",
        (
            "Use a supported client name (claude or codex) with `awf setup "
            "--client <client>`, or omit `--client` to run the readiness pass."
        ),
        "The client selector does not match a client known to this AWF build.",
        "awf setup --client <client>",
        _reason_catalog_link("SETUP_CLIENT_UNKNOWN"),
    ),
    "CLIENT_CONFIG_CONFLICT": _ReasonText(
        "AWF found a client MCP configuration conflict.",
        (
            "Review the proposed diff or existing client config, resolve the "
            "conflicting server entry, then rerun setup for that client."
        ),
        "A Claude or Codex MCP config already contains incompatible AWF server settings.",
        "awf setup --client <client>",
        _reason_catalog_link("CLIENT_CONFIG_CONFLICT"),
    ),
    "CLIENT_CONFIG_WRITE_FAILED": _ReasonText(
        "AWF could not write a client MCP configuration file.",
        (
            "Check file permissions and parent directories, preserve any backup, "
            "then rerun setup for the selected client."
        ),
        "The client config path is missing, read-only, locked by another process, or failed atomic write.",
        "awf setup --client <client>",
        _reason_catalog_link("CLIENT_CONFIG_WRITE_FAILED"),
    ),
    "INSTALLER_UNSUPPORTED_PLATFORM": _ReasonText(
        "The AWF installer does not support this operating system or architecture.",
        (
            "Use a supported macOS/Linux platform, WSL where documented, or the "
            "manual Python tool install path for this environment."
        ),
        "The installer detected an OS or CPU architecture outside the supported first release matrix.",
        "uv tool install agent-workspace-fabric",
        _reason_catalog_link("INSTALLER_UNSUPPORTED_PLATFORM"),
    ),
    "INSTALLER_DEPENDENCY_MISSING": _ReasonText(
        "The AWF installer is missing a required host dependency.",
        (
            "Install the reported dependency such as `curl`, `tar`, `sh`, `uv`, "
            "or `pipx`, then rerun the installer."
        ),
        "The selected install method depends on a host tool that is not available on PATH.",
        "awf --help",
        _reason_catalog_link("INSTALLER_DEPENDENCY_MISSING"),
    ),
    "INSTALLER_CHECKSUM_MISMATCH": _ReasonText(
        "The AWF installer refused an artifact whose checksum did not match the manifest.",
        (
            "Do not run the downloaded artifact; retry against the canonical "
            "release manifest or choose a different pinned version."
        ),
        "The downloaded wheel or source artifact differs from the manifest-pinned sha256.",
        "awf --version",
        _reason_catalog_link("INSTALLER_CHECKSUM_MISMATCH"),
    ),
    "INSTALLER_METHOD_FAILED": _ReasonText(
        "The AWF installer method failed before AWF was installed.",
        (
            "Inspect the reported method output, fix package-manager or network "
            "issues, and retry the same pinned install method."
        ),
        "The selected uv, pipx, or manual install command exited non-zero.",
        "uv tool install agent-workspace-fabric",
        _reason_catalog_link("INSTALLER_METHOD_FAILED"),
    ),
    "INSTALLER_PATH_NOT_REACHABLE": _ReasonText(
        "The AWF installer completed but the `awf` executable is not reachable on PATH.",
        (
            "Open a new shell or update PATH using the installer-provided shell "
            "hint, then verify with `awf --version`."
        ),
        "The tool install location is not visible to the current shell environment.",
        "awf --version",
        _reason_catalog_link("INSTALLER_PATH_NOT_REACHABLE"),
    ),
    "START_COMPOSE_ASSETS_MISSING": _ReasonText(
        "AWF start could not locate the Compose or runtime assets needed to start local Core.",
        (
            "Use a valid package install or pass a verified `--source-checkout` "
            "path that contains AWF Docker and Compose assets."
        ),
        "The selected package/source asset lane does not contain required bootstrap files.",
        "awf start --source-checkout .",
        _reason_catalog_link("START_COMPOSE_ASSETS_MISSING"),
    ),
    "START_PORT_CONFLICT": _ReasonText(
        "AWF start found a local port conflict.",
        (
            "Stop the process using the reported port or configure a different "
            "AWF API/console port before retrying start."
        ),
        "A local service is already bound to a port needed by AWF Core.",
        "awf start",
        _reason_catalog_link("START_PORT_CONFLICT"),
    ),
    "START_MIGRATION_FAILED": _ReasonText(
        "AWF start could not complete local Core database migrations.",
        (
            "Inspect migration and Postgres logs, fix the database state, then "
            "retry `awf start` or use expert service commands for recovery."
        ),
        "The local control-plane database rejected or failed a required schema migration.",
        "awf service logs --service api",
        _reason_catalog_link("START_MIGRATION_FAILED"),
    ),
    "START_HEALTH_TIMEOUT": _ReasonText(
        "AWF start timed out waiting for local Core health checks.",
        (
            "Inspect API, worker, and Postgres logs, fix the failing service, "
            "then rerun start with an appropriate timeout."
        ),
        "One or more local Core services did not become healthy before the start timeout.",
        "awf service status",
        _reason_catalog_link("START_HEALTH_TIMEOUT"),
    ),
    "DOCKER_OK": _ReasonText(
        "Docker daemon is reachable.",
        "No action required.",
        "",
        "",
        "",
    ),
    "DOCKER_CLI_NOT_FOUND": _ReasonText(
        "Docker CLI is not installed or is not on PATH.",
        "Install Docker Desktop or make the docker CLI available to the AWF service environment.",
        "The docker CLI is missing from the host environment or not accessible to the AWF process.",
        "awf service doctor",
        "https://docs.docker.com/get-docker/",
    ),
    "DOCKER_SOCKET_UNREACHABLE": _ReasonText(
        "Docker socket is not reachable.",
        "Start Docker Desktop or verify AWF_DOCKER_HOST.",
        "The Docker daemon is not running or the socket permissions are incorrect.",
        "awf service doctor",
        "https://docs.docker.com/config/daemon/",
    ),
    "DOCKER_DAEMON_UNREACHABLE": _ReasonText(
        "Docker is installed but the daemon is not reachable.",
        "Start Docker Desktop or verify AWF_DOCKER_HOST.",
        "The Docker daemon is stopped, crashing, or blocking connections.",
        "awf service doctor",
        "https://docs.docker.com/config/daemon/",
    ),
    "API_OK": _ReasonText(
        "AWF API health endpoint is reachable.",
        "No action required.",
        "",
        "",
        "",
    ),
    "API_UNREACHABLE": _ReasonText(
        "AWF API is not reachable.",
        "Run awf service bootstrap or inspect API logs.",
        "The local AWF service container is not running or port 8000 is blocked.",
        "awf service logs",
        "docs/REASON_CATALOG.md#api_unreachable",
    ),
    "ARTIFACT_BLOCKED": _ReasonText(
        "AWF blocked reading artifact content through MCP.",
        (
            "Inspect the workspace artifact directly from trusted local storage, "
            "or reduce/redact the artifact before requesting it through MCP."
        ),
        (
            "The artifact content appears unsafe to return, such as binary data, "
            "encoded secrets, or text that cannot be safely redacted."
        ),
        "awf workspace logs <workspace_id>",
        "docs/REASON_CATALOG.md#artifact_blocked",
    ),
    "ARTIFACT_OVERSIZED": _ReasonText(
        "AWF refused to read artifact content because it exceeded the configured byte limit.",
        (
            "Request a smaller artifact, lower the requested path scope, or use "
            "the REST artifact download path when an operator intentionally needs "
            "the full file."
        ),
        "The requested artifact is larger than the MCP read limit or grew beyond the limit while being read.",
        "awf workspace show <workspace_id>",
        "docs/REASON_CATALOG.md#artifact_oversized",
    ),
    "WORKSPACE_CREATE_RATE_LIMITED": _ReasonText(
        "AWF rejected a workspace creation request because the request-admission rate limit was exhausted.",
        (
            "Wait for the response's `Retry-After` delay, reduce workspace creation "
            "concurrency, or replay the original request with the same idempotency "
            "key and body when recovering from a lost response."
        ),
        (
            "Too many `POST /v1/workspaces` requests arrived for the same "
            "bearer-token or client-host identity within one admission window."
        ),
        "awf workspace list",
        "docs/REASON_CATALOG.md#workspace_create_rate_limited",
    ),
    "CALLBACK_REGISTER_RATE_LIMITED": _ReasonText(
        "AWF rejected a callback registration request because the request-admission rate limit was exhausted.",
        (
            "Wait for the response's `Retry-After` delay, reduce registration "
            "concurrency, or replay the original request with the same idempotency "
            "key and body when recovering from a lost response."
        ),
        (
            "Too many `POST /v1/callbacks` requests arrived for the same bearer-token "
            "or client-host identity within one admission window."
        ),
        "awf service logs",
        "docs/REASON_CATALOG.md#callback_register_rate_limited",
    ),
    "CALLBACK_TARGET_POLICY_VIOLATION": _ReasonText(
        "AWF refused to register or deliver an outbound callback because the target violated configured callback policy.",
        (
            "Update the callback subscription target or callback policy so the "
            "target is explicitly allowed, then let AWF retry pending deliveries."
        ),
        (
            "The target URL does not satisfy the HTTPS requirement or the "
            "configured callback host allowlist."
        ),
        "awf workspace logs <workspace_id>",
        "docs/REASON_CATALOG.md#callback_target_policy_violation",
    ),
    "CALLBACK_TARGET_INVALID": _ReasonText(
        "AWF refused to send an outbound callback because the stored callback target failed delivery-time validation.",
        (
            "Update or recreate the callback subscription with a public, "
            "policy-compliant target URL, then let AWF retry pending deliveries."
        ),
        (
            "The target URL no longer resolves to public addresses, uses a "
            "disallowed scheme or host, includes unsafe URL components, or "
            "violates the configured callback HTTPS/host allowlist policy."
        ),
        "awf workspace logs <workspace_id>",
        "docs/REASON_CATALOG.md#callback_target_invalid",
    ),
    "CALLBACK_TARGET_VALIDATION_TIMEOUT": _ReasonText(
        "AWF could not validate an outbound callback target before the delivery timeout budget expired.",
        (
            "Verify DNS and network reachability for the callback host, increase "
            "the subscription timeout if appropriate, then let AWF retry pending "
            "deliveries."
        ),
        "DNS resolution or callback target validation was too slow or blocked by network conditions.",
        "awf workspace logs <workspace_id>",
        "docs/REASON_CATALOG.md#callback_target_validation_timeout",
    ),
    "CALLBACK_DELIVERY_BUDGET_EXCEEDED": _ReasonText(
        (
            "AWF could not send an outbound callback because target validation "
            "consumed the full delivery timeout budget before the POST could start."
        ),
        (
            "Verify callback target DNS and network latency, increase the "
            "subscription timeout if appropriate, then let AWF retry pending "
            "deliveries."
        ),
        (
            "DNS resolution or target validation completed too slowly for the "
            "subscription's configured timeout."
        ),
        "awf workspace logs <workspace_id>",
        "docs/REASON_CATALOG.md#callback_delivery_budget_exceeded",
    ),
    "IDEMPOTENCY_REPLAY_UNAVAILABLE": _ReasonText(
        "AWF recognized an idempotency key as a replay key, but the original durable response could not be reconstructed.",
        (
            "Retry the exact original request if this is transient; use a fresh "
            "idempotency key only when intentionally creating a new resource."
        ),
        (
            "The in-memory replay response was evicted and the durable workspace "
            "or callback subscription record is no longer available."
        ),
        "awf service logs",
        "docs/REASON_CATALOG.md#idempotency_replay_unavailable",
    ),
    "WORKER_RUNNING": _ReasonText(
        "AWF worker container is running.",
        "No action required.",
        "",
        "",
        "",
    ),
    "WORKER_CONTAINER_MISSING": _ReasonText(
        "AWF worker container was not found in the local Compose project.",
        "Run awf service bootstrap to start the worker.",
        "The AWF service has not been bootstrapped on this machine.",
        "awf service bootstrap",
        "docs/REASON_CATALOG.md#worker_container_missing",
    ),
    "WORKER_CONTAINER_EXITED": _ReasonText(
        "AWF worker container has exited.",
        "Inspect worker logs with awf service logs --service worker.",
        "The worker process crashed due to configuration or resource limits.",
        "awf service logs --service worker",
        "docs/REASON_CATALOG.md#worker_container_exited",
    ),
    "WORKER_CONTAINER_NOT_RUNNING": _ReasonText(
        "AWF worker container is present but is not running.",
        "Run awf service bootstrap or inspect worker logs.",
        "The worker container was stopped manually or failed to start.",
        "awf service bootstrap",
        "docs/REASON_CATALOG.md#worker_container_not_running",
    ),
    "WORKER_UNHEALTHY": _ReasonText(
        "AWF worker container is running but Docker reports it unhealthy.",
        "Inspect worker logs with awf service logs --service worker.",
        "The worker background tasks are stalled or failing.",
        "awf service logs --service worker",
        "docs/REASON_CATALOG.md#worker_unhealthy",
    ),
    "WORKER_STATUS_UNAVAILABLE": _ReasonText(
        "AWF worker container status could not be inspected.",
        "Verify Docker is running and the local service Compose file exists.",
        "Docker is unresponsive or the local compose state is corrupted.",
        "docker compose ps",
        "docs/REASON_CATALOG.md#worker_status_unavailable",
    ),
    "WORKER_STATUS_UNPARSEABLE": _ReasonText(
        "AWF worker container status output could not be parsed.",
        "Upgrade Docker Compose or inspect `docker compose ps worker --format json` manually.",
        "Docker compose returned unexpected output format.",
        "docker compose ps --format json",
        "docs/REASON_CATALOG.md#worker_status_unparseable",
    ),
    "GITHUB_AUTH_OK": _ReasonText(
        "GitHub CLI auth is usable for PR operations.",
        "No action required.",
        "",
        "",
        "",
    ),
    "CODEX_AUTH_OK": _ReasonText(
        "Codex auth is usable for agent workspaces.",
        "No action required.",
        "",
        "",
        "",
    ),
    "CLAUDE_CODE_AUTH_OK": _ReasonText(
        "Claude Code auth is usable for agent workspaces.",
        "No action required.",
        "",
        "",
        "",
    ),
    "CURSOR_AUTH_OK": _ReasonText(
        "Cursor auth is usable for agent workspaces.",
        "No action required.",
        "",
        "",
        "",
    ),
    "GEMINI_AUTH_OK": _ReasonText(
        "Gemini auth is usable for agent workspaces.",
        "No action required.",
        "",
        "",
        "",
    ),
    "OPENCODE_AUTH_OK": _ReasonText(
        "OpenCode/Ollama auth is usable for agent workspaces.",
        "No action required.",
        "",
        "",
        "",
    ),
    "GROK_AUTH_OK": _ReasonText(
        "Grok Build auth is usable for agent workspaces.",
        "No action required.",
        "",
        "",
        "",
    ),
    "GITHUB_TOKEN_ENV_MISSING": _ReasonText(
        "No service-visible GitHub token was found.",
        "Set AWF_GITHUB_TOKEN from `gh auth token` before starting the service.",
        "GitHub CLI is not authenticated or token is not passed to the service.",
        "gh auth login",
        "docs/REASON_CATALOG.md#github_token_env_missing",
    ),
    "GITHUB_CLI_NOT_FOUND": _ReasonText(
        "GitHub token is present, but the gh CLI is not installed.",
        "Install gh in the service image or rebuild the local service image.",
        "The gh CLI is missing from the container environment.",
        "awf service bootstrap",
        "docs/REASON_CATALOG.md#github_cli_not_found",
    ),
    "GITHUB_AUTH_UNUSABLE": _ReasonText(
        "GitHub CLI auth is not usable for local service PR operations.",
        "Run gh auth status locally and refresh AWF_GITHUB_TOKEN if needed.",
        "The GitHub token is expired, invalid, or lacks required scopes.",
        "gh auth status",
        "docs/REASON_CATALOG.md#github_auth_unusable",
    ),
    "INVALID_GITHUB_REPO": _ReasonText(
        "A GitHub repository identifier or URL could not be parsed.",
        "Use an `owner/repo` slug or a valid GitHub repository URL.",
        "The PR adoption request used an unsupported repository format.",
        "awf workspace adopt-pr",
        "docs/REASON_CATALOG.md#invalid_github_repo",
    ),
    "PR_ADOPTION_INPUT_REQUIRED": _ReasonText(
        "A PR monitor adoption request omitted required input.",
        "Provide the repository and PR number, or use a complete GitHub PR URL.",
        "The request did not include enough repository or PR information to identify the PR.",
        "awf workspace adopt-pr",
        "docs/REASON_CATALOG.md#pr_adoption_input_required",
    ),
    "PR_ADOPTION_POLICY_CONFLICT": _ReasonText(
        "The requested PR cannot be adopted under the current workspace policy.",
        "Review the PR target and adoption options, then retry with policy-compatible input.",
        (
            "The PR targets an unsupported branch, conflicts with requested "
            "metadata, or violates adoption policy."
        ),
        "awf workspace adopt-pr",
        "docs/REASON_CATALOG.md#pr_adoption_policy_conflict",
    ),
    "PR_ALREADY_CLOSED": _ReasonText(
        "The PR selected for monitor adoption is already closed.",
        "Reopen the PR or adopt an active replacement PR.",
        "The PR was closed before AWF could adopt monitoring.",
        "awf workspace adopt-pr",
        "docs/REASON_CATALOG.md#pr_already_closed",
    ),
    "PR_ALREADY_MERGED": _ReasonText(
        "The PR selected for monitor adoption is already merged.",
        "No monitor adoption is needed; use workspace cleanup or status commands instead.",
        "There is no open PR monitor work left for AWF to own.",
        "awf workspace adopt-pr",
        "docs/REASON_CATALOG.md#pr_already_merged",
    ),
    "PR_METADATA_FETCH_FAILED": _ReasonText(
        "AWF could not fetch GitHub metadata for the requested PR.",
        "Verify `gh auth status`, repository access, and network connectivity, then retry adoption.",
        "GitHub auth, network access, rate limits, or repository permissions blocked the metadata query.",
        "gh pr view",
        "docs/REASON_CATALOG.md#pr_metadata_fetch_failed",
    ),
    "PR_METADATA_INVALID": _ReasonText(
        "GitHub returned PR metadata that AWF could not use safely.",
        "Inspect the PR metadata with `gh pr view --json` and retry after GitHub/API data is consistent.",
        "Required PR fields were missing or had an unexpected shape.",
        "gh pr view",
        "docs/REASON_CATALOG.md#pr_metadata_invalid",
    ),
    "PR_NOT_FOUND": _ReasonText(
        "The requested PR was not found.",
        "Confirm the repository, PR number, and GitHub permissions.",
        "The PR number is wrong, the repository is wrong, or the token lacks access.",
        "gh pr view",
        "docs/REASON_CATALOG.md#pr_not_found",
    ),
    "MERGE_METHOD_MISMATCH": _ReasonText(
        "AWF could not merge a PR because no effective merge method succeeded.",
        (
            "Inspect the repository and base-branch merge policy, enable a "
            "compatible merge method, or update the branch ruleset before "
            "remonitoring the workspace."
        ),
        (
            "Repository merge settings and base-branch rules left AWF with no "
            "allowed method, or GitHub rejected every allowed merge method."
        ),
        "awf workspace remonitor <workspace_id>",
        "docs/REASON_CATALOG.md#merge_method_mismatch",
    ),
    "GIT_FETCH_BASE_FAILED": _ReasonText(
        "The PR monitor could not refresh the target branch ref for a workspace.",
        (
            "Inspect the workspace monitor log and run `git fsck` on the AWF mirror. "
            "If AWF reports a repaired orphan ref, restart or remonitor the workspace; "
            "otherwise repair GitHub/network access before retrying."
        ),
        (
            "The shared git mirror has a broken AWF ref, network access to the remote "
            "failed, or the target branch no longer exists."
        ),
        "awf workspace logs <workspace_id>",
        "docs/REASON_CATALOG.md#git_fetch_base_failed",
    ),
    "GIT_BASE_FETCH_TRANSIENT_RETRY": _ReasonText(
        (
            "The PR monitor hit a transient GitHub or network error while refreshing "
            "the target branch and is retrying."
        ),
        (
            "No immediate action required. AWF will retry with bounded exponential "
            "backoff; inspect monitor logs if retries keep recurring."
        ),
        "GitHub returned a temporary 5xx response, timed out, or reset the connection during `git fetch`.",
        "awf workspace logs <workspace_id>",
        "docs/REASON_CATALOG.md#git_base_fetch_transient_retry",
    ),
    "GIT_BASE_FETCH_TRANSIENT_RETRY_EXHAUSTED": _ReasonText(
        "The PR monitor exhausted retries for transient target-branch fetch failures.",
        "Verify GitHub/network availability, then remonitor the workspace once the remote is reachable.",
        "GitHub or network access remained unavailable past the configured retry budget.",
        "awf workspace remonitor <workspace_id>",
        "docs/REASON_CATALOG.md#git_base_fetch_transient_retry_exhausted",
    ),
    "CI_TRANSIENT_RERUN_FAILED": _ReasonText(
        (
            "AWF could not request a GitHub rerun for CI failures classified "
            "as transient infrastructure failures."
        ),
        (
            "Verify `gh run rerun <run_id> --failed` works with the AWF "
            "GitHub token, then remonitor the workspace if the failure was transient."
        ),
        (
            "GitHub rejected the rerun request, the workflow run no longer "
            "exists, or the token lacks permission to rerun workflow jobs."
        ),
        "gh run rerun <run_id> --failed",
        "docs/REASON_CATALOG.md#ci_transient_rerun_failed",
    ),
    "PROTECTED_SCOPE_DIFF_UNAVAILABLE": _ReasonText(
        (
            "The PR monitor could not verify protected-scope changes against "
            "the remote PR branch before push."
        ),
        (
            "Verify GitHub/network access and the PR branch ref, then remonitor "
            "the workspace once the remote branch is reachable."
        ),
        (
            "The PR branch diff baseline could not be fetched because of a "
            "GitHub/network failure, a missing remote ref, or delayed ref replication."
        ),
        "awf workspace remonitor <workspace_id>",
        "docs/REASON_CATALOG.md#protected_scope_diff_unavailable",
    ),
    "PROTECTED_SCOPE_PUSH_BLOCKED": _ReasonText(
        (
            "The PR monitor refused to push because committed changes touched "
            "protected quality-gate paths outside the workspace owned paths."
        ),
        (
            "Inspect the blocked paths in the monitor event, remove or revert the "
            "out-of-scope quality-gate changes, then remonitor the workspace."
        ),
        (
            "A CI-fix, sync-base, or comment-addressing push included protected "
            "quality-gate files that the workspace profile does not own."
        ),
        "awf workspace logs <workspace_id>",
        "docs/REASON_CATALOG.md#protected_scope_push_blocked",
    ),
    "CODEX_AUTH_MISSING": _ReasonText(
        "No Codex auth signal was visible.",
        "Mount ~/.codex or set OPENAI_API_KEY, OPENAI_API_TOKEN, CODEX_API_KEY, or CODEX_AUTH_TOKEN.",
        "Missing Codex API credentials.",
        "awf service doctor",
        "docs/REASON_CATALOG.md#codex_auth_missing",
    ),
    "CLAUDE_AUTH_MISSING": _ReasonText(
        "No Claude Code auth signal was visible.",
        "Mount ~/.claude or set ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, or CLAUDE_CODE_OAUTH_TOKEN.",
        "Missing Claude API credentials.",
        "awf service doctor",
        "docs/REASON_CATALOG.md#claude_auth_missing",
    ),
    "CURSOR_AUTH_MISSING": _ReasonText(
        "No Cursor auth signal was visible.",
        "Set CURSOR_API_KEY before starting AWF.",
        "Missing Cursor API credentials.",
        "awf service doctor",
        "docs/REASON_CATALOG.md#cursor_auth_missing",
    ),
    "GEMINI_AUTH_MISSING": _ReasonText(
        "No Gemini auth signal was visible.",
        "Mount ~/.gemini or set GEMINI_API_KEY, GOOGLE_API_KEY, or GOOGLE_APPLICATION_CREDENTIALS.",
        "Missing Gemini API credentials.",
        "awf service doctor",
        "docs/REASON_CATALOG.md#gemini_auth_missing",
    ),
    "OPENCODE_OLLAMA_AUTH_MISSING": _ReasonText(
        "No OpenCode/Ollama auth signal was visible.",
        "Mount ~/.config/opencode, mount ~/.ollama auth files, or set OLLAMA_API_KEY.",
        "Missing OpenCode/Ollama credentials.",
        "awf service doctor",
        "docs/REASON_CATALOG.md#opencode_ollama_auth_missing",
    ),
    "GROK_AUTH_MISSING": _ReasonText(
        "No Grok Build auth signal was visible.",
        "Set XAI_API_KEY in the AWF service environment.",
        "Missing xAI API credentials for the official Grok Build CLI.",
        "awf service doctor",
        "https://docs.x.ai/build/enterprise",
    ),
    "GROK_RUNTIME_CLI_NOT_FOUND": _ReasonText(
        "The Grok Build CLI binary ('grok') was not found in the agent runtime container.",
        "Rebuild the agent-runtime Docker image or ensure 'grok' is installed on the PATH.",
        "The grok executable is missing or not executable.",
        "awf service doctor",
        "https://docs.x.ai/build/cli",
    ),
    "PORT_OPEN": _ReasonText(
        "Required local port is accepting connections.",
        "No action required.",
        "",
        "",
        "",
    ),
    "PORT_CLOSED": _ReasonText(
        "Required local port is not accepting connections.",
        "Start the AWF local service or free the configured port.",
        "The service is not running or the port is in use by another process.",
        "awf service bootstrap",
        "docs/REASON_CATALOG.md#port_closed",
    ),
    "PORT_CONFIG_INVALID": _ReasonText(
        "Required local port could not be derived from configuration.",
        "Fix the local AWF URL configuration and re-run doctor.",
        "Invalid AWF_API_URL or AWF_FRONTEND_URL.",
        "awf service doctor",
        "docs/REASON_CATALOG.md#port_config_invalid",
    ),
    "SUFFICIENT_DISK": _ReasonText(
        "Free disk is above the configured AWF threshold.",
        "No action required.",
        "",
        "",
        "",
    ),
    "INSUFFICIENT_DISK": _ReasonText(
        "Free disk is below the configured AWF threshold.",
        "Free disk before creating new workspaces or intentionally lower AWF_MIN_FREE_DISK_BYTES.",
        "Too many stopped containers, volumes, or large workspaces.",
        "docker system prune",
        "docs/REASON_CATALOG.md#insufficient_disk",
    ),
    "DISK_USAGE_UNAVAILABLE": _ReasonText(
        "Free disk could not be inspected for the AWF work directory.",
        "Verify AWF_WORK_DIR is accessible and re-run doctor.",
        "Permission denied or path does not exist.",
        "awf service doctor",
        "docs/REASON_CATALOG.md#disk_usage_unavailable",
    ),
    "NO_STRANDED_WORKSPACES": _ReasonText(
        "No stale or exited AWF workspace containers were detected.",
        "No action required.",
        "",
        "",
        "",
    ),
    "STRANDED_WORKSPACES_PRESENT": _ReasonText(
        "Stale or exited AWF workspace containers need operator review.",
        "Inspect the listed workspaces before running cleanup or recovery.",
        "Workspaces failed to tear down cleanly after task completion.",
        "awf workspace list",
        "docs/REASON_CATALOG.md#stranded_workspaces_present",
    ),
    "NO_ORPHANS": _ReasonText(
        "No orphan AWF Docker resources were detected.",
        "No action required.",
        "",
        "",
        "",
    ),
    "ORPHAN_RESOURCES_PRESENT": _ReasonText(
        "Orphan AWF Docker resources were detected.",
        "Review the listed resources before running cleanup.",
        "Networks or volumes left behind by deleted workspaces.",
        "awf service gc",
        "docs/REASON_CATALOG.md#orphan_resources_present",
    ),
    "NETWORK_POSTURE_NO_ACTIVE_OPEN": _ReasonText(
        "No active workspace is using open network posture.",
        "No action required.",
        "",
        "",
        "",
    ),
    "NETWORK_POSTURE_OPEN_ACTIVE": _ReasonText(
        "One or more active workspaces have unrestricted internet access.",
        "Confirm the open workspaces are trusted local work or recreate them with restricted/offline posture.",
        "Workspaces were started with --network=open.",
        "awf workspace list",
        "docs/REASON_CATALOG.md#network_posture_open_active",
    ),
    "NETWORK_POSTURE_UNAVAILABLE": _ReasonText(
        "Workspace network posture could not be inspected.",
        "Restore control-plane database access and re-run doctor.",
        "Cannot query the local database to check workspace posture.",
        "awf service doctor",
        "docs/REASON_CATALOG.md#network_posture_unavailable",
    ),
    "LOCAL_CONFIG_OK": _ReasonText(
        "Local AWF configuration looks usable.",
        "No action required.",
        "",
        "",
        "",
    ),
    "LOCAL_CONFIG_INVALID": _ReasonText(
        "Local AWF configuration has issues that block reliable service use.",
        "Fix the listed environment or path settings and re-run doctor.",
        "Invalid values in .env or missing required paths.",
        "awf service doctor",
        "docs/REASON_CATALOG.md#local_config_invalid",
    ),
    "MCP_EGRESS_AUDIT_ERROR": _ReasonText(
        "The MCP egress audit evidence tool could not read workspace audit evidence.",
        "Verify the workspace id and AWF database health, then retry the MCP call.",
        "The workspace lookup failed or the control-plane database was unavailable.",
        "awf workspace show <workspace_id>",
        "docs/REASON_CATALOG.md#mcp_egress_audit_error",
    ),
    "SERVICE_STATUS_COLLECTION_FAILED": _ReasonText(
        "AWF service status checks could not be collected.",
        "Fix the reported local configuration error and re-run doctor.",
        "Service discovery or database connection failed.",
        "awf service doctor",
        "docs/REASON_CATALOG.md#service_status_collection_failed",
    ),
    "COMPLETED_WORKSPACE_WITHOUT_PR": _ReasonText(
        "A completed workspace has no associated PR metadata.",
        "No action needed; the workspace is preserved by GC policy until explicitly destroyed.",
        "The workspace completed without ever creating or linking a PR.",
        "awf service gc",
        "docs/REASON_CATALOG.md#completed_workspace_without_pr",
    ),
    "COMPLETED_PR_NOT_MERGED": _ReasonText(
        "A completed workspace has a PR, but the PR has not been merged.",
        "Verify PR status; if merged, ensure pr_merge_sha is populated. GC preserves the workspace until the PR is confirmed merged.",
        "The PR is still open, was closed without merging, or the merge SHA was not recorded.",
        "awf service gc",
        "docs/REASON_CATALOG.md#completed_pr_not_merged",
    ),
    "CONFORMANCE_REQUIRES_AWF_VALIDATION": _ReasonText(
        (
            "Plan conformance found no deterministic implementation gap, but "
            "AWF-owned validation evidence is missing, stale, or insufficient."
        ),
        (
            "Let AWF run validation and rerun conformance against the persisted "
            "validation provenance and log stream references."
        ),
        (
            "The agent completed the implementation before AWF ran or persisted "
            "the required profile validation gates."
        ),
        "awf workspace show <workspace_id>",
        "docs/REASON_CATALOG.md#conformance_requires_awf_validation",
    ),
    "POST_AGENT_COMMIT_FAILED": _ReasonText(
        (
            "The post-agent ``git commit`` exited non-zero for a reason unrelated "
            "to a pre-commit hook (e.g. missing git identity, detached HEAD, "
            '"nothing to commit").'
        ),
        (
            "Inspect the worktree with ``awf workspace logs <workspace_id>`` and "
            "re-run the commit inside the worktree to reproduce; fix git "
            "identity or repository state and recover."
        ),
        (
            "Git environment misconfiguration in the workspace container or an "
            "agent that left the worktree in an unexpected state (orphan HEAD, "
            "empty index after stage)."
        ),
        "awf workspace logs <workspace_id>",
        "docs/REASON_CATALOG.md#post_agent_commit_failed",
    ),
    "POST_AGENT_COMMIT_FORMAT_REWRITE_NEEDED": _ReasonText(
        (
            "The post-agent ``git commit`` was rejected because "
            "``awf-ruff-format-check`` reported files would be reformatted, but "
            "AWF could not locate any agent-staged paths to repair "
            "(intersection with ``Would reformat:`` was empty)."
        ),
        (
            "Inspect the ``workspace.post_agent_commit_repair`` event. If the "
            "flagged path is intentionally part of the workspace change, run "
            "``uv run --python 3.12 --extra dev ruff format <flagged_path_1> "
            "<flagged_path_2> ...`` locally on the flagged paths, commit, and "
            "remonitor."
        ),
        (
            "The format check flagged files outside the agent's staged diff, so "
            "AWF refused to silently mutate them."
        ),
        "uv run --python 3.12 --extra dev ruff format <flagged_paths...>",
        "docs/REASON_CATALOG.md#post_agent_commit_format_rewrite_needed",
    ),
    "POST_AGENT_COMMIT_PRECOMMIT_FAILED": _ReasonText(
        (
            "A pre-commit hook rejected the post-agent commit after AWF exhausted "
            "bounded repair. Deterministic normalizer/formatter hooks are retried "
            "once by AWF. Ruff diagnostics marked ``[*]`` are repaired with a "
            "bounded ``ruff check --fix`` pass on already-staged Python paths. "
            "Remaining semantic hooks such as ``awf-ruff-check``, ``awf-mypy``, "
            "security, large-file, merge-conflict, or unknown hooks are routed "
            "through one targeted agent repair pass when the original agent run "
            "was healthy."
        ),
        (
            "Inspect ``details.post_agent_commit`` and the "
            "``workspace.post_agent_commit_repair`` event for ``repair_strategy``, "
            "``failed_hooks``, ``repaired_paths``, ``normalizer_paths``, "
            "``restaged_paths``, and ``retry_outcome``. Run ``uv run --python "
            "3.12 --extra dev pre-commit run --all-files`` locally against the "
            "workspace branch, fix the reported issues, push, and remonitor."
        ),
        (
            "The agent's diff trips a semantic lint/type/security invariant, a "
            "Ruff auto-fix changed the code but the retry still failed, or a "
            "deterministic repair/targeted repair retry still left pre-commit "
            "failing."
        ),
        "uv run --python 3.12 --extra dev pre-commit run --all-files",
        "docs/REASON_CATALOG.md#post_agent_commit_precommit_failed",
    ),
    "POST_AGENT_FORMAT_REPAIR_FAILED": _ReasonText(
        (
            "AWF detected a repairable post-agent pre-commit failure but the "
            "repair pipeline itself exited non-zero before the retry commit could run."
        ),
        "Inspect the workspace logs for the repair sub-step stderr, fix the toolchain or git state, and remonitor.",
        (
            "The workspace image is missing `uv` or dev extras, the pinned Python "
            "version is unavailable, `ruff` crashed on flagged paths, or the "
            "post-repair `git add` failed. The corresponding "
            "``workspace.post_agent_commit_repair`` event records "
            '``retry_outcome="error"``.'
        ),
        "awf workspace logs <workspace_id>",
        "docs/REASON_CATALOG.md#post_agent_format_repair_failed",
    ),
    "POST_AGENT_GIT_ADD_FAILED": _ReasonText(
        (
            "``git add -A`` failed during post-agent salvage (e.g. exit 128 with "
            "``fatal: not a git repository``)."
        ),
        "Inspect the worktree, recover any salvageable files manually, and recreate the workspace.",
        (
            "The agent damaged the worktree's git metadata or removed ``.git``; "
            "no commit could be attempted to capture work."
        ),
        "awf workspace logs <workspace_id>",
        "docs/REASON_CATALOG.md#post_agent_git_add_failed",
    ),
    "PROVIDER_AUTH_FAILED": _ReasonText(
        (
            "A workspace agent or PR monitor could not run because the selected "
            "LLM provider authentication failed."
        ),
        (
            "Refresh the provider credentials, restart or rebuild the AWF "
            "service/runtime if credentials are mounted into containers, then "
            "remonitor or reschedule the workspace."
        ),
        (
            "The provider token is expired, reused, missing inside the workspace "
            "runtime, or rejected by the provider CLI/API."
        ),
        "awf service doctor",
        "docs/REASON_CATALOG.md#provider_auth_failed",
    ),
}


def _reason_text(
    reason: str,
    *,
    label: str,
    status: DiagnosticStatus,
    context: Mapping[str, str] | None = None,
) -> _ReasonText:
    """Return catalog text or fallback diagnostic guidance for a reason."""
    text = _REASON_TEXT.get(reason)
    if text is None:
        if status == "ok":
            return _ReasonText(f"{label} check passed.", "No action required.", "", "", "")
        if status == "skipped":
            return _ReasonText(
                f"{label} check was skipped.",
                "Fix prerequisite checks first.",
                "Prerequisites failed.",
                "awf service doctor",
                "",
            )
        return _ReasonText(
            f"{label} check reported {status}.",
            "Inspect the diagnostic detail and the matching service status check.",
            "An unknown diagnostic check failed.",
            "awf service doctor",
            "",
        )
    if reason == "API_UNREACHABLE" and context and context.get("url"):
        return _ReasonText(
            f"AWF API is not reachable at {context['url']}.",
            text.action,
            text.likely_cause,
            text.related_command,
            text.docs_link,
        )
    if reason == "PORT_OPEN" and context and context.get("endpoint"):
        return _ReasonText(
            f"{context['endpoint']} is accepting connections.",
            text.action,
            text.likely_cause,
            text.related_command,
            text.docs_link,
        )
    return text
