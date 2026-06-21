# Provider In-Place Retry — Validation (SLICE 3/3, FINAL, #612)

**Plan reference:** `plans/PROVIDER_INPLACE_RETRY_PLAN.md` (tasks **T5** + **T6**). Workspace
plan: `docs/awf-plans/ws_9d9d0187d022486fb0fb76f5.md`. The slice-2 validation
(`plans/PROVIDER_INPLACE_RETRY_VALIDATION.md`) explicitly deferred T5/T6 to this slice; this
document records the missing slice-3 evidence.

**Slice scope (this validation):** T5 (manual-retry dedup on `recovering`), T6a
(merge-candidate continuity DB test), T6b (`apps/console` `recovering` surfacing).

**Slices 1 + 2 (already merged):** `WorkspaceStatus.recovering` enum + state machine +
accounting (slice 1); failure-fork divert into `recovering` + epoch-fenced in-place resume
(slice 2). This slice only consumes them and adds the operator-facing pieces.

**Code under validation:** committed in `ce6a5a111` (`feat: #612 slice 3/3 …`). This iteration
adds the missing validation artifact (this file) and records focused-test + console evidence; no
behavioral code change was required — the committed implementation already conforms to the plan.

---

## Requirement-by-requirement status

### T5 — Dedup: manual retry on a `recovering` workspace

| Req | Status | Evidence |
| --- | --- | --- |
| New `WorkspaceRetryRecoveringInFlightError(WorkspaceRetryError)` (`error_code = "WORKSPACE_AUTO_RETRY_IN_FLIGHT"`) raised BEFORE the `RETRYABLE_WORKSPACE_STATUSES` check, before any workspace is created | **Complete** | `src/awf/service/workspaces.py:198`; guard at `src/awf/service/workspaces_retry.py:210-211` (runs under the existing `get_for_update` row lock, before the generic not-allowed check at `:213`). |
| Message carries the cooldown ETA + "cooldown protects against re-hitting the stalled provider"; ETA-`None` fallback branch | **Complete** | `workspaces.py:213-229` — reads `provider_cooldown_not_before(workspace.task_policy)`; both branches covered by `test_retry_on_recovering_workspace_is_noop_with_eta` and `test_retry_recovering_error_handles_missing_cooldown` in `tests/unit/service/test_workspace_retry.py`. |
| No duplicate workspace created (fixes `ws_d8a285`) | **Complete** | Service test asserts the workspace count is unchanged before/after the rejected retry. |
| 409 surfaced identically via REST/CLI/MCP with zero schema/OpenAPI/MCP churn | **Complete** | Route test (`tests/unit/api/test_workspace_retry.py`) asserts POST `/v1/workspaces/{id}/retry` on a `recovering` ws → **409** with `error_code` + ETA in `detail`. CLI/MCP surface the existing structured error body (no source change, per plan §T5). OpenAPI drift check: **OK** (below). |

### T6a — Merge-candidate continuity (Python test)

| Req | Status | Evidence |
| --- | --- | --- |
| In-place retry keeps SAME workspace id + SAME attempt id (no retry-lineage) → candidate canonical at `monitoring_pr` → auto-merge fires | **Complete** | `test_inplace_retry_keeps_single_canonical_candidate` in `tests/unit/db/test_task_attempts.py` drives one workspace/attempt through `…running → recovering → running → … → monitoring_pr`, then asserts `get_open_for_workspace_with_merge_inputs(workspace.id)` returns the candidate, `attempt.is_canonical_for_merge`, `candidate.ready`, and that **no second `TaskAttempt`** exists. |

### T6b — Console surfacing (`apps/console`)

