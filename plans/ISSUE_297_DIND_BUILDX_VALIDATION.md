# Issue #297 — buildx plugin for DinD workspaces — Validation

Validation of the implementation against `ISSUE_297_DIND_BUILDX_PLAN.md`.

## Plan adherence

| Plan item | Status | Notes |
|-----------|--------|-------|
| buildx in agent-runtime image | Done | `ARG DOCKER_BUILDX_PLUGIN_VERSION=0.34.1-1~debian.12~bookworm`, `docker-buildx-plugin=${...}` in Stage 2 apt install, `docker buildx version` verification. |
| buildx in control-plane image | Done | Same pinned ARG + `docker-buildx-plugin` in apt install. |
| Profile-configurable `dind_image` | Done | `ProfileDocker.dind_image` (default `docker:27-dind`), plumbed via `stack_launcher.py` into `WorkspaceComposeSpec`. |
| Regenerate OpenAPI | Done | `ProfileDocker.dind_image` present; `generate_openapi.py --check` clean. |
| Docs | Done | CONTRIBUTING "Build the Agent Runtime Image" notes buildx. |

## Version pin verification

Queried the live Docker apt repo
(`download.docker.com/linux/debian/dists/bookworm/stable/binary-amd64`):

- `docker-ce-cli=5:29.4.1-1~debian.12~bookworm` — exists (matches existing pin).
- `docker-compose-plugin=5.1.3-1~debian.12~bookworm` — exists (matches existing pin).
- `docker-buildx-plugin=0.34.1-1~debian.12~bookworm` — latest stable available;
  well above the `>= 0.17` minimum Compose requires. Not fabricated.

## Tests added

- `tests/unit/test_agent_runtime_dockerfile.py` — pinned buildx ARG/package +
  `docker buildx version`; CONTRIBUTING section mentions buildx.
- `tests/unit/test_control_plane_dockerfile.py` (new) — docker CLI + compose +
  pinned buildx packages.
- `tests/unit/profiles/test_profile_docker_config.py` (new) — `dind_image`
  default, override, empty rejection.
- `tests/unit/node/test_stack_launcher.py` — default + custom `dind_image` flow.
- `tests/unit/node/test_compose_manager.py` — custom `dind_image` render +
  `ensure_project_up(wait=False)` flag omission.
- `tests/unit/profiles/test_profile_models_helpers.py` (new) — health-check
  command/target getters, URL userinfo redaction, method/scheme normalizers.
- `tests/unit/node/test_stack_launcher_edge_cases.py` (new) —
  `DOCKER_UNAVAILABLE` with no required services / empty detail; parsed
  companion compose-up timeout branch.
- `tests/unit/node/test_service_auth_mounts.py` — read-only mount chown skip.

## Validation gate results

- `ruff check .` — All checks passed.
- `ruff format --check .` — all files formatted.
- `mypy` — Success, no issues in 288 source files.
- `python scripts/generate_openapi.py --check` — spec matches the app.
- `pytest -n 20 --dist=loadscope --cov=awf` — 8702 passed; coverage 99.01%
  (`Required test coverage of 99.0% reached`).

## Gaps / notes

- The buildx warning is client-side; the fix targets the agent-runtime and
  control-plane images (the actual Docker clients) rather than the DinD daemon
  image the issue named. The DinD image remains profile-configurable but its
  default is unchanged.
- A live "no buildx warning" assertion would require an integration test that
  builds the real agent-runtime image (not the lightweight `docker:27-cli`
  stand-in used by `test_compose_manager_dind_dogfood.py`); deferred as it needs
  a Docker daemon and a full image build. Dockerfile-content tests + the
  bootstrap/CI image builds enforce the package presence instead.
