# Issue #297 — buildx plugin for DinD workspaces

## Problem

Every `docker compose up` in a DinD workspace prints:

```
level=warning msg="Docker Compose requires buildx plugin to be installed"
```

Compose then falls back to the legacy builder. The warning is noise, pollutes
`COMPOSE_COMMAND_FAILED` stderr (false-cause for unrelated failures), and the
legacy builder forgoes BuildKit features (cache mounts, `--secret`).

## Root cause (corrected from the issue's framing)

The warning is **client-side** — emitted by whichever `docker compose` CLI runs
the build, not by the DinD daemon image the issue points at
(`compose_manager.py:169`). In AWF the Docker clients are:

- `docker/agent-runtime.Dockerfile` — the agent is the Docker client inside a
  DinD workspace (`DOCKER_HOST=tcp://docker:2375`). It installed `docker-ce-cli`
  + `docker-compose-plugin` but **not** `docker-buildx-plugin`.
- `docker/control-plane.Dockerfile` — runs `docker compose up` for the outer
  workspace stack (which builds managed companions). Same gap.

The `docker:27-dind` daemon already ships BuildKit, so the daemon side was fine.
Fixing only the DinD image would not have silenced the warning when the agent or
control plane runs compose.

## Approach (option C — comprehensive)

1. Add the official, pinned `docker-buildx-plugin` to both AWF-built client
   images, mirroring the existing `docker-compose-plugin` install (pinned ARG +
   apt from the already-configured Docker repo + version check). This silences
   the warning at its real source and unlocks BuildKit features.
2. Make the DinD daemon image profile-configurable
   (`ProfileDocker.dind_image`, default unchanged `docker:27-dind`) so the
   hardcoding the issue calls out is removed without forcing a new image or a
   registry/publish pipeline on the OSS core.

Pinned buildx version: `0.34.1-1~debian.12~bookworm` — the latest stable in the
Docker apt repo for bookworm (verified against the live package index), well
above the `>= 0.17` minimum Compose requires.

## Changes

- `docker/agent-runtime.Dockerfile`: `ARG DOCKER_BUILDX_PLUGIN_VERSION` +
  `docker-buildx-plugin=${...}` in Stage 2 apt install + `docker buildx version`.
- `docker/control-plane.Dockerfile`: same ARG + apt package.
- `src/awf/profiles/models.py`: `ProfileDocker.dind_image: str` (default
  `docker:27-dind`, `min_length=1`, `max_length=512`).
- `src/awf/node/stack_launcher.py`: pass `dind_image=profile.docker.dind_image`
  into `WorkspaceComposeSpec`.
- `openapi.json`: regenerated for the new `ProfileDocker.dind_image` property.
- `CONTRIBUTING.md`: note buildx is bundled in the agent-runtime image.

## Tests

- `tests/unit/test_agent_runtime_dockerfile.py`: pinned buildx ARG + apt package
  + `docker buildx version`; CONTRIBUTING section mentions buildx.
- `tests/unit/test_control_plane_dockerfile.py` (new): docker CLI + compose +
  pinned buildx packages.
- `tests/unit/profiles/test_profiles.py`: `dind_image` default, override,
  empty-string rejection.
- `tests/unit/node/test_stack_launcher.py`: default + custom `dind_image` flow
  from profile into the compose spec.
- `tests/unit/node/test_compose_manager.py`: custom `dind_image` reaches the
  rendered `docker` service.

## NOT in scope

- Custom `awf-dind` image / registry pipeline (issue Option A) — unnecessary once
  clients carry buildx; daemon already has BuildKit.
- DinD entrypoint apk-install of buildx (Option B) — needs startup egress, slower.
- Changing the default DinD image or pointing any profile at a non-default image.
- buildx-feature usage docs (`--secret`, cache mounts) — availability is the
  enabler; usage docs can follow separately.
