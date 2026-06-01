# Issue #345 Phase 1 — Validation

Validates `plans/ISSUE_345_PHASE1_PLAN.md`. Strict TDD: failing tests written
first, then the smallest green implementation. Per the AWF workspace contract,
AWF/GitHub CI own the full 99%-coverage gate and broad validation after the agent
exits; the commands below are the focused checks plus the documented full-gate
lint/type commands recorded as evidence.

## Acceptance criteria → evidence

| Criterion | Evidence |
|---|---|
| Provider-neutral `ForgeClient` Protocol exists; `GitHubClient` satisfies it unchanged | `tests/unit/common/test_forge.py::test_github_client_satisfies_forge_client_protocol` (runtime_checkable isinstance) + `::test_forge_client_protocol_typing_accepts_github_client` + mypy clean (structural) |
| Consumers + 2 construction sites depend on the Protocol via the factory | Type hints swapped in `runtime/pr_monitor_runner/{runner,types}.py`, `runtime/release_pr_{monitor,sync}.py`; `service/worker.py` + `control/executor/monitor_handoff.py` build `gh` via `make_forge_client`; mypy clean |
| `forge:` detection (URL + workspace.yml override) resolves + persists `resolved_profile.forge`; precedence explicit > URL > github | `tests/unit/profiles/test_forge_resolution.py` (8 cases incl. round-trip persistence) |
| BitBucket repo detected → fails fast with `FORGE_NOT_SUPPORTED`; GitHub byte-for-byte unchanged | `tests/unit/control/test_executor_forge_gate.py` (failed + reason_code + no `gh pr create`); existing `github_client`/`pr_monitor`/resolver suites green |
| Full new-branch coverage; local lint/type gate green | ruff check + format clean; mypy `Success: no issues found in 315 source files` |
| Reason catalog + OpenAPI drift gates satisfied | `scripts/generate_reason_catalog.py` + `scripts/generate_openapi.py` regenerated; `tests/unit/docs/test_catalog_coverage.py`, `tests/unit/service/test_doctor_reasons.py`, `tests/unit/api/test_docs_drift.py` green |

## Commands run (focused)

```bash
pytest tests/unit/common/test_forge.py tests/unit/common/test_repo_ref_forge.py \
       tests/unit/profiles/test_forge_resolution.py \
       tests/unit/control/test_executor_forge_gate.py     # 58 passed
pytest tests/unit/common/test_github_client_parts ...      # 172 passed (regression)
pytest tests/unit/profiles                                 # 340 passed (regression)
pytest tests/unit/runtime/test_release_pr_sync.py \
       tests/unit/runtime/test_release_pr_monitor.py       # 24 passed
pytest tests/unit/service/test_doctor_reasons.py \
       tests/unit/docs/test_catalog_coverage.py \
       tests/unit/api/test_docs_drift.py                   # green
ruff check <changed files>                                 # All checks passed
ruff format --check .                                      # all formatted
mypy                                                       # Success: 315 files
python scripts/generate_reason_catalog.py                  # catalog regenerated
python scripts/generate_openapi.py                         # openapi.json regenerated (+forge)
```

## Regression directories (focused, not whole-repo / no coverage gate)

`tests/unit/common`, `tests/unit/profiles`, `tests/unit/runtime`,
`tests/unit/control` (executor + worker), `tests/unit/node`, `tests/unit/service`
— see final agent run notes. Whole-repo `pytest` + 99% coverage are owned by
AWF/GitHub CI after the agent exits (workspace contract).

## Notes / deviations

- Added a small `concrete_forge()` helper in `forge.py` (not named in the plan):
  legacy `resolved_profile` snapshots predate the `forge` field, so a
  reconstructed `WorkspaceProfile` reads `forge="auto"` for them — the
  construction sites normalize that (and `None`) to `github` so pre-existing
  GitHub workspaces never hit the fail-fast path. Also required for mypy
  (`profile.forge` is `ForgeKind | "auto"`).
- `BranchOpenPullRequestResolver` intentionally stays a separate collaborator
  (not on the Protocol), per the locked design.
- The executor forge gate is placed in `execution_flow.py` BEFORE non-feature
  dispatch (not only on the feature path) and reads the persisted
  `resolved_profile.forge`. This is a deliberate, stronger placement than the
  literal "after profile reconstruction" wording in the design doc: `sync_release_pr`
  builds `gh` in its monitor handoff and `_safely_execute` only logs (does not
  mark-failed) on an uncaught exception, so a per-path gate would have stranded a
  BitBucket `sync_release_pr` workspace in `running`. The single before-dispatch
  gate fails fast with `FORGE_NOT_SUPPORTED` for every task kind. Covered by
  `test_executor_forge_gate.py` parametrized over `feature_branch_pr` and
  `sync_release_pr`.
