# AWF Reason and Error Code Catalog

This catalog documents common API/CLI/MCP failures, likely causes, and operator fixes.

### API_UNREACHABLE
**Problem:** AWF API is not reachable.
**Likely Cause:** The local AWF service container is not running or port 8000 is blocked.
**Operator Fix:** Run awf service bootstrap or inspect API logs.
**Related Command:** `awf service logs`
**Docs Link:** [docs/REASON_CATALOG.md#api_unreachable](#api_unreachable)

### CLAUDE_AUTH_MISSING
**Problem:** No Claude Code auth signal was visible.
**Likely Cause:** Missing Claude API credentials.
**Operator Fix:** Mount ~/.claude or set ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, or CLAUDE_CODE_OAUTH_TOKEN.
**Related Command:** `awf service doctor`
**Docs Link:** [docs/REASON_CATALOG.md#claude_auth_missing](#claude_auth_missing)

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

### INSUFFICIENT_DISK
**Problem:** Free disk is below the configured AWF threshold.
**Likely Cause:** Too many stopped containers, volumes, or large workspaces.
**Operator Fix:** Free disk before creating new workspaces or intentionally lower AWF_MIN_FREE_DISK_BYTES.
**Related Command:** `docker system prune`
**Docs Link:** [docs/REASON_CATALOG.md#insufficient_disk](#insufficient_disk)

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

### PR_ADOPTION_INPUT_REQUIRED
**Problem:** A PR monitor adoption request omitted required input.
**Likely Cause:** The request did not include enough repository or PR information to identify the PR.
**Operator Fix:** Provide the repository and PR number, or use a complete GitHub PR URL.
**Related Command:** `awf workspace adopt-pr`
**Docs Link:** [docs/REASON_CATALOG.md#pr_adoption_input_required](#pr_adoption_input_required)

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

### SERVICE_STATUS_COLLECTION_FAILED
**Problem:** AWF service status checks could not be collected.
**Likely Cause:** Service discovery or database connection failed.
**Operator Fix:** Fix the reported local configuration error and re-run doctor.
**Related Command:** `awf service doctor`
**Docs Link:** [docs/REASON_CATALOG.md#service_status_collection_failed](#service_status_collection_failed)

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
