# Plan: OSS Alpha Release Readiness

## Summary

Prepare Agent Workspace Fabric (AWF) for an Apache-2.0 alpha public release by
adding the missing legal files, removing internal generated work artifacts from
the tracked public tree, aligning public branding, adding a release checklist,
and making local service readiness reproducible from documented setup.

## Intended Changes

- Add a canonical Apache-2.0 `LICENSE` file and omit `NOTICE` until a concrete
  attribution requirement exists.
- Rename Python package metadata to `agent-workspace-fabric` while keeping the
  import package as `awf`.
- Update README, contributor docs, documentation index, and status language to
  use the public brand `Agent Workspace Fabric (AWF)`.
- Remove tracked generated artifacts under `TODO/`, `docs/awf-plans/` except
  `docs/awf-plans/README.md`, and `plans/` except
  `plans/PLAN_EXECUTION_PROTOCOL.md`.
- Add narrow ignore rules for future generated plan/backlog artifacts without
  hiding the canonical execution protocol.
- Add `RELEASING.md` with release validation, dependency license audit, and
  readiness checklist.
- Fix local doctor/service readiness so Docker Compose worker inspection loads
  `docker/compose/.env` automatically when present.

## Validation

- Add or update unit/docs tests for license presence, package metadata, public
  brand language, CI docs, release checklist content, and Compose env-file
  handling.
- Run focused tests first, then repo validation commands requested by the user
  where practical.

## Risks

- Removing tracked generated artifacts is intentionally large but should not
  affect runtime code.
- Historical docs may still mention Aira as product-origin context; public
  entrypoint docs should not brand the project as Aira AWF.
