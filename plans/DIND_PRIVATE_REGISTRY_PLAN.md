# DinD Private-Registry Docker Auth Recipe — Plan

## Problem statement and scope

Projects running in an AWF Docker-in-Docker workspace (`docker.mode: dind`) talk to
the managed DinD daemon at `tcp://docker:2375`, which has **no registry
credentials**. Private `docker pull` / Gradle pulls therefore fail with
`unauthorized` / `denied`. We need a documented, copy-pasteable recipe that wires
registry auth into a DinD workspace using **existing AWF mechanisms only** (no core
code change).

Scope (PR #529, `docs(#527)`):

- `docs/recipes/dind-private-registry-auth.md` — the recipe.
- `docs/recipes/examples/dind-private-registry/.awf/workspace.yml` — a canonical,
  parseable sample profile referenced by the recipe.
- `tests/unit/node/test_dind_private_registry_recipe.py` — keeps the sample honest.
- `docs/README.md`, `docs/PROJECT_ONBOARDING.md` — discoverability links.

Out of scope: any change to `src/awf` core (secret-lease resolution, compose
rendering, and DinD `DOCKER_HOST` injection already exist).

## Assumptions

- `provider: local-file` (alias `host-file`) mounts an existing host file read-only
  at an arbitrary target; `local-auth` only knows fixed refs (`gh`, `gcloud`,
  `gitconfig`, `ssh`) and has no `docker` ref, so it cannot mount `config.json`.
- AWF auto-injects `DOCKER_HOST=tcp://docker:2375` for `docker.mode: dind` when the
  profile does not declare it.
- Secret-lease source guards already reject "too broad" home-rooted refs
  (`SECRET_LEASE_SOURCE_TOO_BROAD`), non-existent / non-file refs
  (`SECRET_LEASE_SOURCE_INVALID`), and writable mounts
  (`SECRET_LEASE_WRITABLE_UNSUPPORTED`).

## Explicit requirements checklist

1. Explain why private pulls fail under `docker.mode: dind`.
2. Provide a copy-pasteable `.awf/workspace.yml` that:
   - sets `DOCKER_CONFIG` to the **directory** `/run/awf/secrets/docker`;
   - declares a `local-file` mount lease with `mode: ro` whose `target` is
     `/run/awf/secrets/docker/config.json`.
3. Ship a parseable sample profile at the documented `examples/...` path.
4. Document `ref` restrictions (literal absolute path; rejected broad roots and
   their reason codes) and `required: true` vs `false` behavior.
5. Document security properties: never paste tokens, read-only mount, sanitized
   lease metadata only, local-mode (not a cloud broker).
6. Add a test that the sample profile parses as a `WorkspaceProfile` and resolves
   the lease into the exact read-only mount with `DOCKER_CONFIG` = mount-dir.

## Implementation steps

1. Write the recipe doc (problem, solution, profile, "how it fits together", `ref`
   restrictions table, required-vs-optional, security notes, verify steps).
2. Add the canonical sample profile under
   `docs/recipes/examples/dind-private-registry/.awf/workspace.yml`.
3. Add `tests/unit/node/test_dind_private_registry_recipe.py` asserting parse +
   lease resolution into `AuthMount(target=/run/awf/secrets/docker/config.json,
   mode=ro)`.
4. Link the recipe from `docs/README.md` and `docs/PROJECT_ONBOARDING.md`.

## Verification commands and pass criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_dind_private_registry_recipe.py -q`
  → both tests pass.
- Doc and sample profile agree on `DOCKER_CONFIG`, mount target, provider, and mode.

Full repo-wide validation (lint/type/coverage/CI) is owned by AWF + GitHub CI after
the agent phase, per the workspace contract.
