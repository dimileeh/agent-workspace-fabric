# Plan: Generic Redis App Worker Sidecar Fixture

## Scope

Implement the narrow P1 realistic-profile slice from
`TODO/pre-gke-industrial-readiness.md` for a generic Redis-backed app plus
worker workspace profile. The goal is fixture and contract coverage, not a new
profile engine: prove AWF can resolve a repo-local profile that declares Redis,
an app service, and a worker sidecar; waits for health checks; runs setup and
validation commands through the agent container; and tears down containers,
networks, and named volumes cleanly with Docker Compose.

This should stay generic. The fixture must not encode Aira database,
pgvector, Alembic, service names, ports, or environment assumptions.

## Intended Files And Modules To Touch

Tests:

- `tests/unit/profiles/test_workspace_services_profile.py`
  - Add fixture-backed unit coverage for the new Redis/app/worker profile.
  - Assert profile resolution source, runtime environment, `ports`,
    service declarations, `depends_on`, health checks, setup command,
    validation command, and profile validation health checks.
  - Assert `profile_services(profile, base_path=...)` resolves worktree-local
    build contexts while preserving named Redis volume semantics.
- `tests/unit/node/test_compose_manager.py` or a new focused
  `tests/unit/node/test_redis_worker_profile_compose.py`
  - Render the fixture-derived `WorkspaceComposeSpec` without Docker.
  - Assert app, Redis, worker, and agent are on the workspace network.
  - Assert service health checks render as `CMD-SHELL`.
  - Assert `depends_on` uses `condition: service_healthy` for app/worker
    service dependencies and for the agent's healthchecked service waits.
  - Assert the Redis named volume is rendered with an AWF workspace-prefixed
    Docker volume name.
- `tests/integration/test_redis_worker_profile_compose.py` (new)
  - Docker smoke test modeled on the existing profile integration tests.
  - Use the same deterministic Docker-unavailable skip style:
    `AWF_SKIP_DOCKER_TESTS=1`, missing `docker`, failing `docker version`, or
    failing `docker compose version` all skip with a clear reason.
  - Launch the profile services plus a lightweight Python agent runtime with
    `ComposeManager.up(..., wait=True)`.
  - Run setup, health checks, and validation through `ValidationRunner`.
  - Always tear down in `finally`; after teardown, assert no
    `awf-{workspace_id}-*` containers or volumes remain.

Fixtures:

- `tests/fixtures/workspace_services/redis_worker_app/.awf/workspace.yml`
  - Repo-local profile named something generic like `redis-worker-app`.
  - `docker.mode: none`.
  - Runtime env such as:
    - `APP_BASE_URL=http://app:8080`
    - `REDIS_URL=redis://redis:6379/0`
    - `WORKER_STATUS_URL=http://app:8080/status`
  - Services:
    - `redis` using `redis:7-alpine`, `redis-cli ping` health check, and a
      named volume such as `redis_data:/data`.
    - `app` built from `.`, depending on `redis`, with an internal HTTP
      health check and no required host port publishing.
    - `worker` built from `.`, depending on `redis`, with a worker heartbeat
      health check and a command that processes Redis queue messages.
  - `phases.setup` and `phases.validate` commands that run from the agent
    container using only Python standard library code.
  - `validation.healthchecks` for Redis, app, and worker readiness from the
    agent container.
  - `ports` entries documenting service-name endpoints, not host-only access.
- `tests/fixtures/workspace_services/redis_worker_app/Dockerfile`
  - Small Python image that copies only fixture files needed by app and
    worker containers.
- `tests/fixtures/workspace_services/redis_worker_app/app.py`
  - Tiny HTTP app exposing `/healthz`, `/enqueue`, `/status`, and/or
    `/validate`.
- `tests/fixtures/workspace_services/redis_worker_app/worker.py`
  - Tiny worker loop that consumes Redis queue messages and records a
    deterministic result.
- `tests/fixtures/workspace_services/redis_worker_app/redis_client.py`
  - Minimal RESP socket helper so the fixture does not need third-party
    Python packages.
- `tests/fixtures/workspace_services/redis_worker_app/scripts/*.py`
  - Agent-side setup, health-check, and validation scripts using only Python
    standard library networking.

Docs:

- `README.md`
  - Add a compact generic Redis/app/worker profile example or a short
    reference to the new fixture under the existing workspace profile service
    examples.
  - Explain that the app, worker, and Redis communicate by Compose service
    name on the per-workspace network and that AWF owns Compose teardown.

Production modules, only if tests expose a real gap:

- `src/awf/profiles/compose.py`
  - Touch only for a narrow profile-service path-resolution or named-volume
    preservation bug.
- `src/awf/node/compose_manager.py`
  - Touch only for a renderer bug around service health waits, named volumes,
    or cleanup-safe Compose output.
- `src/awf/runtime/validation.py`
  - Not expected. Touch only if current health-check phase execution cannot
    run the fixture's declared health checks through the agent container.

No schema, migration, API, scheduler, PR monitor, console, or lockfile changes
are expected.

