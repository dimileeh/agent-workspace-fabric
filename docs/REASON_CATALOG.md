# AWF Reason and Error Code Catalog

This catalog documents common API/CLI/MCP failures, likely causes, and operator fixes.

### ACTIVE_EXECUTION_SALVAGE_VALIDATION_REQUESTED
**Problem:** AWF rewound a preserved active execution to run validate-only recovery.
**Likely Cause:** A worker restart found a stale active execution with recoverable committed work and created a validation recovery operation instead of failing the workspace.
**Operator Fix:** Let the worker dispatch the recovery validation. If it does not progress, inspect workspace events and active operations for the preserved execution.
**Related Command:** `awf workspace show <workspace_id>`
**Docs Link:** [docs/REASON_CATALOG.md#active_execution_salvage_validation_requested](#active_execution_salvage_validation_requested)

### AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED
**Problem:** AWF could not repair workspace runtime file ownership before running setup or committing monitor fixes.
**Likely Cause:** The local control-plane container could not chown the workspace worktree to the agent runtime UID/GID, often because the worktree or nested runtime state such as `.venv` is missing, read-only, or mounted with incompatible permissions.
**Operator Fix:** Inspect worker logs, verify the AWF work directory is writable by the control-plane container, then remonitor or recreate the workspace after fixing permissions.
**Related Command:** `awf service logs --service worker`
**Docs Link:** [docs/REASON_CATALOG.md#agent_runtime_ownership_repair_failed](#agent_runtime_ownership_repair_failed)

### API_UNREACHABLE
**Problem:** AWF API is not reachable.
**Likely Cause:** The local AWF service container is not running or port 8000 is blocked.
**Operator Fix:** Run awf service bootstrap or inspect API logs.
**Related Command:** `awf service logs`
**Docs Link:** [docs/REASON_CATALOG.md#api_unreachable](#api_unreachable)

### ARTIFACT_BLOCKED
**Problem:** AWF blocked reading artifact content through MCP.
**Likely Cause:** The artifact content appears unsafe to return, such as binary data, encoded secrets, or text that cannot be safely redacted.
**Operator Fix:** Inspect the workspace artifact directly from trusted local storage, or reduce/redact the artifact before requesting it through MCP.
**Related Command:** `awf workspace logs <workspace_id>`
**Docs Link:** [docs/REASON_CATALOG.md#artifact_blocked](#artifact_blocked)

### ARTIFACT_OVERSIZED
**Problem:** AWF refused to read artifact content because it exceeded the configured byte limit.
**Likely Cause:** The requested artifact is larger than the MCP read limit or grew beyond the limit while being read.
**Operator Fix:** Request a smaller artifact, lower the requested path scope, or use the REST artifact download path when an operator intentionally needs the full file.
**Related Command:** `awf workspace show <workspace_id>`
**Docs Link:** [docs/REASON_CATALOG.md#artifact_oversized](#artifact_oversized)

### AWF_SETUP_PLACEHOLDER
**Problem:** `awf setup` is registered but the first-run setup implementation is not active yet.
**Likely Cause:** This build contains the stable command surface before the setup checks are implemented.
**Operator Fix:** Use the current service bootstrap path for local Core startup, then use project init for repository onboarding until the setup slice lands.
**Related Command:** `awf service bootstrap`
**Docs Link:** [docs/REASON_CATALOG.md#awf_setup_placeholder](#awf_setup_placeholder)

### AWF_START_PLACEHOLDER
**Problem:** `awf start` is registered but the local Core start wrapper is not active yet.
**Likely Cause:** This build contains the stable command surface before the start wrapper is implemented.
**Operator Fix:** Use the current service bootstrap path for local Core startup, then use project init for repository onboarding until the start slice lands.
**Related Command:** `awf service bootstrap`
**Docs Link:** [docs/REASON_CATALOG.md#awf_start_placeholder](#awf_start_placeholder)

### BITBUCKET_AUTH_NOT_CONFIGURED
**Problem:** AWF could not build BitBucket Cloud credentials from the environment.
**Likely Cause:** The BitBucket auth mode, API token, or email is missing or malformed. App passwords are not supported; use an Atlassian API token.
**Operator Fix:** Set BITBUCKET_AUTH_MODE (basic|bearer) and BITBUCKET_API_TOKEN (plus BITBUCKET_EMAIL for basic mode) in the AWF service environment, then remonitor the workspace.
**Related Command:** `awf service doctor`
**Docs Link:** [docs/REASON_CATALOG.md#bitbucket_auth_not_configured](#bitbucket_auth_not_configured)

### BITBUCKET_ERROR
**Problem:** AWF hit a permanent BitBucket API fault while the PR monitor was performing a non-merge action (for example posting a comment or the human notification) and gave up on that action.
**Likely Cause:** BitBucket returned a non-transient error (such as 4xx authorization/validation) that retrying would not resolve, so the monitor operation finished as failed and re-raised.
**Operator Fix:** Inspect the workspace monitor log for the BitBucket response, fix the underlying cause (credentials, permissions, or PR/repo state), then remonitor the workspace.
**Related Command:** `awf workspace logs <workspace_id>`
**Docs Link:** [docs/REASON_CATALOG.md#bitbucket_error](#bitbucket_error)

### BITBUCKET_ISSUE_CAPTURE_FAILED
**Problem:** AWF could not durably capture a deferred review note: the BitBucket issue tracker is disabled and there was no PR context to fall back to, so neither an issue nor a PR comment was recorded.
**Likely Cause:** BitBucket returned 404 for the issues endpoint (tracker disabled) and AWF had not yet remembered the PR, leaving no comment fallback target.
**Operator Fix:** Treat the deferred follow-up as uncaptured — the thread stays unresolved and the merge is blocked. Enable the BitBucket repository issue tracker (or ensure PR context is available) and remonitor so the note can be captured.
**Related Command:** `awf workspace logs <workspace_id>`
**Docs Link:** [docs/REASON_CATALOG.md#bitbucket_issue_capture_failed](#bitbucket_issue_capture_failed)

### BITBUCKET_ISSUE_TRACKER_DISABLED
**Problem:** The BitBucket repository issue tracker is disabled, so AWF posted the tracking note as a pull-request comment instead of opening an issue.
**Likely Cause:** BitBucket returned 404 for the issues endpoint because the repository issue tracker is turned off.
**Operator Fix:** Enable the BitBucket repository issue tracker if durable issues are wanted; otherwise no action is required — the note was captured on the PR.
**Related Command:** `awf workspace logs <workspace_id>`
**Docs Link:** [docs/REASON_CATALOG.md#bitbucket_issue_tracker_disabled](#bitbucket_issue_tracker_disabled)

### BITBUCKET_MERGE_FAILED
**Problem:** BitBucket rejected the pull-request merge the PR monitor attempted, so the merge operation finished as failed.
**Likely Cause:** BitBucket returned a non-transient merge error (such as a merge conflict, failed merge check, or insufficient permissions) that AWF could not safely retry.
**Operator Fix:** Resolve the merge blocker on the PR (conflicts, required checks, or branch permissions) in BitBucket, then remonitor the workspace.
**Related Command:** `awf workspace logs <workspace_id>`
**Docs Link:** [docs/REASON_CATALOG.md#bitbucket_merge_failed](#bitbucket_merge_failed)

### BITBUCKET_PIPELINE_FULL_RERUN
**Problem:** AWF re-ran the entire BitBucket pipeline because BitBucket Cloud has no failed-only rerun API (that action is UI-only).
**Likely Cause:** BitBucket Cloud exposes only a whole-pipeline trigger over REST, so a transient-failure rerun necessarily reruns every step, not just the failed ones.
**Operator Fix:** No action required. The full pipeline was retriggered for the PR; inspect the monitor log if reruns keep recurring.
**Related Command:** `awf workspace logs <workspace_id>`
**Docs Link:** [docs/REASON_CATALOG.md#bitbucket_pipeline_full_rerun](#bitbucket_pipeline_full_rerun)

### BITBUCKET_PIPELINE_NOT_RERUNNABLE
**Problem:** AWF refused to rerun a BitBucket pipeline because the pull-request pipeline target could not be safely reconstructed.
**Likely Cause:** The failing pipeline was custom/manual, required variables, or lacked the source/destination commit metadata AWF needs to retrigger the correct PR pipeline — so AWF declined rather than trigger a wrong run.
**Operator Fix:** Re-run the pipeline from the BitBucket UI, or verify the PR pipeline configuration (custom/manual pipelines and required variables are not auto-rerunnable), then remonitor the workspace.
**Related Command:** `awf workspace logs <workspace_id>`
**Docs Link:** [docs/REASON_CATALOG.md#bitbucket_pipeline_not_rerunnable](#bitbucket_pipeline_not_rerunnable)

### BITBUCKET_TRANSIENT_ERROR
**Problem:** AWF hit a transient BitBucket API blip while the PR monitor was performing an action, so it waited and kept polling instead of failing the workspace outright.
**Likely Cause:** BitBucket returned a temporary error (such as a 5xx or rate-limit response) that is expected to clear on its own.
**Operator Fix:** Usually no action is required — the monitor retries automatically. If the condition persists across many polls, check BitBucket status and the workspace monitor log, then remonitor.
**Related Command:** `awf workspace logs <workspace_id>`
**Docs Link:** [docs/REASON_CATALOG.md#bitbucket_transient_error](#bitbucket_transient_error)

### CALLBACK_DELIVERY_BUDGET_EXCEEDED
**Problem:** AWF could not send an outbound callback because target validation consumed the full delivery timeout budget before the POST could start.
**Likely Cause:** DNS resolution or target validation completed too slowly for the subscription's configured timeout.
**Operator Fix:** Verify callback target DNS and network latency, increase the subscription timeout if appropriate, then let AWF retry pending deliveries.
**Related Command:** `awf workspace logs <workspace_id>`
**Docs Link:** [docs/REASON_CATALOG.md#callback_delivery_budget_exceeded](#callback_delivery_budget_exceeded)

### CALLBACK_REGISTER_RATE_LIMITED
**Problem:** AWF rejected a callback registration request because the request-admission rate limit was exhausted.
**Likely Cause:** Too many `POST /v1/callbacks` requests arrived for the same bearer-token or client-host identity within one admission window.
**Operator Fix:** Wait for the response's `Retry-After` delay, reduce registration concurrency, or replay the original request with the same idempotency key and body when recovering from a lost response.
**Related Command:** `awf service logs`
**Docs Link:** [docs/REASON_CATALOG.md#callback_register_rate_limited](#callback_register_rate_limited)

### CALLBACK_TARGET_INVALID
**Problem:** AWF refused to send an outbound callback because the stored callback target failed delivery-time validation.
**Likely Cause:** The target URL no longer resolves to public addresses, uses a disallowed scheme or host, includes unsafe URL components, or violates the configured callback HTTPS/host allowlist policy.
**Operator Fix:** Update or recreate the callback subscription with a public, policy-compliant target URL, then let AWF retry pending deliveries.
**Related Command:** `awf workspace logs <workspace_id>`
**Docs Link:** [docs/REASON_CATALOG.md#callback_target_invalid](#callback_target_invalid)

### CALLBACK_TARGET_POLICY_VIOLATION
**Problem:** AWF refused to register or deliver an outbound callback because the target violated configured callback policy.
**Likely Cause:** The target URL does not satisfy the HTTPS requirement or the configured callback host allowlist.
**Operator Fix:** Update the callback subscription target or callback policy so the target is explicitly allowed, then let AWF retry pending deliveries.
**Related Command:** `awf workspace logs <workspace_id>`
**Docs Link:** [docs/REASON_CATALOG.md#callback_target_policy_violation](#callback_target_policy_violation)

### CALLBACK_TARGET_VALIDATION_TIMEOUT
**Problem:** AWF could not validate an outbound callback target before the delivery timeout budget expired.
**Likely Cause:** DNS resolution or callback target validation was too slow or blocked by network conditions.
**Operator Fix:** Verify DNS and network reachability for the callback host, increase the subscription timeout if appropriate, then let AWF retry pending deliveries.
**Related Command:** `awf workspace logs <workspace_id>`
**Docs Link:** [docs/REASON_CATALOG.md#callback_target_validation_timeout](#callback_target_validation_timeout)

### CI_TRANSIENT_RERUN_FAILED
**Problem:** AWF could not request a GitHub rerun for CI failures classified as transient infrastructure failures.
**Likely Cause:** GitHub rejected the rerun request, the workflow run no longer exists, or the token lacks permission to rerun workflow jobs.
**Operator Fix:** Verify `gh run rerun <run_id> --failed` works with the AWF GitHub token, then remonitor the workspace if the failure was transient.
**Related Command:** `gh run rerun <run_id> --failed`
**Docs Link:** [docs/REASON_CATALOG.md#ci_transient_rerun_failed](#ci_transient_rerun_failed)

### CLAUDE_AUTH_MISSING
**Problem:** No Claude Code auth signal was visible.
**Likely Cause:** Missing Claude API credentials.
**Operator Fix:** Mount ~/.claude or set ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, or CLAUDE_CODE_OAUTH_TOKEN.
**Related Command:** `awf service doctor`
**Docs Link:** [docs/REASON_CATALOG.md#claude_auth_missing](#claude_auth_missing)

### CLIENT_CONFIG_CONFLICT
**Problem:** AWF found a client MCP configuration conflict.
**Likely Cause:** A Claude or Codex MCP config already contains incompatible AWF server settings.
**Operator Fix:** Review the proposed diff or existing client config, resolve the conflicting server entry, then rerun setup for that client.
**Related Command:** `awf setup --client <client>`
**Docs Link:** [docs/REASON_CATALOG.md#client_config_conflict](#client_config_conflict)

### CLIENT_CONFIG_WRITE_FAILED
**Problem:** AWF could not write a client MCP configuration file.
**Likely Cause:** The client config path is missing, read-only, locked by another process, or failed atomic write.
**Operator Fix:** Check file permissions and parent directories, preserve any backup, then rerun setup for the selected client.
**Related Command:** `awf setup --client <client>`
**Docs Link:** [docs/REASON_CATALOG.md#client_config_write_failed](#client_config_write_failed)

### CODEX_AUTH_MISSING
**Problem:** No Codex auth signal was visible.
**Likely Cause:** Missing Codex API credentials.
**Operator Fix:** Mount ~/.codex or set OPENAI_API_KEY, OPENAI_API_TOKEN, CODEX_API_KEY, or CODEX_AUTH_TOKEN.
**Related Command:** `awf service doctor`
**Docs Link:** [docs/REASON_CATALOG.md#codex_auth_missing](#codex_auth_missing)

### COMPLETED_PR_NOT_MERGED
**Problem:** A completed workspace has a PR, but the PR has not been merged.
**Likely Cause:** The PR is still open, was closed without merging, or the merge SHA was not recorded.
**Operator Fix:** Verify PR status; if merged, ensure pr_merge_sha is populated. GC preserves the workspace until the PR is confirmed merged.
**Related Command:** `awf service gc`
**Docs Link:** [docs/REASON_CATALOG.md#completed_pr_not_merged](#completed_pr_not_merged)

### COMPLETED_WORKSPACE_WITHOUT_PR
**Problem:** A completed workspace has no associated PR metadata.
**Likely Cause:** The workspace completed without ever creating or linking a PR.
**Operator Fix:** No action needed; the workspace is preserved by GC policy until explicitly destroyed.
**Related Command:** `awf service gc`
**Docs Link:** [docs/REASON_CATALOG.md#completed_workspace_without_pr](#completed_workspace_without_pr)

### CONFORMANCE_REQUIRES_AWF_VALIDATION
**Problem:** Plan conformance found no deterministic implementation gap, but AWF-owned validation evidence is missing, stale, or insufficient.
**Likely Cause:** The agent completed the implementation before AWF ran or persisted the required profile validation gates.
**Operator Fix:** Let AWF run validation and rerun conformance against the persisted validation provenance and log stream references.
**Related Command:** `awf workspace show <workspace_id>`
**Docs Link:** [docs/REASON_CATALOG.md#conformance_requires_awf_validation](#conformance_requires_awf_validation)

### CREDENTIAL_BACKEND_UNAVAILABLE
**Problem:** AWF could not access a usable credential storage backend.
**Likely Cause:** The preferred keychain backend is unavailable and no safe fallback was selected.
**Operator Fix:** Configure the OS keychain backend, provide env refs, or explicitly choose the approved plain-file fallback when that policy applies.
**Related Command:** `awf setup --provider <provider>`
**Docs Link:** [docs/REASON_CATALOG.md#credential_backend_unavailable](#credential_backend_unavailable)

### CREDENTIAL_PLAIN_FILE_CONSENT_REQUIRED
**Problem:** AWF refused to store a provider credential in a plain file without explicit consent.
**Likely Cause:** Plain-file provider secrets are an opt-in fallback and cannot be selected silently.
**Operator Fix:** Use keyring or env refs, or rerun setup with the explicit plain-file consent flag only on an approved headless Linux path.
**Related Command:** `awf setup --allow-plain-secrets`
**Docs Link:** [docs/REASON_CATALOG.md#credential_plain_file_consent_required](#credential_plain_file_consent_required)

### CREDENTIAL_REF_INVALID
**Problem:** AWF rejected a malformed provider credential reference.
**Likely Cause:** The credential reference uses an unsupported scheme, unsafe path, or raw secret value.
**Operator Fix:** Use a `keyring://`, `env://`, or approved `plain-file://` reference and keep raw token values out of config and JSON payloads.
**Related Command:** `awf setup --provider <provider>`
**Docs Link:** [docs/REASON_CATALOG.md#credential_ref_invalid](#credential_ref_invalid)

### CURSOR_AUTH_MISSING
**Problem:** No Cursor auth signal was visible.
**Likely Cause:** Missing Cursor API credentials.
**Operator Fix:** Set CURSOR_API_KEY before starting AWF.
**Related Command:** `awf service doctor`
**Docs Link:** [docs/REASON_CATALOG.md#cursor_auth_missing](#cursor_auth_missing)

### DISK_USAGE_UNAVAILABLE
**Problem:** Free disk could not be inspected for the AWF work directory.
**Likely Cause:** Permission denied or path does not exist.
**Operator Fix:** Verify AWF_WORK_DIR is accessible and re-run doctor.
**Related Command:** `awf service doctor`
**Docs Link:** [docs/REASON_CATALOG.md#disk_usage_unavailable](#disk_usage_unavailable)

### DOCKER_CLI_NOT_FOUND
**Problem:** Docker CLI is not installed or is not on PATH.
**Likely Cause:** The docker CLI is missing from the host environment or not accessible to the AWF process.
**Operator Fix:** Install Docker Desktop or make the docker CLI available to the AWF service environment.
**Related Command:** `awf service doctor`
**Docs Link:** [https://docs.docker.com/get-docker/](https://docs.docker.com/get-docker/)

### DOCKER_DAEMON_UNREACHABLE
**Problem:** Docker is installed but the daemon is not reachable.
**Likely Cause:** The Docker daemon is stopped, crashing, or blocking connections.
**Operator Fix:** Start Docker Desktop or verify AWF_DOCKER_HOST.
**Related Command:** `awf service doctor`
**Docs Link:** [https://docs.docker.com/config/daemon/](https://docs.docker.com/config/daemon/)

### DOCKER_SOCKET_UNREACHABLE
**Problem:** Docker socket is not reachable.
**Likely Cause:** The Docker daemon is not running or the socket permissions are incorrect.
**Operator Fix:** Start Docker Desktop or verify AWF_DOCKER_HOST.
**Related Command:** `awf service doctor`
**Docs Link:** [https://docs.docker.com/config/daemon/](https://docs.docker.com/config/daemon/)

### DUPLICATE_HOST_PORT
**Problem:** The same host port is claimed by more than one service or companion within a single workspace create request.
**Likely Cause:** A companion and a profile service, or two profile services, bind the same Docker host port in the same request. The database conflict check cannot detect this because the new workspace is not persisted yet.
**Operator Fix:** Change your profile or companion configuration so that each service binds a unique host port, then retry the create request.
**Related Command:** `awf workspace list`
**Docs Link:** [docs/REASON_CATALOG.md#duplicate_host_port](#duplicate_host_port)

### FORGE_NOT_SUPPORTED
**Problem:** AWF detected a code forge it does not support. GitHub and BitBucket Cloud are implemented; any other forge fails fast.
**Likely Cause:** The workspace repository URL resolved to an unsupported forge (a host other than github.com or bitbucket.org), or the workspace profile set an unsupported `forge:` value. AWF fails fast instead of mis-routing to GitHub.
**Operator Fix:** Use a GitHub or BitBucket Cloud repository, or track support for the detected forge upstream. Recreate the workspace against a supported remote (github.com or bitbucket.org).
**Related Command:** `awf workspace create`
**Docs Link:** [docs/REASON_CATALOG.md#forge_not_supported](#forge_not_supported)

### GEMINI_AUTH_MISSING
**Problem:** No Gemini auth signal was visible.
**Likely Cause:** Missing Gemini API credentials.
**Operator Fix:** Mount ~/.gemini or set GEMINI_API_KEY, GOOGLE_API_KEY, or GOOGLE_APPLICATION_CREDENTIALS.
**Related Command:** `awf service doctor`
**Docs Link:** [docs/REASON_CATALOG.md#gemini_auth_missing](#gemini_auth_missing)

### GITHUB_AUTH_UNUSABLE
**Problem:** GitHub CLI auth is not usable for local service PR operations.
**Likely Cause:** The GitHub token is expired, invalid, or lacks required scopes.
**Operator Fix:** Run gh auth status locally and refresh AWF_GITHUB_TOKEN if needed.
**Related Command:** `gh auth status`
**Docs Link:** [docs/REASON_CATALOG.md#github_auth_unusable](#github_auth_unusable)

### GITHUB_CLI_NOT_FOUND
**Problem:** GitHub token is present, but the gh CLI is not installed.
**Likely Cause:** The gh CLI is missing from the container environment.
**Operator Fix:** Install gh in the service image or rebuild the local service image.
**Related Command:** `awf service bootstrap`
**Docs Link:** [docs/REASON_CATALOG.md#github_cli_not_found](#github_cli_not_found)

### GITHUB_TOKEN_ENV_MISSING
**Problem:** No service-visible GitHub token was found.
**Likely Cause:** GitHub CLI is not authenticated or token is not passed to the service.
**Operator Fix:** Set AWF_GITHUB_TOKEN from `gh auth token` before starting the service.
**Related Command:** `gh auth login`
**Docs Link:** [docs/REASON_CATALOG.md#github_token_env_missing](#github_token_env_missing)

### GIT_BASE_FETCH_TRANSIENT_RETRY
**Problem:** The PR monitor hit a transient GitHub or network error while refreshing the target branch and is retrying.
**Likely Cause:** GitHub returned a temporary 5xx response, timed out, or reset the connection during `git fetch`.
**Operator Fix:** No immediate action required. AWF will retry with bounded exponential backoff; inspect monitor logs if retries keep recurring.
**Related Command:** `awf workspace logs <workspace_id>`
**Docs Link:** [docs/REASON_CATALOG.md#git_base_fetch_transient_retry](#git_base_fetch_transient_retry)

### GIT_BASE_FETCH_TRANSIENT_RETRY_EXHAUSTED
**Problem:** The PR monitor exhausted retries for transient target-branch fetch failures.
**Likely Cause:** GitHub or network access remained unavailable past the configured retry budget.
**Operator Fix:** Verify GitHub/network availability, then remonitor the workspace once the remote is reachable.
**Related Command:** `awf workspace remonitor <workspace_id>`
**Docs Link:** [docs/REASON_CATALOG.md#git_base_fetch_transient_retry_exhausted](#git_base_fetch_transient_retry_exhausted)

### GIT_FETCH_BASE_FAILED
**Problem:** The PR monitor could not refresh the target branch ref for a workspace.
**Likely Cause:** The shared git mirror has a broken AWF ref, network access to the remote failed, or the target branch no longer exists.
**Operator Fix:** Inspect the workspace monitor log and run `git fsck` on the AWF mirror. If AWF reports a repaired orphan ref, restart or remonitor the workspace; otherwise repair GitHub/network access before retrying.
**Related Command:** `awf workspace logs <workspace_id>`
**Docs Link:** [docs/REASON_CATALOG.md#git_fetch_base_failed](#git_fetch_base_failed)

### GROK_AUTH_MISSING
**Problem:** No Grok Build auth signal was visible.
**Likely Cause:** Missing xAI API credentials for the official Grok Build CLI.
**Operator Fix:** Mount ~/.grok or set XAI_API_KEY in the AWF service environment.
**Related Command:** `awf service doctor`
**Docs Link:** [https://docs.x.ai/build/enterprise](https://docs.x.ai/build/enterprise)

### GROK_RUNTIME_CLI_NOT_FOUND
**Problem:** The Grok Build CLI binary ('grok') was not found in the agent runtime container.
**Likely Cause:** The grok executable is missing or not executable.
**Operator Fix:** Rebuild the agent-runtime Docker image or ensure 'grok' is installed on the PATH.
**Related Command:** `awf service doctor`
**Docs Link:** [https://docs.x.ai/build/cli](https://docs.x.ai/build/cli)

### HOST_PORT_CONFLICT
**Problem:** AWF rejected a workspace create or retry because a host port needed by the new workspace is already in use by another active or unreleased workspace.
**Likely Cause:** Another workspace's profile services or companions bind the same Docker host port and its compose stack is still running, or the workspace is terminal but has not yet released its runtime resources (no `workspace.terminal_runtime_released` event exists). For auto-resolved profiles, the conflict may also be detected at provision time by the provisioner's host-port re-check (``_check_auto_resolved_profile_host_ports``) rather than at dispatch, surfacing as an ``INFRASTRUCTURE_FAILURE`` instead of a 409.
**Operator Fix:** Wait for the conflicting workspace to release its ports (destroy, complete, or have its runtime released), then retry. Use `awf workspace show <conflicting_workspace_id>` to check its status and events.
**Related Command:** `awf workspace list`
**Docs Link:** [docs/REASON_CATALOG.md#host_port_conflict](#host_port_conflict)

### HOST_SETUP_CONFIG_CORRUPT
**Problem:** AWF could not read the host setup config.
**Likely Cause:** The config file is malformed, has duplicate keys, uses unsupported schema versions, or cannot be interpreted safely.
**Operator Fix:** Inspect `~/.awf/config.yml`, remove invalid YAML or unsupported fields, then rerun `awf setup`.
**Related Command:** `awf setup`
**Docs Link:** [docs/REASON_CATALOG.md#host_setup_config_corrupt](#host_setup_config_corrupt)

### HOST_SETUP_CONFIG_SECRET_VALUE
**Problem:** AWF rejected a host setup config value that looks like a secret.
**Likely Cause:** Host setup config stores references and metadata only, never raw provider credentials.
**Operator Fix:** Replace raw credentials in `~/.awf/config.yml` with keyring, env, or approved plain-file refs, then rerun setup.
**Related Command:** `awf setup --provider <provider>`
**Docs Link:** [docs/REASON_CATALOG.md#host_setup_config_secret_value](#host_setup_config_secret_value)

### HOST_SETUP_CONFIG_WRITE_FAILED
**Problem:** AWF could not write the host setup config.
**Likely Cause:** The host config path or parent directory is missing, read-only, or refused atomic writes.
**Operator Fix:** Verify the `~/.awf` directory exists, is writable by your user, and is not blocked by filesystem permissions, then rerun setup.
**Related Command:** `awf setup`
**Docs Link:** [docs/REASON_CATALOG.md#host_setup_config_write_failed](#host_setup_config_write_failed)

### IDEMPOTENCY_REPLAY_UNAVAILABLE
**Problem:** AWF recognized an idempotency key as a replay key, but the original durable response could not be reconstructed.
**Likely Cause:** The in-memory replay response was evicted and the durable workspace or callback subscription record is no longer available.
**Operator Fix:** Retry the exact original request if this is transient; use a fresh idempotency key only when intentionally creating a new resource.
**Related Command:** `awf service logs`
**Docs Link:** [docs/REASON_CATALOG.md#idempotency_replay_unavailable](#idempotency_replay_unavailable)

### INSTALLER_CHECKSUM_MISMATCH
**Problem:** The AWF installer refused an artifact whose checksum did not match the manifest.
**Likely Cause:** The downloaded wheel or source artifact differs from the manifest-pinned sha256.
**Operator Fix:** Do not run the downloaded artifact; retry against the canonical release manifest or choose a different pinned version.
**Related Command:** `awf --version`
**Docs Link:** [docs/REASON_CATALOG.md#installer_checksum_mismatch](#installer_checksum_mismatch)

### INSTALLER_DEPENDENCY_MISSING
**Problem:** The AWF installer is missing a required host dependency.
**Likely Cause:** The selected install method depends on a host tool that is not available on PATH.
**Operator Fix:** Install the reported dependency such as `curl`, `tar`, `sh`, `uv`, or `pipx`, then rerun the installer.
**Related Command:** `awf --help`
**Docs Link:** [docs/REASON_CATALOG.md#installer_dependency_missing](#installer_dependency_missing)

### INSTALLER_METHOD_FAILED
**Problem:** The AWF installer method failed before AWF was installed.
**Likely Cause:** The selected uv, pipx, or manual install command exited non-zero.
**Operator Fix:** Inspect the reported method output, fix package-manager or network issues, and retry the same pinned install method.
**Related Command:** `uv tool install agent-workspace-fabric`
**Docs Link:** [docs/REASON_CATALOG.md#installer_method_failed](#installer_method_failed)

### INSTALLER_PATH_NOT_REACHABLE
**Problem:** The AWF installer completed but the `awf` executable is not reachable on PATH.
**Likely Cause:** The tool install location is not visible to the current shell environment.
**Operator Fix:** Open a new shell or update PATH using the installer-provided shell hint, then verify with `awf --version`.
**Related Command:** `awf --version`
**Docs Link:** [docs/REASON_CATALOG.md#installer_path_not_reachable](#installer_path_not_reachable)

### INSTALLER_UNSUPPORTED_PLATFORM
**Problem:** The AWF installer does not support this operating system or architecture.
**Likely Cause:** The installer detected an OS or CPU architecture outside the supported first release matrix.
**Operator Fix:** Use a supported macOS/Linux platform, WSL where documented, or the manual Python tool install path for this environment.
**Related Command:** `uv tool install agent-workspace-fabric`
**Docs Link:** [docs/REASON_CATALOG.md#installer_unsupported_platform](#installer_unsupported_platform)

### INSUFFICIENT_DISK
**Problem:** Free disk is below the configured AWF threshold.
**Likely Cause:** Too many stopped containers, volumes, or large workspaces.
**Operator Fix:** Free disk before creating new workspaces or intentionally lower AWF_MIN_FREE_DISK_BYTES.
**Related Command:** `docker system prune`
**Docs Link:** [docs/REASON_CATALOG.md#insufficient_disk](#insufficient_disk)

### INTERACTIVE_INPUT_REQUIRED
**Problem:** AWF setup needs interactive input that is unavailable in this run.
**Likely Cause:** A non-interactive setup path reached a prompt that would collect required settings or credentials.
**Operator Fix:** Run setup interactively, or provide safe env/keychain references for the requested provider before retrying non-interactive setup.
**Related Command:** `awf setup --non-interactive`
**Docs Link:** [docs/REASON_CATALOG.md#interactive_input_required](#interactive_input_required)

### INVALID_GITHUB_REPO
**Problem:** A GitHub repository identifier or URL could not be parsed.
**Likely Cause:** The PR adoption request used an unsupported repository format.
**Operator Fix:** Use an `owner/repo` slug or a valid GitHub repository URL.
**Related Command:** `awf workspace adopt-pr`
**Docs Link:** [docs/REASON_CATALOG.md#invalid_github_repo](#invalid_github_repo)

### LOCAL_CONFIG_INVALID
**Problem:** Local AWF configuration has issues that block reliable service use.
**Likely Cause:** Invalid values in .env or missing required paths.
**Operator Fix:** Fix the listed environment or path settings and re-run doctor.
**Related Command:** `awf service doctor`
**Docs Link:** [docs/REASON_CATALOG.md#local_config_invalid](#local_config_invalid)

### MCP_EGRESS_AUDIT_ERROR
**Problem:** The MCP egress audit evidence tool could not read workspace audit evidence.
**Likely Cause:** The workspace lookup failed or the control-plane database was unavailable.
**Operator Fix:** Verify the workspace id and AWF database health, then retry the MCP call.
**Related Command:** `awf workspace show <workspace_id>`
**Docs Link:** [docs/REASON_CATALOG.md#mcp_egress_audit_error](#mcp_egress_audit_error)

### MERGE_METHOD_MISMATCH
**Problem:** AWF could not merge a PR because no effective merge method succeeded.
**Likely Cause:** Repository merge settings and base-branch rules left AWF with no allowed method, or GitHub rejected every allowed merge method.
**Operator Fix:** Inspect the repository and base-branch merge policy, enable a compatible merge method, or update the branch ruleset before remonitoring the workspace.
**Related Command:** `awf workspace remonitor <workspace_id>`
**Docs Link:** [docs/REASON_CATALOG.md#merge_method_mismatch](#merge_method_mismatch)

### MONITOR_RECOVERY_CANCELLED
**Problem:** AWF cancelled a PR-monitor recovery (remonitor) operation before it finished resuming the monitor.
**Likely Cause:** A stale-monitor reconcile cancelled the resume task because the workspace left the monitoring_pr state while recovery was still dispatching. This is the expected outcome of normal reconciliation, not a runtime error.
**Operator Fix:** No action is usually required. If the workspace should still be monitored, remonitor it and confirm it is in monitoring_pr.
**Related Command:** `awf workspace show <workspace_id>`
**Docs Link:** [docs/REASON_CATALOG.md#monitor_recovery_cancelled](#monitor_recovery_cancelled)

### NETWORK_POSTURE_OPEN_ACTIVE
**Problem:** One or more active workspaces have unrestricted internet access.
**Likely Cause:** Workspaces were started with --network=open.
**Operator Fix:** Confirm the open workspaces are trusted local work or recreate them with restricted/offline posture.
**Related Command:** `awf workspace list`
**Docs Link:** [docs/REASON_CATALOG.md#network_posture_open_active](#network_posture_open_active)

### NETWORK_POSTURE_UNAVAILABLE
**Problem:** Workspace network posture could not be inspected.
**Likely Cause:** Cannot query the local database to check workspace posture.
**Operator Fix:** Restore control-plane database access and re-run doctor.
**Related Command:** `awf service doctor`
**Docs Link:** [docs/REASON_CATALOG.md#network_posture_unavailable](#network_posture_unavailable)

### OPENCODE_OLLAMA_AUTH_MISSING
**Problem:** No OpenCode/Ollama auth signal was visible.
**Likely Cause:** Missing OpenCode/Ollama credentials.
**Operator Fix:** Mount ~/.config/opencode, mount ~/.ollama auth files, or set OLLAMA_API_KEY.
**Related Command:** `awf service doctor`
**Docs Link:** [docs/REASON_CATALOG.md#opencode_ollama_auth_missing](#opencode_ollama_auth_missing)

### OPEN_PR_RESOLVER_FORGE_NOT_SUPPORTED
**Problem:** AWF could not recover the open PR for a preserved workspace because the open-PR resolver only supports GitHub.
**Likely Cause:** The workspace is on a supported non-GitHub forge (e.g. BitBucket Cloud), but the GitHub-only open-PR resolver cannot look up its open PR by branch yet, so worker recovery fails fast instead of querying the branch as a same-slug GitHub repo.
**Operator Fix:** No repository change is needed — the forge is supported. Adopt the PR monitor explicitly with `awf workspace adopt-pr`, or remonitor once a forge-neutral open-PR resolver lands.
**Related Command:** `awf workspace adopt-pr`
**Docs Link:** [docs/REASON_CATALOG.md#open_pr_resolver_forge_not_supported](#open_pr_resolver_forge_not_supported)

### ORPHAN_RESOURCES_PRESENT
**Problem:** Orphan AWF Docker resources were detected.
**Likely Cause:** Networks or volumes left behind by deleted workspaces.
**Operator Fix:** Review the listed resources before running cleanup.
**Related Command:** `awf service gc`
**Docs Link:** [docs/REASON_CATALOG.md#orphan_resources_present](#orphan_resources_present)

### PORT_CLOSED
**Problem:** Required local port is not accepting connections.
**Likely Cause:** The service is not running or the port is in use by another process.
**Operator Fix:** Start the AWF local service or free the configured port.
**Related Command:** `awf service bootstrap`
**Docs Link:** [docs/REASON_CATALOG.md#port_closed](#port_closed)

### PORT_CONFIG_INVALID
**Problem:** Required local port could not be derived from configuration.
**Likely Cause:** Invalid AWF_API_URL or AWF_FRONTEND_URL.
**Operator Fix:** Fix the local AWF URL configuration and re-run doctor.
**Related Command:** `awf service doctor`
**Docs Link:** [docs/REASON_CATALOG.md#port_config_invalid](#port_config_invalid)

### POST_AGENT_COMMIT_FAILED
**Problem:** The post-agent ``git commit`` exited non-zero for a reason unrelated to a pre-commit hook (e.g. missing git identity, detached HEAD, "nothing to commit").
**Likely Cause:** Git environment misconfiguration in the workspace container or an agent that left the worktree in an unexpected state (orphan HEAD, empty index after stage).
**Operator Fix:** Inspect the worktree with ``awf workspace logs <workspace_id>`` and re-run the commit inside the worktree to reproduce; fix git identity or repository state and recover.
**Related Command:** `awf workspace logs <workspace_id>`
**Docs Link:** [docs/REASON_CATALOG.md#post_agent_commit_failed](#post_agent_commit_failed)

### POST_AGENT_COMMIT_FORMAT_REWRITE_NEEDED
**Problem:** The post-agent ``git commit`` was rejected because ``awf-ruff-format-check`` reported files would be reformatted, but AWF could not locate any agent-staged paths to repair (intersection with ``Would reformat:`` was empty).
**Likely Cause:** The format check flagged files outside the agent's staged diff, so AWF refused to silently mutate them.
**Operator Fix:** Inspect the ``workspace.post_agent_commit_repair`` event. If the flagged path is intentionally part of the workspace change, run ``uv run --python 3.12 --extra dev ruff format <flagged_path_1> <flagged_path_2> ...`` locally on the flagged paths, commit, and remonitor.
**Related Command:** `uv run --python 3.12 --extra dev ruff format <flagged_paths...>`
**Docs Link:** [docs/REASON_CATALOG.md#post_agent_commit_format_rewrite_needed](#post_agent_commit_format_rewrite_needed)

### POST_AGENT_COMMIT_PRECOMMIT_FAILED
**Problem:** A pre-commit hook rejected the post-agent commit after AWF exhausted bounded repair. Deterministic normalizer/formatter hooks are retried once by AWF. Ruff diagnostics marked ``[*]`` are repaired with a bounded ``ruff check --fix`` pass on already-staged Python paths. Remaining semantic hooks such as ``awf-ruff-check``, ``awf-mypy``, security, large-file, merge-conflict, or unknown hooks are routed through one targeted agent repair pass when the original agent run was healthy.
**Likely Cause:** The agent's diff trips a semantic lint/type/security invariant, a Ruff auto-fix changed the code but the retry still failed, or a deterministic repair/targeted repair retry still left pre-commit failing.
**Operator Fix:** Inspect ``details.post_agent_commit`` and the ``workspace.post_agent_commit_repair`` event for ``repair_strategy``, ``failed_hooks``, ``repaired_paths``, ``normalizer_paths``, ``restaged_paths``, and ``retry_outcome``. Run ``uv run --python 3.12 --extra dev pre-commit run --all-files`` locally against the workspace branch, fix the reported issues, push, and remonitor.
**Related Command:** `uv run --python 3.12 --extra dev pre-commit run --all-files`
**Docs Link:** [docs/REASON_CATALOG.md#post_agent_commit_precommit_failed](#post_agent_commit_precommit_failed)

### POST_AGENT_FORMAT_REPAIR_FAILED
**Problem:** AWF detected a repairable post-agent pre-commit failure but the repair pipeline itself exited non-zero before the retry commit could run.
**Likely Cause:** The workspace image is missing `uv` or dev extras, the pinned Python version is unavailable, `ruff` crashed on flagged paths, or the post-repair `git add` failed. The corresponding ``workspace.post_agent_commit_repair`` event records ``retry_outcome="error"``.
**Operator Fix:** Inspect the workspace logs for the repair sub-step stderr, fix the toolchain or git state, and remonitor.
**Related Command:** `awf workspace logs <workspace_id>`
**Docs Link:** [docs/REASON_CATALOG.md#post_agent_format_repair_failed](#post_agent_format_repair_failed)

### POST_AGENT_GIT_ADD_FAILED
**Problem:** ``git add -A`` failed during post-agent salvage (e.g. exit 128 with ``fatal: not a git repository``).
**Likely Cause:** The agent damaged the worktree's git metadata or removed ``.git``; no commit could be attempted to capture work.
**Operator Fix:** Inspect the worktree, recover any salvageable files manually, and recreate the workspace.
**Related Command:** `awf workspace logs <workspace_id>`
**Docs Link:** [docs/REASON_CATALOG.md#post_agent_git_add_failed](#post_agent_git_add_failed)

### PROTECTED_SCOPE_DIFF_UNAVAILABLE
**Problem:** The PR monitor could not verify protected-scope changes against the remote PR branch before push.
**Likely Cause:** The PR branch diff baseline could not be fetched because of a GitHub/network failure, a missing remote ref, or delayed ref replication.
**Operator Fix:** Verify GitHub/network access and the PR branch ref, then remonitor the workspace once the remote branch is reachable.
**Related Command:** `awf workspace remonitor <workspace_id>`
**Docs Link:** [docs/REASON_CATALOG.md#protected_scope_diff_unavailable](#protected_scope_diff_unavailable)

### PROTECTED_SCOPE_PUSH_BLOCKED
**Problem:** The PR monitor refused to push because committed changes touched protected quality-gate paths outside the workspace owned paths.
**Likely Cause:** A CI-fix, sync-base, or comment-addressing push included protected quality-gate files that the workspace profile does not own.
**Operator Fix:** Inspect the blocked paths in the monitor event, remove or revert the out-of-scope quality-gate changes, then remonitor the workspace.
**Related Command:** `awf workspace logs <workspace_id>`
**Docs Link:** [docs/REASON_CATALOG.md#protected_scope_push_blocked](#protected_scope_push_blocked)

### PROVIDER_AUTH_FAILED
**Problem:** A workspace agent or PR monitor could not run because the selected LLM provider authentication failed.
**Likely Cause:** The provider token is expired, reused, missing inside the workspace runtime, or rejected by the provider CLI/API.
**Operator Fix:** Refresh the provider credentials, restart or rebuild the AWF service/runtime if credentials are mounted into containers, then remonitor or reschedule the workspace.
**Related Command:** `awf service doctor`
**Docs Link:** [docs/REASON_CATALOG.md#provider_auth_failed](#provider_auth_failed)

### PROVIDER_SETUP_AUTH_INVALID
**Problem:** AWF could not authenticate one provider during setup.
**Likely Cause:** The provider token, CLI login, or credential reference is missing, expired, or rejected.
**Operator Fix:** Refresh that provider's credential or login state, then rerun setup for the provider; other providers may remain usable.
**Related Command:** `awf setup --provider <provider>`
**Docs Link:** [docs/REASON_CATALOG.md#provider_setup_auth_invalid](#provider_setup_auth_invalid)

### PR_ADOPTION_INPUT_REQUIRED
**Problem:** A PR monitor adoption request omitted required input.
**Likely Cause:** The request did not include enough repository or PR information to identify the PR.
**Operator Fix:** Provide the repository and PR number, or use a complete GitHub PR URL.
**Related Command:** `awf workspace adopt-pr`
**Docs Link:** [docs/REASON_CATALOG.md#pr_adoption_input_required](#pr_adoption_input_required)

### PR_ADOPTION_METADATA_FETCH_GITHUB_ONLY
**Problem:** AWF could not adopt the PR because the default adoption metadata fetcher only supports GitHub.
**Likely Cause:** The repository is on a supported non-GitHub forge (e.g. BitBucket Cloud), but the default adoption metadata fetcher shells `gh pr view`, which is GitHub-only — so AWF fails fast instead of querying GitHub for the same owner/repo slug.
**Operator Fix:** No repository change is needed — the forge is supported. Inject a BitBucket-aware adoption metadata fetcher, or adopt a GitHub PR until the forge-neutral fetcher lands.
**Related Command:** `awf workspace adopt-pr`
**Docs Link:** [docs/REASON_CATALOG.md#pr_adoption_metadata_fetch_github_only](#pr_adoption_metadata_fetch_github_only)

### PR_ADOPTION_POLICY_CONFLICT
**Problem:** The requested PR cannot be adopted under the current workspace policy.
**Likely Cause:** The PR targets an unsupported branch, conflicts with requested metadata, or violates adoption policy.
**Operator Fix:** Review the PR target and adoption options, then retry with policy-compatible input.
**Related Command:** `awf workspace adopt-pr`
**Docs Link:** [docs/REASON_CATALOG.md#pr_adoption_policy_conflict](#pr_adoption_policy_conflict)

### PR_ALREADY_CLOSED
**Problem:** The PR selected for monitor adoption is already closed.
**Likely Cause:** The PR was closed before AWF could adopt monitoring.
**Operator Fix:** Reopen the PR or adopt an active replacement PR.
**Related Command:** `awf workspace adopt-pr`
**Docs Link:** [docs/REASON_CATALOG.md#pr_already_closed](#pr_already_closed)

### PR_ALREADY_MERGED
**Problem:** The PR selected for monitor adoption is already merged.
**Likely Cause:** There is no open PR monitor work left for AWF to own.
**Operator Fix:** No monitor adoption is needed; use workspace cleanup or status commands instead.
**Related Command:** `awf workspace adopt-pr`
**Docs Link:** [docs/REASON_CATALOG.md#pr_already_merged](#pr_already_merged)

### PR_CREATE_FORGE_NOT_SUPPORTED
**Problem:** AWF could not open a new pull request because new-PR creation only supports GitHub.
**Likely Cause:** The workspace is on a supported non-GitHub forge (e.g. BitBucket Cloud), but opening a new PR shells `gh pr create` and parses github.com PR URLs, so AWF fails fast at the push step instead of mis-routing to a same-slug GitHub repository.
**Operator Fix:** No repository change is needed — the forge is supported for monitoring an existing PR. Open the pull request manually first (AWF will then monitor it), use a GitHub repository, or remonitor once forge-neutral PR creation lands.
**Related Command:** `awf workspace logs <workspace_id>`
**Docs Link:** [docs/REASON_CATALOG.md#pr_create_forge_not_supported](#pr_create_forge_not_supported)

### PR_METADATA_FETCH_FAILED
**Problem:** AWF could not fetch GitHub metadata for the requested PR.
**Likely Cause:** GitHub auth, network access, rate limits, or repository permissions blocked the metadata query.
**Operator Fix:** Verify `gh auth status`, repository access, and network connectivity, then retry adoption.
**Related Command:** `gh pr view`
**Docs Link:** [docs/REASON_CATALOG.md#pr_metadata_fetch_failed](#pr_metadata_fetch_failed)

### PR_METADATA_INVALID
**Problem:** GitHub returned PR metadata that AWF could not use safely.
**Likely Cause:** Required PR fields were missing or had an unexpected shape.
**Operator Fix:** Inspect the PR metadata with `gh pr view --json` and retry after GitHub/API data is consistent.
**Related Command:** `gh pr view`
**Docs Link:** [docs/REASON_CATALOG.md#pr_metadata_invalid](#pr_metadata_invalid)

### PR_NOT_FOUND
**Problem:** The requested PR was not found.
**Likely Cause:** The PR number is wrong, the repository is wrong, or the token lacks access.
**Operator Fix:** Confirm the repository, PR number, and GitHub permissions.
**Related Command:** `gh pr view`
**Docs Link:** [docs/REASON_CATALOG.md#pr_not_found](#pr_not_found)

### RELEASE_SYNC_FORGE_NOT_SUPPORTED
**Problem:** AWF could not run release-PR sync because the release-PR sync path only supports GitHub.
**Likely Cause:** The workspace is on a supported non-GitHub forge (e.g. BitBucket Cloud), but release-PR sync shells `gh pr list` / `gh pr view` and parses github.com PR URLs, so AWF fails fast instead of mis-routing to a same-slug GitHub repository.
**Operator Fix:** No repository change is needed — the forge is supported. Run release-PR sync against a GitHub repository, or remonitor once a forge-neutral release sync lands.
**Related Command:** `awf workspace logs <workspace_id>`
**Docs Link:** [docs/REASON_CATALOG.md#release_sync_forge_not_supported](#release_sync_forge_not_supported)

### SERVICE_STATUS_COLLECTION_FAILED
**Problem:** AWF service status checks could not be collected.
**Likely Cause:** Service discovery or database connection failed.
**Operator Fix:** Fix the reported local configuration error and re-run doctor.
**Related Command:** `awf service doctor`
**Docs Link:** [docs/REASON_CATALOG.md#service_status_collection_failed](#service_status_collection_failed)

### SETUP_CLIENT_UNKNOWN
**Problem:** AWF setup was asked to configure an unsupported client integration.
**Likely Cause:** The client selector does not match a client known to this AWF build.
**Operator Fix:** Use a supported client name (claude or codex) with `awf setup --client <client>`, or omit `--client` to run the readiness pass.
**Related Command:** `awf setup --client <client>`
**Docs Link:** [docs/REASON_CATALOG.md#setup_client_unknown](#setup_client_unknown)

### SETUP_PLAIN_SECRETS_CLIENT_CONFLICT
**Problem:** AWF setup cannot combine --allow-plain-secrets with --client.
**Likely Cause:** The client dispatch never reaches the consent-persisting path, so accepting --allow-plain-secrets there would silently drop the operator's opt-in flag.
**Operator Fix:** Re-run setup with --client to register a client MCP integration, and record plain-file consent separately via the readiness/provider path (awf setup --allow-plain-secrets), which is the only path that persists it.
**Related Command:** `awf setup --client <client>`
**Docs Link:** [docs/REASON_CATALOG.md#setup_plain_secrets_client_conflict](#setup_plain_secrets_client_conflict)

### SETUP_PROVIDER_CLIENT_CONFLICT
**Problem:** AWF setup cannot combine --provider with --client.
**Likely Cause:** The setup command received mutually exclusive --provider and --client selectors in one invocation.
**Operator Fix:** Re-run setup with either --provider to evaluate providers or --client to register a client MCP integration, but not both.
**Related Command:** `awf setup --client <client>`
**Docs Link:** [docs/REASON_CATALOG.md#setup_provider_client_conflict](#setup_provider_client_conflict)

### SETUP_PROVIDER_UNKNOWN
**Problem:** AWF setup was asked to configure an unsupported provider.
**Likely Cause:** The provider selector does not match a provider known to this AWF build.
**Operator Fix:** Use a supported provider name from setup help, or omit `--provider` to evaluate all configured providers.
**Related Command:** `awf setup --help`
**Docs Link:** [docs/REASON_CATALOG.md#setup_provider_unknown](#setup_provider_unknown)

### SETUP_READINESS_FAILED
**Problem:** AWF setup found one or more machine readiness blockers.
**Likely Cause:** The local machine does not currently satisfy the prerequisites for first-run AWF Core.
**Operator Fix:** Fix the reported blockers such as Docker, Compose, Git, disk, port, or PATH issues, then rerun `awf setup --dry-run`.
**Related Command:** `awf setup --dry-run`
**Docs Link:** [docs/REASON_CATALOG.md#setup_readiness_failed](#setup_readiness_failed)

### SOURCE_CHECKOUT_ASSETS_STALE
**Problem:** AWF source checkout metadata is stale or no longer matches the current asset contract.
**Likely Cause:** A previously recorded source checkout was moved, changed, or verified against an older asset list.
**Operator Fix:** Re-run setup with the current checkout path so AWF can verify and record fresh source asset metadata.
**Related Command:** `awf setup --source-checkout .`
**Docs Link:** [docs/REASON_CATALOG.md#source_checkout_assets_stale](#source_checkout_assets_stale)

### SOURCE_CHECKOUT_INVALID
**Problem:** AWF could not verify the source checkout assets needed for first-run setup/start.
**Likely Cause:** The selected path is missing required AWF source markers or contains unreadable assets.
**Operator Fix:** Run from the AWF repository root or pass `--source-checkout` pointing at a complete checkout with pyproject, docs, migrations, and Docker assets.
**Related Command:** `awf setup --source-checkout .`
**Docs Link:** [docs/REASON_CATALOG.md#source_checkout_invalid](#source_checkout_invalid)

### SOURCE_RUNTIME_NOT_RELEASED
**Problem:** AWF rejected a workspace retry because the source workspace's compose runtime has not been released yet.
**Likely Cause:** The source workspace is in a terminal status but its `compose_project_name` is not NULL and no `workspace.terminal_runtime_released` event exists, meaning its Docker Compose stack may still be running and its host ports are still claimed.
**Operator Fix:** Wait for the source workspace's runtime to be released (automatic on destroy or manual runtime release), then retry. Use `awf workspace show <source_workspace_id>` to check for the `terminal_runtime_released` event.
**Related Command:** `awf workspace list`
**Docs Link:** [docs/REASON_CATALOG.md#source_runtime_not_released](#source_runtime_not_released)

### START_COMPOSE_ASSETS_MISSING
**Problem:** AWF start could not locate the Compose or runtime assets needed to start local Core.
**Likely Cause:** The selected package/source asset lane does not contain required bootstrap files.
**Operator Fix:** Use a valid package install or pass a verified `--source-checkout` path that contains AWF Docker and Compose assets.
**Related Command:** `awf start --source-checkout .`
**Docs Link:** [docs/REASON_CATALOG.md#start_compose_assets_missing](#start_compose_assets_missing)

### START_HEALTH_TIMEOUT
**Problem:** AWF start timed out waiting for local Core health checks.
**Likely Cause:** One or more local Core services did not become healthy before the start timeout.
**Operator Fix:** Inspect API, worker, and Postgres logs, fix the failing service, then rerun start with an appropriate timeout.
**Related Command:** `awf service status`
**Docs Link:** [docs/REASON_CATALOG.md#start_health_timeout](#start_health_timeout)

### START_MIGRATION_FAILED
**Problem:** AWF start could not complete local Core database migrations.
**Likely Cause:** The local control-plane database rejected or failed a required schema migration.
**Operator Fix:** Inspect migration and Postgres logs, fix the database state, then retry `awf start` or use expert service commands for recovery.
**Related Command:** `awf service logs --service api`
**Docs Link:** [docs/REASON_CATALOG.md#start_migration_failed](#start_migration_failed)

### START_PORT_CONFLICT
**Problem:** AWF start found a local port conflict.
**Likely Cause:** A local service is already bound to a port needed by AWF Core.
**Operator Fix:** Stop the process using the reported port or configure a different AWF API/console port before retrying start.
**Related Command:** `awf start`
**Docs Link:** [docs/REASON_CATALOG.md#start_port_conflict](#start_port_conflict)

### STRANDED_WORKSPACES_PRESENT
**Problem:** Stale or exited AWF workspace containers need operator review.
**Likely Cause:** Workspaces failed to tear down cleanly after task completion.
**Operator Fix:** Inspect the listed workspaces before running cleanup or recovery.
**Related Command:** `awf workspace list`
**Docs Link:** [docs/REASON_CATALOG.md#stranded_workspaces_present](#stranded_workspaces_present)

### WORKER_CONTAINER_EXITED
**Problem:** AWF worker container has exited.
**Likely Cause:** The worker process crashed due to configuration or resource limits.
**Operator Fix:** Inspect worker logs with awf service logs --service worker.
**Related Command:** `awf service logs --service worker`
**Docs Link:** [docs/REASON_CATALOG.md#worker_container_exited](#worker_container_exited)

### WORKER_CONTAINER_MISSING
**Problem:** AWF worker container was not found in the local Compose project.
**Likely Cause:** The AWF service has not been bootstrapped on this machine.
**Operator Fix:** Run awf service bootstrap to start the worker.
**Related Command:** `awf service bootstrap`
**Docs Link:** [docs/REASON_CATALOG.md#worker_container_missing](#worker_container_missing)

### WORKER_CONTAINER_NOT_RUNNING
**Problem:** AWF worker container is present but is not running.
**Likely Cause:** The worker container was stopped manually or failed to start.
**Operator Fix:** Run awf service bootstrap or inspect worker logs.
**Related Command:** `awf service bootstrap`
**Docs Link:** [docs/REASON_CATALOG.md#worker_container_not_running](#worker_container_not_running)

### WORKER_STATUS_UNAVAILABLE
**Problem:** AWF worker container status could not be inspected.
**Likely Cause:** Docker is unresponsive or the local compose state is corrupted.
**Operator Fix:** Verify Docker is running and the local service Compose file exists.
**Related Command:** `docker compose ps`
**Docs Link:** [docs/REASON_CATALOG.md#worker_status_unavailable](#worker_status_unavailable)

### WORKER_STATUS_UNPARSEABLE
**Problem:** AWF worker container status output could not be parsed.
**Likely Cause:** Docker compose returned unexpected output format.
**Operator Fix:** Upgrade Docker Compose or inspect `docker compose ps worker --format json` manually.
**Related Command:** `docker compose ps --format json`
**Docs Link:** [docs/REASON_CATALOG.md#worker_status_unparseable](#worker_status_unparseable)

### WORKER_UNHEALTHY
**Problem:** AWF worker container is running but Docker reports it unhealthy.
**Likely Cause:** The worker background tasks are stalled or failing.
**Operator Fix:** Inspect worker logs with awf service logs --service worker.
**Related Command:** `awf service logs --service worker`
**Docs Link:** [docs/REASON_CATALOG.md#worker_unhealthy](#worker_unhealthy)

### WORKSPACE_CREATE_RATE_LIMITED
**Problem:** AWF rejected a workspace creation request because the request-admission rate limit was exhausted.
**Likely Cause:** Too many `POST /v1/workspaces` requests arrived for the same bearer-token or client-host identity within one admission window.
**Operator Fix:** Wait for the response's `Retry-After` delay, reduce workspace creation concurrency, or replay the original request with the same idempotency key and body when recovering from a lost response.
**Related Command:** `awf workspace list`
**Docs Link:** [docs/REASON_CATALOG.md#workspace_create_rate_limited](#workspace_create_rate_limited)
