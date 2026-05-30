# AWF Plan Artifact Nonblocking Overlap Plan

## Summary

AWF currently persists `docs/awf-plans/**` in workspace `owned_paths` as an
internal planning/conformance artifact scope. Live merge-queue state showed
unrelated PRs being serialized only because they all share this internal AWF
plan-artifact glob. That contradicts the PRD model: owned paths are advisory
coordination hints, and AWF-generated plan artifacts must not create ordinary
inter-workspace merge dependencies.

## Scope

- Add a shared owned-path helper that identifies AWF internal plan artifacts:
  `docs/awf-plans`, any path under `docs/awf-plans/`, and glob patterns such as
  `docs/awf-plans/**`.
- Filter those internal plan-artifact paths only where AWF infers
  inter-workspace dependency:
  - merge-queue blocker detection;
  - active owned-path overlap warnings during workspace creation/retry;
  - lock overlap risks;
  - overlap graph path matches and edges.
- Preserve raw `owned_paths` storage, API display, task policy display, and
  planning/conformance scope enforcement.
- Keep staleness behavior intact: plan-artifact target changes remain advisory
  and non-blocking.

## Non-Goals

- Do not ignore real docs paths such as `docs/**`, `docs/runbooks/**`, or
  implementation plans under `plans/**`.
- Do not change scheduler admission, exclusive resource locks, validation, or
  PR monitor merge policy beyond removing this false dependency source.
- Do not migrate existing database rows; recomputation should be enough after
  deploying/restarting AWF.

## Tests

- Merge queue:
  - candidates sharing only `docs/awf-plans/**` do not block each other;
  - a real shared path still blocks in age order;
  - distinct real paths plus shared `docs/awf-plans/**` do not block.
- Overlap warnings and graph:
  - plan-artifact-only overlap does not create workspace warnings or lock risks;
  - plan-artifact-only overlap does not create overlap graph edges;
  - real overlap remains visible even when plan artifacts are also present.
- Staleness:
  - existing advisory plan-artifact staleness coverage remains green.

## Validation

- Run targeted unit tests for merge queue, workspace overlap warning policy,
  locks, overlap graph, and staleness.
- Run `ruff` and `mypy` on touched Python files.