## Tests To Write First

1. Add a failing profile fixture contract test.
   - Reference `tests/fixtures/workspace_services/redis_worker_app` before the
     fixture exists.
   - Expected red: the fixture/profile is missing.
   - Then add the smallest `.awf/workspace.yml` and fixture files needed for
     the profile to resolve.

2. Add failing unit assertions for profile contents.
   - Assert exactly three services: `redis`, `app`, and `worker`.
   - Assert Redis image, Redis health check, named volume, and no Aira-specific
     environment.
   - Assert app and worker build from the fixture, depend on Redis, expose
     health checks, and use deterministic commands.
   - Assert setup, validation, and validation health-check commands are present
     with bounded timeouts.

3. Add failing profile-to-Compose adapter/rendering tests.
   - Build a `WorkspaceComposeSpec` from `profile_services(profile,
     base_path=fixture)`.
   - Assert rendered YAML has service health checks, dependency health waits,
     agent health waits, service networking, and workspace-prefixed named
     volumes.
   - If existing rendering already passes, keep production code unchanged.

4. Add the Docker integration smoke test.
   - Keep the skip guard deterministic and local to the test unless an
     existing shared helper is already available.
   - Start the stack with `agent_runtime_image="python:3.12-alpine"`.
   - Run setup first, then run profile health checks plus validation.
   - Expected success output should prove the worker processed a Redis-backed
     queue item, not just that Redis responds to PING.
   - Ensure cleanup assertions run after `manager.down(spec)`.

5. Implement the smallest green fixture and docs changes.
   - Prefer fixture/test/docs-only changes.
   - Make production code changes only for behavior the tests prove is missing.

## Validation Commands

Focused TDD loops:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/profiles/test_workspace_services_profile.py -q
uv run --python 3.12 --extra dev pytest tests/unit/node/test_compose_manager.py -q
uv run --python 3.12 --extra dev pytest tests/integration/test_redis_worker_profile_compose.py -q
```

If a new unit test file is added under `tests/unit/node`, run that file instead
of or in addition to `tests/unit/node/test_compose_manager.py`.

Required focused validation for this slice:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/profiles/test_workspace_services_profile.py tests/unit/node/test_compose_manager.py tests/integration/test_redis_worker_profile_compose.py -q
```

If production code under `src/awf` changes:

```bash
uv run --python 3.12 --extra dev ruff check src/awf tests
uv run --python 3.12 --extra dev mypy src/awf
```

If only fixtures, tests, and docs change:

```bash
uv run --python 3.12 --extra dev ruff check tests
```

If the implementation touches profile resolution, validation, or Compose
manager behavior more broadly, also run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/profiles tests/unit/node tests/unit/runtime -q
```

Coverage should not be lowered. If the change unexpectedly affects core
behavior broadly, run:

```bash
uv run --python 3.12 --extra dev pytest --cov=awf --cov-report=term-missing
```

## Risks

- Docker integration can be slow because it may pull `redis:7-alpine` and
  `python:3.12-alpine`. The test should skip only when Docker or Compose is
  unavailable; image pull or runtime failures should remain real failures.
- A worker readiness check can accidentally prove only Redis connectivity. The
  fixture should make the worker publish a heartbeat or process a deterministic
  queue item, and validation should assert that worker-produced result.
- Host port mappings can collide in parallel integration runs. The fixture
  should avoid required host ports and use Compose service DNS plus container
  ports for all agent-to-service communication.
- Named-volume cleanup must be asserted after `down -v`; otherwise the fixture
  could pass while leaving workspace-scoped state behind.
- If production code changes become necessary, the risk is mostly in shared
  Compose rendering semantics. Keep changes narrow and covered by existing
  compose manager tests plus the new fixture tests.

## Assumptions

- Profile-declared services in the outer AWF workspace Compose stack are the
  intended mechanism for this slice.
- Existing `ComposeManager.up(..., wait=True)` and Docker health checks are the
  right readiness gate before profile validation commands run.
- The agent container executes setup, health-check, and validation commands
  from `/workspace`, so fixture scripts can be mounted from the worktree.
- Service-name URLs such as `http://app:8080` and `redis:6379` are the canonical
  workspace-local access path.
- The task is satisfied by a generic fixture, tests, and documentation; no new
  built-in profile detector is required.

## Explicit Non-Goals

- Do not switch branches, push, rebase, force-push, or manually manage PR
  lifecycle.
- Do not add Aira-specific database, pgvector, Alembic, endpoint, or service
  assumptions.
- Do not redesign DinD or nested project Docker Compose behavior.
- Do not change public API response schemas, database schemas, migrations,
  scheduler policy, merge policy, PR monitor behavior, or console UI.
- Do not require Docker for unit tests.
- Do not hide Docker runtime failures behind retries or broad skips.
- Do not lower coverage thresholds, quality gates, lint settings, or mypy
  strictness.
- Do not update lockfiles unless an unexpected production dependency is
  explicitly required; the intended fixture uses only existing images and
  Python standard library code.
