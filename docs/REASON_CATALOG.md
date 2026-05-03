# AWF Reason and Error Code Catalog

This catalog documents common API/CLI/MCP failures, likely causes, and operator fixes.

### API_UNREACHABLE
**Problem:** AWF API is not reachable.
**Likely Cause:** The local AWF service container is not running or port 8000 is blocked.
**Operator Fix:** Run awf service bootstrap or inspect API logs.
**Related Command:** `awf service logs`
**Docs Link:** [docs/REASON_CATALOG.md#api_unreachable](docs/REASON_CATALOG.md#api_unreachable)

### CLAUDE_AUTH_MISSING
**Problem:** No Claude Code auth signal was visible.
**Likely Cause:** Missing Claude API credentials.
**Operator Fix:** Mount ~/.claude or set ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, or CLAUDE_CODE_OAUTH_TOKEN.
**Related Command:** `awf doctor`
**Docs Link:** [docs/REASON_CATALOG.md#claude_auth_missing](docs/REASON_CATALOG.md#claude_auth_missing)

### CODEX_AUTH_MISSING
**Problem:** No Codex auth signal was visible.
**Likely Cause:** Missing Codex API credentials.
**Operator Fix:** Mount ~/.codex or set OPENAI_API_KEY, OPENAI_API_TOKEN, CODEX_API_KEY, or CODEX_AUTH_TOKEN.
**Related Command:** `awf doctor`
**Docs Link:** [docs/REASON_CATALOG.md#codex_auth_missing](docs/REASON_CATALOG.md#codex_auth_missing)

### DISK_USAGE_UNAVAILABLE
**Problem:** Free disk could not be inspected for the AWF work directory.
**Likely Cause:** Permission denied or path does not exist.
**Operator Fix:** Verify AWF_WORK_DIR is accessible and re-run doctor.
**Related Command:** `awf doctor`
**Docs Link:** [docs/REASON_CATALOG.md#disk_usage_unavailable](docs/REASON_CATALOG.md#disk_usage_unavailable)

### DOCKER_CLI_NOT_FOUND
**Problem:** Docker CLI is not installed or is not on PATH.
**Likely Cause:** The docker CLI is missing from the host environment or not accessible to the AWF process.
**Operator Fix:** Install Docker Desktop or make the docker CLI available to the AWF service environment.
**Related Command:** `awf doctor`
**Docs Link:** [https://docs.docker.com/get-docker/](https://docs.docker.com/get-docker/)

### DOCKER_DAEMON_UNREACHABLE
**Problem:** Docker is installed but the daemon is not reachable.
**Likely Cause:** The Docker daemon is stopped, crashing, or blocking connections.
**Operator Fix:** Start Docker Desktop or verify AWF_DOCKER_HOST.
**Related Command:** `awf doctor`
**Docs Link:** [https://docs.docker.com/config/daemon/](https://docs.docker.com/config/daemon/)

### DOCKER_SOCKET_UNREACHABLE
**Problem:** Docker socket is not reachable.
**Likely Cause:** The Docker daemon is not running or the socket permissions are incorrect.
**Operator Fix:** Start Docker Desktop or verify AWF_DOCKER_HOST.
**Related Command:** `awf doctor`
**Docs Link:** [https://docs.docker.com/config/daemon/](https://docs.docker.com/config/daemon/)

### GEMINI_AUTH_MISSING
**Problem:** No Gemini auth signal was visible.
**Likely Cause:** Missing Gemini API credentials.
**Operator Fix:** Mount ~/.gemini or set GEMINI_API_KEY, GOOGLE_API_KEY, or GOOGLE_APPLICATION_CREDENTIALS.
**Related Command:** `awf doctor`
**Docs Link:** [docs/REASON_CATALOG.md#gemini_auth_missing](docs/REASON_CATALOG.md#gemini_auth_missing)

### GITHUB_AUTH_UNUSABLE
**Problem:** GitHub CLI auth is not usable for local service PR operations.
**Likely Cause:** The GitHub token is expired, invalid, or lacks required scopes.
**Operator Fix:** Run gh auth status locally and refresh AWF_GITHUB_TOKEN if needed.
**Related Command:** `gh auth status`
**Docs Link:** [docs/REASON_CATALOG.md#github_auth_unusable](docs/REASON_CATALOG.md#github_auth_unusable)

### GITHUB_CLI_NOT_FOUND
**Problem:** GitHub token is present, but the gh CLI is not installed.
**Likely Cause:** The gh CLI is missing from the container environment.
**Operator Fix:** Install gh in the service image or rebuild the local service image.
**Related Command:** `awf service bootstrap`
**Docs Link:** [docs/REASON_CATALOG.md#github_cli_not_found](docs/REASON_CATALOG.md#github_cli_not_found)

### GITHUB_TOKEN_ENV_MISSING
**Problem:** No service-visible GitHub token was found.
**Likely Cause:** GitHub CLI is not authenticated or token is not passed to the service.
**Operator Fix:** Set AWF_GITHUB_TOKEN from `gh auth token` before starting the service.
**Related Command:** `gh auth login`
**Docs Link:** [docs/REASON_CATALOG.md#github_token_env_missing](docs/REASON_CATALOG.md#github_token_env_missing)

### INSUFFICIENT_DISK
**Problem:** Free disk is below the configured AWF threshold.
**Likely Cause:** Too many stopped containers, volumes, or large workspaces.
**Operator Fix:** Free disk before creating new workspaces or intentionally lower AWF_MIN_FREE_DISK_BYTES.
**Related Command:** `docker system prune`
**Docs Link:** [docs/REASON_CATALOG.md#insufficient_disk](docs/REASON_CATALOG.md#insufficient_disk)

### LOCAL_CONFIG_INVALID
**Problem:** Local AWF configuration has issues that block reliable service use.
**Likely Cause:** Invalid values in .env or missing required paths.
**Operator Fix:** Fix the listed environment or path settings and re-run doctor.
**Related Command:** `awf doctor`
**Docs Link:** [docs/REASON_CATALOG.md#local_config_invalid](docs/REASON_CATALOG.md#local_config_invalid)

### NETWORK_POSTURE_OPEN_ACTIVE
**Problem:** One or more active workspaces have unrestricted internet access.
**Likely Cause:** Workspaces were started with --network=open.
**Operator Fix:** Confirm the open workspaces are trusted local work or recreate them with restricted/offline posture.
**Related Command:** `awf workspace list`
**Docs Link:** [docs/REASON_CATALOG.md#network_posture_open_active](docs/REASON_CATALOG.md#network_posture_open_active)

### NETWORK_POSTURE_UNAVAILABLE
**Problem:** Workspace network posture could not be inspected.
**Likely Cause:** Cannot query the local database to check workspace posture.
**Operator Fix:** Restore control-plane database access and re-run doctor.
**Related Command:** `awf doctor`
**Docs Link:** [docs/REASON_CATALOG.md#network_posture_unavailable](docs/REASON_CATALOG.md#network_posture_unavailable)

### OPENCODE_OLLAMA_AUTH_MISSING
**Problem:** No OpenCode/Ollama auth signal was visible.
**Likely Cause:** Missing OpenCode/Ollama credentials.
**Operator Fix:** Mount ~/.config/opencode, mount ~/.ollama auth files, or set OLLAMA_API_KEY.
**Related Command:** `awf doctor`
**Docs Link:** [docs/REASON_CATALOG.md#opencode_ollama_auth_missing](docs/REASON_CATALOG.md#opencode_ollama_auth_missing)

### ORPHAN_RESOURCES_PRESENT
**Problem:** Orphan AWF Docker resources were detected.
**Likely Cause:** Networks or volumes left behind by deleted workspaces.
**Operator Fix:** Review the listed resources before running cleanup.
**Related Command:** `awf service cleanup`
**Docs Link:** [docs/REASON_CATALOG.md#orphan_resources_present](docs/REASON_CATALOG.md#orphan_resources_present)

### PORT_CLOSED
**Problem:** Required local port is not accepting connections.
**Likely Cause:** The service is not running or the port is in use by another process.
**Operator Fix:** Start the AWF local service or free the configured port.
**Related Command:** `awf service bootstrap`
**Docs Link:** [docs/REASON_CATALOG.md#port_closed](docs/REASON_CATALOG.md#port_closed)

### PORT_CONFIG_INVALID
**Problem:** Required local port could not be derived from configuration.
**Likely Cause:** Invalid AWF_API_URL or AWF_FRONTEND_URL.
**Operator Fix:** Fix the local AWF URL configuration and re-run doctor.
**Related Command:** `awf doctor`
**Docs Link:** [docs/REASON_CATALOG.md#port_config_invalid](docs/REASON_CATALOG.md#port_config_invalid)

### SERVICE_STATUS_COLLECTION_FAILED
**Problem:** AWF service status checks could not be collected.
**Likely Cause:** Service discovery or database connection failed.
**Operator Fix:** Fix the reported local configuration error and re-run doctor.
**Related Command:** `awf doctor`
**Docs Link:** [docs/REASON_CATALOG.md#service_status_collection_failed](docs/REASON_CATALOG.md#service_status_collection_failed)

### STRANDED_WORKSPACES_PRESENT
**Problem:** Stale or exited AWF workspace containers need operator review.
**Likely Cause:** Workspaces failed to tear down cleanly after task completion.
**Operator Fix:** Inspect the listed workspaces before running cleanup or recovery.
**Related Command:** `awf workspace list`
**Docs Link:** [docs/REASON_CATALOG.md#stranded_workspaces_present](docs/REASON_CATALOG.md#stranded_workspaces_present)

### WORKER_CONTAINER_EXITED
**Problem:** AWF worker container has exited.
**Likely Cause:** The worker process crashed due to configuration or resource limits.
**Operator Fix:** Inspect worker logs with awf service logs --service worker.
**Related Command:** `awf service logs --service worker`
**Docs Link:** [docs/REASON_CATALOG.md#worker_container_exited](docs/REASON_CATALOG.md#worker_container_exited)

### WORKER_CONTAINER_MISSING
**Problem:** AWF worker container was not found in the local Compose project.
**Likely Cause:** The AWF service has not been bootstrapped on this machine.
**Operator Fix:** Run awf service bootstrap to start the worker.
**Related Command:** `awf service bootstrap`
**Docs Link:** [docs/REASON_CATALOG.md#worker_container_missing](docs/REASON_CATALOG.md#worker_container_missing)

### WORKER_CONTAINER_NOT_RUNNING
**Problem:** AWF worker container is present but is not running.
**Likely Cause:** The worker container was stopped manually or failed to start.
**Operator Fix:** Run awf service bootstrap or inspect worker logs.
**Related Command:** `awf service bootstrap`
**Docs Link:** [docs/REASON_CATALOG.md#worker_container_not_running](docs/REASON_CATALOG.md#worker_container_not_running)

### WORKER_STATUS_UNAVAILABLE
**Problem:** AWF worker container status could not be inspected.
**Likely Cause:** Docker is unresponsive or the local compose state is corrupted.
**Operator Fix:** Verify Docker is running and the local service Compose file exists.
**Related Command:** `docker compose ps`
**Docs Link:** [docs/REASON_CATALOG.md#worker_status_unavailable](docs/REASON_CATALOG.md#worker_status_unavailable)

### WORKER_STATUS_UNPARSEABLE
**Problem:** AWF worker container status output could not be parsed.
**Likely Cause:** Docker compose returned unexpected output format.
**Operator Fix:** Upgrade Docker Compose or inspect `docker compose ps worker --format json` manually.
**Related Command:** `docker compose ps --format json`
**Docs Link:** [docs/REASON_CATALOG.md#worker_status_unparseable](docs/REASON_CATALOG.md#worker_status_unparseable)

### WORKER_UNHEALTHY
**Problem:** AWF worker container is running but Docker reports it unhealthy.
**Likely Cause:** The worker background tasks are stalled or failing.
**Operator Fix:** Inspect worker logs with awf service logs --service worker.
**Related Command:** `awf service logs --service worker`
**Docs Link:** [docs/REASON_CATALOG.md#worker_unhealthy](docs/REASON_CATALOG.md#worker_unhealthy)
