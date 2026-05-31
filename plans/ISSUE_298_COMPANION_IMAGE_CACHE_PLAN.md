# Issue #298 — cache companion image builds across workspaces

## Problem

Each workspace clones the companion repo and builds its Dockerfile from scratch.
With the documented target of 8+ parallel agents, a dispatch wave for the same
companion runs N cold builds (~3-5 min each), inflating dispatch latency and the
`compose_up_timeout_seconds` budget.

## Root cause (corrected from the issue's framing)

Companions build on the **shared host Docker daemon**, not per-workspace DinD:
`ComposeManager._compose` inherits `os.environ` with no `DOCKER_HOST` override,
the control-plane worker mounts `/var/run/docker.sock`, and companions are
ordinary `build:` services in the same outer stack as the DinD sidecar. Because
the build daemon is shared, a locally-built tag is reusable by every workspace —
**no registry is required** (the issue's Option 1 is overkill for the single-node
local Core). The real gaps: concurrent dispatch waves race a cold cache, nothing
pre-builds, companions always render `build:` (never `image:`), and built images
are never pruned.

## Approach (option A — local-tag pre-build, with image GC)

1. At provision time, resolve each companion's HEAD commit sha, derive
   `awf-companion-<name>:<short_sha>`, skip the build if the tag already exists,
   else build once with an in-process per-tag lock to dedupe concurrent waves.
2. Render `image:` for companions that have a pre-built tag; fall back to
   `build:` when the builder is disabled or a build fails.
3. Label cached images `awf.managed-companion=true`; prune unused ones older than
   a retention window from `awf service gc` (Docker protects in-use images).

## Changes

- `src/awf/node/companion_images.py` (new): `CompanionImageBuilder`
  (tag derivation, per-tag `asyncio.Lock` dedupe, skip-if-exists, build with
  labels, `None` on failure) + `companion_image_prune_command`.
- `src/awf/node/compose_manager.py`: `CompanionService.image` field + render uses
  it; `build_companion_image`, `companion_image_exists`, `prune_companion_images`
  methods; `_docker_capture` gains a call-time-resolved `capture_timeout_seconds`.
- `src/awf/node/companion_services.py`: `MaterializedCompanionService.commit_sha`;
  `companion_service_from_materialized(..., image=...)`.
- `src/awf/node/provisioner.py`: resolve companion `commit_sha` via
  `git.head_sha` during materialization.
- `src/awf/node/stack_launcher.py`: optional `companion_image_builder`;
  `_build_companion_services` applies pre-built tags with `build:` fallback.
- `src/awf/common/config.py` + `src/awf/service/config.py`:
  `companion_image_cache_enabled` (default true), `companion_image_retention_hours`
  (default 168).
- `src/awf/service/worker.py`: `_companion_image_builder_for` wires the builder
  when caching is enabled.
- `src/awf/service/gc.py`: optional `companion_image_prune` callback in
  `run_terminal_workspace_gc`, reported in the GC payload.
- `src/awf/cli/common.py` + `src/awf/cli/service_commands.py`: inject the
  label-based prune in the CLI gc path, gated by the enable flag.
- `docs/CONCEPTS.md`: companion image caching + GC behavior.

## Tests

New: `test_companion_images.py`, `test_stack_launcher_companion_images.py`,
`test_gc_companion_image_prune.py`, `tests/unit/cli/test_companion_image_prune.py`.
Extended: `test_compose_manager.py` (render `image:`/`build:`, docker methods),
`test_companion_services.py` (commit_sha + image plumbing), `test_worker.py`
(builder enabled/disabled), plus updates to existing launcher/worker/CLI wiring
tests for the new constructor kwarg and per-companion off-thread build.

## NOT in scope

- Local `registry:2` / push-pull (Option 1) and buildx remote cache (Option 3) —
  unnecessary on the shared host daemon; revisit for multi-node.
- Distributed (cross-process/node) build coordination — single-node Core uses an
  in-process lock.
- Persisting resolved companion tags in the DB — GC uses image labels + Docker's
  in-use protection.
- Changing the companion API request schema — the tag is derived internally
  (no `openapi.json` change).
