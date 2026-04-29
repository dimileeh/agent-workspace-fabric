# Plan: P0 Workspace Services Test Slice

## Scope

Add focused coverage for a repo profile that represents a Dockerized app plus a
sidecar service. The slice should exercise the existing profile resolver,
profile-to-compose adapter, stack launcher, and compose renderer without
requiring Docker for unit tests. A real Docker integration smoke test is
included only if it can skip cleanly when Docker or Compose is unavailable.

The intended product behavior is that profile-declared workspace services run in
the same per-workspace Compose project and network as the agent. The agent and
peer services should reach sidecars by Compose service name and container port;
host port publishing is optional operator/debugging surface, not the primary
workspace reachability path.

## Intended Files And Modules To Touch

Test fixtures:

- `tests/fixtures/workspace_services/dockerized_app/.awf/workspace.yml` (new)
  - Repo-local profile fixture with:
    - one Dockerized app service using `build_context`;
    - one sidecar service using an image, e.g. Redis or another tiny service;
    - runtime environment such as `APP_BASE_URL=http://app:8080` and
      `CACHE_URL=redis://redis:6379/0`;
    - service environment, health checks, `depends_on`, and port mappings;
    - profile `ports` entries documenting service-name reachability.
- `tests/fixtures/workspace_services/dockerized_app/Dockerfile` and tiny app
  files (new, only if used by the integration test)
  - Keep the fixture minimal and deterministic.
  - Avoid app-specific dependencies beyond a small public base image.

Unit tests:

- `tests/unit/profiles/test_workspace_services_profile.py` (new, or extend
  `tests/unit/profiles/test_profiles.py` if local style strongly favors one
  file)
  - Load the fixture through `ProfileResolver`.
  - Assert resolved profile name/source, runtime env, `ports`, service schema,
    service env, health check, `depends_on`, and port tuple coercion.
  - Assert `profile_services(profile)` preserves the generated
    `ComposeService` fields that the renderer consumes.
- `tests/unit/runtime/test_workspace_services_compose.py` (new)
  - Render the fixture-derived profile through `ComposeStackLauncher` and/or
    `ComposeManager.render` using recording fakes, not Docker.
  - Assert the generated `WorkspaceComposeSpec` contains profile services,
    runtime env, `docker_mode`, and auth/git defaults.
  - Assert rendered YAML includes:
    - app and sidecar services on `awf_net`;
    - service `environment`;
    - `depends_on` with `service_healthy`;
    - health checks;
    - port publishing format;
    - agent env for `APP_BASE_URL` / `CACHE_URL`;
    - agent dependency only on healthchecked services.

Integration test:

- `tests/integration/test_workspace_services_sidecar_compose.py` (new)
  - Reuse the existing Docker availability pattern from
    `tests/integration/test_compose_manager_docker.py`.
  - Render a profile-derived Compose file and remove or avoid the `agent`
    service if the local `awf-agent-runtime:latest` image is not required for
    the reachability assertion.
  - Bring up app plus sidecar with `docker compose up -d --wait`.
  - Run a one-shot probe container on the same Compose project network proving
    service-name reachability, for example `http://app:8080` and/or
    `redis:6379`.
  - Always tear down with `down -v`; assert no matching containers/volumes
    remain when the test did run.

Production modules, only if the tests expose a current behavior gap:

- `src/awf/profiles/compose.py`
  - Smallest likely change: preserve or resolve profile service fields when
    converting `ProfileService` to `ComposeService`.
  - If relative `build_context` / `env_file` paths from repo-local profiles are
    proven wrong in rendered Compose files, add a narrow path-resolution helper
    rather than redesigning profiles.
- `src/awf/node/stack_launcher.py`
  - Touch only if relative profile service paths need access to
    `WorktreeLayout.worktree_path` at launch time.
- `src/awf/node/compose_manager.py`
  - Touch only for a renderer bug directly exposed by the new fixture tests.
