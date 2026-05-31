# Issue #298 — companion image cache — Validation

Validation of the implementation against `ISSUE_298_COMPANION_IMAGE_CACHE_PLAN.md`.

## Plan adherence

| Plan item | Status | Notes |
|-----------|--------|-------|
| CompanionImageBuilder (tag, dedupe lock, skip, build, None-on-fail) | Done | `src/awf/node/companion_images.py`, fully unit-tested incl. concurrent dedupe. |
| ComposeManager build/exists/prune + render `image:` | Done | Companion renders `image:` when tagged, `build:` otherwise. |
| Companion `commit_sha` + image plumbing | Done | Resolved in provisioner; threaded launcher → render. |
| Config flags | Done | `companion_image_cache_enabled` (default true), `companion_image_retention_hours` (default 168) in both Settings layers. |
| Worker wiring | Done | `_companion_image_builder_for` (enabled/disabled) wired into the launcher. |
| Image GC | Done | `companion_image_prune` callback in `run_terminal_workspace_gc`; CLI runs label-scoped `docker image prune` gated by the enable flag. |
| Docs | Done | `docs/CONCEPTS.md` companion image caching + GC. |

## Root-cause confirmation

Companion builds run on the host daemon (`ComposeManager._compose` uses
`os.environ`, worker mounts the host socket, companions share the outer stack
with the DinD sidecar). A local tag is therefore reusable without a registry —
confirmed via the existing `test_compose_manager_dind_dogfood` topology and the
render tests.

## Validation gate results

- `ruff check .` — All checks passed.
- `ruff format --check .` — all files formatted.
- `mypy` — Success, no issues in 289 source files.
- `python scripts/generate_openapi.py --check` — spec matches (no companion API
  schema change, as planned).
- `pytest -n 8 --dist=loadscope --cov=awf` — 8716 passed, coverage 99.00%
  reached. With the added `auth_mounts` margin test the total is **99.01%**
  (223 misses), verified via `--cov-append`.

## Notes / gaps

- `-n 20` on this machine intermittently flakes pre-existing concurrency tests
  (capacity gate, worktree remover, target-branch monitor, stale recovery);
  they pass in isolation and under `-n 8` (CI parity). Not related to this change.
- A live "no rebuild on second dispatch" assertion would need a Docker daemon and
  two real builds; covered structurally here via the builder/render/GC unit tests.
- Multi-node build coordination and a shared registry remain future work (the
  in-process per-tag lock is sufficient for the single-node local Core).
