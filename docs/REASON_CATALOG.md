# AWF Reason and Error Code Catalog

This catalog documents common API/CLI/MCP failures, likely causes, and operator fixes.

### API_UNREACHABLE
**Problem:** AWF API is not reachable.
**Likely Cause:** A required resource or configuration is missing.
**Operator Fix:** Run awf service bootstrap or inspect API logs.
**Related Command:** `awf doctor`
**Docs Link:** [Troubleshooting](#)

### CLAUDE_AUTH_MISSING
**Problem:** No Claude Code auth signal was visible.
**Likely Cause:** A required resource or configuration is missing.
**Operator Fix:** Mount ~/.claude or set ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, or CLAUDE_CODE_OAUTH_TOKEN.
**Related Command:** `awf doctor`
**Docs Link:** [Troubleshooting](#)

### CODEX_AUTH_MISSING
**Problem:** No Codex auth signal was visible.
**Likely Cause:** A required resource or configuration is missing.
**Operator Fix:** Mount ~/.codex or set OPENAI_API_KEY, OPENAI_API_TOKEN, CODEX_API_KEY, or CODEX_AUTH_TOKEN.
**Related Command:** `awf doctor`
**Docs Link:** [Troubleshooting](#)

### DISK_USAGE_UNAVAILABLE
**Problem:** Free disk could not be inspected for the AWF work directory.
**Likely Cause:** A required resource or configuration is missing.
**Operator Fix:** Verify AWF_WORK_DIR is accessible and re-run doctor.
**Related Command:** `awf doctor`
**Docs Link:** [Troubleshooting](#)

### DOCKER_CLI_NOT_FOUND
**Problem:** Docker CLI is not installed or is not on PATH.
**Likely Cause:** A required resource or configuration is missing.
**Operator Fix:** Install Docker Desktop or make the docker CLI available to the AWF service environment.
**Related Command:** `awf doctor`
**Docs Link:** [Troubleshooting](#)

### DOCKER_DAEMON_UNREACHABLE
**Problem:** Docker is installed but the daemon is not reachable.
**Likely Cause:** A required resource or configuration is missing.
**Operator Fix:** Start Docker Desktop or verify AWF_DOCKER_HOST.
**Related Command:** `awf doctor`
**Docs Link:** [Troubleshooting](#)

### DOCKER_SOCKET_UNREACHABLE
**Problem:** Docker socket is not reachable.
**Likely Cause:** A required resource or configuration is missing.
**Operator Fix:** Start Docker Desktop or verify AWF_DOCKER_HOST.
**Related Command:** `awf doctor`
**Docs Link:** [Troubleshooting](#)

### GEMINI_AUTH_MISSING
**Problem:** No Gemini auth signal was visible.
**Likely Cause:** A required resource or configuration is missing.
**Operator Fix:** Mount ~/.gemini or set GEMINI_API_KEY, GOOGLE_API_KEY, or GOOGLE_APPLICATION_CREDENTIALS.
**Related Command:** `awf doctor`
**Docs Link:** [Troubleshooting](#)

### GITHUB_AUTH_UNUSABLE
**Problem:** GitHub CLI auth is not usable for local service PR operations.
**Likely Cause:** A required resource or configuration is missing.
**Operator Fix:** Run gh auth status locally and refresh AWF_GITHUB_TOKEN if needed.
**Related Command:** `awf doctor`
**Docs Link:** [Troubleshooting](#)

### GITHUB_CLI_NOT_FOUND
**Problem:** GitHub token is present, but the gh CLI is not installed.
**Likely Cause:** A required resource or configuration is missing.
**Operator Fix:** Install gh in the service image or rebuild the local service image.
**Related Command:** `awf doctor`
**Docs Link:** [Troubleshooting](#)

### GITHUB_TOKEN_ENV_MISSING
**Problem:** No service-visible GitHub token was found.
**Likely Cause:** A required resource or configuration is missing.
**Operator Fix:** Set AWF_GITHUB_TOKEN from `gh auth token` before starting the service.
**Related Command:** `awf doctor`
**Docs Link:** [Troubleshooting](#)

### INSUFFICIENT_DISK
**Problem:** Free disk is below the configured AWF threshold.
**Likely Cause:** A required resource or configuration is missing.
**Operator Fix:** Free disk before creating new workspaces or intentionally lower AWF_MIN_FREE_DISK_BYTES.
**Related Command:** `awf doctor`
**Docs Link:** [Troubleshooting](#)

### LOCAL_CONFIG_INVALID
**Problem:** Local AWF configuration has issues that block reliable service use.
**Likely Cause:** A required resource or configuration is missing.
**Operator Fix:** Fix the listed environment or path settings and re-run doctor.
**Related Command:** `awf doctor`
**Docs Link:** [Troubleshooting](#)

### NETWORK_POSTURE_OPEN_ACTIVE
**Problem:** One or more active workspaces have unrestricted internet access.
**Likely Cause:** A required resource or configuration is missing.
**Operator Fix:** Confirm the open workspaces are trusted local work or recreate them with restricted/offline posture.
**Related Command:** `awf doctor`
**Docs Link:** [Troubleshooting](#)

### NETWORK_POSTURE_UNAVAILABLE
**Problem:** Workspace network posture could not be inspected.
**Likely Cause:** A required resource or configuration is missing.
**Operator Fix:** Restore control-plane database access and re-run doctor.
**Related Command:** `awf doctor`
**Docs Link:** [Troubleshooting](#)

### OPENCODE_OLLAMA_AUTH_MISSING
**Problem:** No OpenCode/Ollama auth signal was visible.
**Likely Cause:** A required resource or configuration is missing.
**Operator Fix:** Mount ~/.config/opencode, mount ~/.ollama auth files, or set OLLAMA_API_KEY.
**Related Command:** `awf doctor`
**Docs Link:** [Troubleshooting](#)

### ORPHAN_RESOURCES_PRESENT
**Problem:** Orphan AWF Docker resources were detected.
**Likely Cause:** A required resource or configuration is missing.
**Operator Fix:** Review the listed resources before running cleanup.
**Related Command:** `awf doctor`
**Docs Link:** [Troubleshooting](#)

### PORT_CLOSED
**Problem:** Required local port is not accepting connections.
**Likely Cause:** A required resource or configuration is missing.
**Operator Fix:** Start the AWF local service or free the configured port.
**Related Command:** `awf doctor`
**Docs Link:** [Troubleshooting](#)

### PORT_CONFIG_INVALID
**Problem:** Required local port could not be derived from configuration.
**Likely Cause:** A required resource or configuration is missing.
**Operator Fix:** Fix the local AWF URL configuration and re-run doctor.
**Related Command:** `awf doctor`
**Docs Link:** [Troubleshooting](#)

### SERVICE_STATUS_COLLECTION_FAILED
**Problem:** AWF service status checks could not be collected.
**Likely Cause:** A required resource or configuration is missing.
**Operator Fix:** Fix the reported local configuration error and re-run doctor.
**Related Command:** `awf doctor`
**Docs Link:** [Troubleshooting](#)

### STRANDED_WORKSPACES_PRESENT
**Problem:** Stale or exited AWF workspace containers need operator review.
**Likely Cause:** A required resource or configuration is missing.
**Operator Fix:** Inspect the listed workspaces before running cleanup or recovery.
**Related Command:** `awf doctor`
**Docs Link:** [Troubleshooting](#)

### WORKER_CONTAINER_EXITED
**Problem:** AWF worker container has exited.
**Likely Cause:** A required resource or configuration is missing.
**Operator Fix:** Inspect worker logs with awf service logs --service worker.
**Related Command:** `awf doctor`
**Docs Link:** [Troubleshooting](#)

### WORKER_CONTAINER_MISSING
**Problem:** AWF worker container was not found in the local Compose project.
**Likely Cause:** A required resource or configuration is missing.
**Operator Fix:** Run awf service bootstrap to start the worker.
**Related Command:** `awf doctor`
**Docs Link:** [Troubleshooting](#)

### WORKER_CONTAINER_NOT_RUNNING
**Problem:** AWF worker container is present but is not running.
**Likely Cause:** A required resource or configuration is missing.
**Operator Fix:** Run awf service bootstrap or inspect worker logs.
**Related Command:** `awf doctor`
**Docs Link:** [Troubleshooting](#)

### WORKER_STATUS_UNAVAILABLE
**Problem:** AWF worker container status could not be inspected.
**Likely Cause:** A required resource or configuration is missing.
**Operator Fix:** Verify Docker is running and the local service Compose file exists.
**Related Command:** `awf doctor`
**Docs Link:** [Troubleshooting](#)

### WORKER_STATUS_UNPARSEABLE
**Problem:** AWF worker container status output could not be parsed.
**Likely Cause:** A required resource or configuration is missing.
**Operator Fix:** Upgrade Docker Compose or inspect `docker compose ps worker --format json` manually.
**Related Command:** `awf doctor`
**Docs Link:** [Troubleshooting](#)

### WORKER_UNHEALTHY
**Problem:** AWF worker container is running but Docker reports it unhealthy.
**Likely Cause:** A required resource or configuration is missing.
**Operator Fix:** Inspect worker logs with awf service logs --service worker.
**Related Command:** `awf doctor`
**Docs Link:** [Troubleshooting](#)