- `src/awf/profiles/models.py`
  - Not expected. Touch only if existing tuple/list coercion or validation
    blocks the fixture without a good reason.

## Tests To Write First

1. Add the profile fixture contract test first, initially referencing the new
   fixture path before adding the fixture file.
   - Expected red: fixture/profile is missing.
   - Then add the minimal fixture and assert `ProfileResolver` loads it as
     `repo:.awf/workspace.yml`.

2. Add unit tests for profile data and adapter behavior.
   - Assert runtime env values are preserved.
   - Assert `profile.ports` contains service-name URLs, not host-only URLs.
   - Assert sidecar/app service env, health checks, `depends_on`, command,
     volumes if any, and port mappings survive `profile_services(profile)`.

3. Add no-Docker compose rendering tests.
   - Build a `WorkspaceComposeSpec` from the fixture profile through the same
     code path used by `ComposeStackLauncher`.
   - Assert the rendered YAML expresses the expected service network,
     health-check wait, port, and env semantics.
   - If a relative `build_context` renders relative to the generated Compose
     directory instead of the worktree, keep that test red and implement the
     smallest path-resolution fix.

4. Add the optional Docker integration smoke test.
   - First write the skip guard for Docker CLI, Docker daemon, and Compose
     plugin availability.
   - Keep the test small: launch only the fixture services needed to prove
     sidecar reachability.
   - Clean up in `finally` using the rendered project name.

5. Implement the smallest green change.
   - Prefer no production change if existing machinery already satisfies the
     new tests.
   - If production code changes are required, keep them limited to profile
     service conversion / launch-time path resolution / compose rendering.

## Validation Commands

Required task validation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/profiles tests/unit/runtime tests/integration -q
uv run --python 3.12 --extra dev ruff check src/awf tests
uv run --python 3.12 --extra dev mypy src/awf
```

Narrow TDD loops while developing:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/profiles/test_workspace_services_profile.py -q
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_workspace_services_compose.py -q
uv run --python 3.12 --extra dev pytest tests/integration/test_workspace_services_sidecar_compose.py -q
```

If production changes touch node compose/launcher modules, also run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/node/test_compose_manager.py tests/unit/node/test_stack_launcher.py -q
```

If the fixture or tests change broad profile identity behavior, also run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q
```

## Risks

- Relative `build_context` and `env_file` semantics for repo-local profile
  services may be under-specified today. If the new fixture reveals a bug, fix
  only worktree-relative service path handling and document it through tests.
- Integration tests that pull public images can be slow or flaky on offline
  machines. The test should skip only for Docker/Compose unavailability; image
  pull failures should remain normal test failures unless the existing
  integration-test convention says otherwise.
- The agent runtime image may not exist in developer environments. The
  integration test should avoid depending on that image when proving sidecar
  reachability.
- Compose `up --wait` behavior differs for one-shot probe containers, so the
  probe should run after persistent services are healthy rather than as a
  required long-running service.

## Assumptions

- Profile-declared services are the current supported mechanism for
  workspace-local sidecars in the outer AWF Compose project.
- The service DNS name plus container port is the canonical reachability path
  inside the workspace network.
- Host port mappings are allowed in profiles for operator/debug access but
  should not be required for agent-to-sidecar communication.
- Existing public API response shapes do not need to change for this test
  slice.
- No new database schema, migration, scheduler, admission, or PR monitor logic
  is required.

## Explicit Non-Goals

- Do not redesign DinD, nested project Compose, profile detection, or the whole
  profile schema.
- Do not add app-under-test lifecycle orchestration beyond what the existing
  profile service and Compose machinery can already express.
- Do not require Docker for unit tests.
- Do not lower coverage thresholds, workspace coverage requirements, quality
  gates, or validation policy.
- Do not modify API response compatibility or add public response fields.
- Do not switch branches, push, rebase, force-push, or manually manage the AWF
  PR lifecycle.