| Req | Status | Evidence |
| --- | --- | --- |
| `statusTone("recovering") === "info"` (NOT `warn`) | **Complete** | `apps/console/lib/format.ts`; node:test case in `apps/console/lib/format.test.mjs`. |
| `statusGlyph("recovering") === "↻"` (distinct from blocked's `⏸`) | **Complete** | `format.ts` `statusGlyph`; node:test case asserts `↻` and `!== "⏸"`. |
| `recovering` in `lifecycleStages` as an in-flight (non-terminal) stage after `running` | **Complete** | `format.ts` `lifecycleStages` + `normalizeLifecycle`/`fallbackLifecycleStages` branches; node:test cases. |
| "Auto-retrying" KPI (info tone, `counts.recovering`) distinct from blocked's "Awaiting operator"; not folded into Running | **Complete** | `apps/console/components/console-dashboard.tsx`; Running-KPI spec stays green (`recovering: 0` fixture added). |
| Reverse-transition guard covers `recovering` (resume not flagged a step-back) | **Complete** | `apps/console/lib/recovery-format.ts`; node:test in `apps/console/lib/recovery-format.test.mjs`. |
| Type fan-out: `WorkspaceSaturationCounts.recovering` added; all constructors updated | **Complete** | `apps/console/lib/types.ts`, `console-dashboard-shared.tsx`, spec fixtures; `npm run typecheck` green (below). |

---

## Focused validation evidence

**Python focused suites (slice-3 paths):**
```bash
uv run --python 3.12 --extra dev pytest \
    tests/unit/service/test_workspace_retry.py tests/unit/db/test_task_attempts.py -q
→ 23 passed
uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspace_retry.py -q -k retry
→ 10 passed
```

**Static checks (changed files):**
```bash
uv run --python 3.12 --extra dev ruff check \
    src/awf/service/workspaces.py src/awf/service/workspaces_retry.py   → All checks passed!
uv run --python 3.12 --extra dev ruff format --check \
    src/awf/service/workspaces.py src/awf/service/workspaces_retry.py \
    tests/unit/service/test_workspace_retry.py tests/unit/api/test_workspace_retry.py \
    tests/unit/db/test_task_attempts.py                                 → 5 files already formatted
uv run --python 3.12 --extra dev mypy                                   → Success: no issues found in 399 source files
```

**Console validation (the task's `--test` overrides wire this; no profile/`.awf` edits):**
```bash
npm --prefix apps/console ci          → added 377 packages, 0 vulnerabilities
npm --prefix apps/console run lint     → eslint . (clean)
npm --prefix apps/console run typecheck→ next typegen + tsc --noEmit (✓ types generated, no errors)
npm --prefix apps/console run test     → node:test — # pass 209  # fail 0
npm --prefix apps/console run build    → Next build OK (routes incl. /api/awf/workspaces/[id]/retry)
```

**Protected-file & drift checks:**
```bash
git show --name-only ce6a5a111 | grep -E 'pyproject|^\.github/|^\.awf/|\.coveragerc|setup.cfg'
                                                                       → NONE
uv run --python 3.12 --extra dev python scripts/generate_openapi.py --check
                                                                       → OK: openapi.json matches
uv run --python 3.12 --extra dev python scripts/generate_reason_catalog.py --check
                                                                       → no drift (working tree clean)
```

---

## Coverage reasoning (full 99% gate is AWF/CI-owned)

The full `pytest --cov` aggregate gate is owned by AWF + GitHub CI after agent exit and was not
run or lowered here (no protected quality-gate file was touched). Focused coverage on the two
changed Python source files confirms every NEW slice-3 line/branch is exercised by the new tests:

```bash
uv run --python 3.12 --extra dev pytest \
    tests/unit/service/test_workspace_retry.py tests/unit/api/test_workspace_retry.py \
    tests/unit/db/test_task_attempts.py \
    --cov=awf.service.workspaces --cov=awf.service.workspaces_retry --cov-report=term-missing
```
- `workspaces.py` new error class (lines 198–233, incl. **both** the ETA-present and ETA-`None`
  message branches) — **none** appear in the term-missing list → fully covered.
- `workspaces_retry.py` new dedup guard (`if … == recovering: raise …`, lines 210–211) — **not**
  in the term-missing list (only the pre-existing not-found `:208` / not-allowed `:214` branches,
  unexercised by these focused tests, appear) → covered.

The low whole-file percentage in that focused run is expected — these three suites only touch the
retry/continuity paths; the rest of each file is covered by the broader suite the AWF/CI aggregate
gate runs. The console `format.ts` / `recovery-format.ts` new tone/glyph/lifecycle/normalize/
reverse-guard branches are covered by the new `apps/console/lib/*.test.mjs` node:test cases (part
of the 209 passing). No new unreachable/defensive code was added that would need a coverage
exclusion.

---

## Result

All planned SLICE-3 requirements (**T5**, **T6a**, **T6b**) are **Complete** and validated:
focused Python suites + ruff/format/mypy green; console ci/lint/typecheck/test(209)/build green;
no protected quality-gate file touched; OpenAPI + reason-catalog drift-free. This completes #612
(`Fixes #612`). The full Python suite + 99% aggregate coverage gate and the console Playwright
browser smoke remain owned by AWF + GitHub CI after the agent phase.
