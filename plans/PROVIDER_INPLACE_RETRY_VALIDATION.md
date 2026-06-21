# Provider In-Place Retry — Validation (SLICE 2/3, #612)

**Plan reference:** `plans/PROVIDER_INPLACE_RETRY_PLAN.md` (design + decisions D1–D5, tasks
T1–T9). Workspace plan: `docs/awf-plans/ws_a4f93f199ebe45aa982c6a2f.md`.

**Slice scope (this validation):** T2, T3, T4, T7, T8, T9 — the core in-place provider-retry
loop: ENTER `recovering` on a retryable provider failure at BOTH agent-run failure forks, and
RESUME the agent in place after the cooldown. Python only — no `apps/console` (slice 3).

**Slice 1 (already merged):** `WorkspaceStatus.recovering` enum + state-machine edges + status
accounting. Confirmed still green (see regression evidence below).

**Code under validation:** committed in `19e424061` (`feat: #612 slice 2/3 …`), diffed against
the slice-1 parent `cda077704`. This iteration adds the missing validation artifact (this file)
and records focused-test evidence; no behavioral code change was required — the implementation
already conforms to the plan.

---

## Requirement-by-requirement status

### ENTER `recovering` (T3 + T7 + T8)

| Req | Status | Evidence |
| --- | --- | --- |
| **T7** — hoist classify+budget decision UP to the fork via a single-sourced helper (no duplication of `decide_provider_recovery`) | **Complete** | `should_recover_in_place` at `src/awf/service/provider_recovery.py:321` wraps `provider_recovery_metadata_from_failure` + `decide_provider_recovery`; returns an in-place decision only for a same-agent `retry`. Unit-tested in `tests/unit/service/test_provider_recovery_in_place.py` (retry→decision, terminal→None, fallback/different-agent→None, non-retryable→None, auth→None). |
| **T3** — epoch-fenced `running → recovering` CAS BEFORE `_mark_failed`, persisting cooldown `not_before` + budget + provider reason | **Complete** | `enter_recovering_for_provider_failure` at `src/awf/control/executor/state_ops.py:543`. Wired at BOTH forks: `execution_flow.py:1091` (no-commits agent-failure fork, diverts with `return` before `_mark_failed`/`_prepare_provider_recovery`) and `quality_methods.py:1312` (post-agent-commit fork). Reuses the #421 `execution_claim` fence. Covered by `tests/unit/control/test_executor_recovering.py`. |
| **T3 regression** — budget-exhausted still → `failed` | **Complete** | Asserted in `test_executor_recovering.py` and the existing fork suites `test_executor_parts/test_executor_part_008.py`, `test_executor_error_paths_parts/test_executor_error_paths_part_001.py`, `test_executor_post_agent_commit_parts/test_executor_post_agent_commit_part_003.py`. |
| **T3 regression** — non-retryable still → `failed` | **Complete** | Same suites as above (non-retryable classification → no divert). |
| **T8** — widen worker finally-block teardown guards so `recovering` is treated like `blocked` (claim + warm stack retained) | **Complete** | `_release_execution_claim(skip_if_blocked=…)` at `src/awf/control/worker/claims.py:971` skips release when `ws.status in {blocked, recovering}`. `_release_terminal_runtime_promptly` no-ops for `recovering` (non-terminal). Covered by teardown-guard tests in `test_worker_recovering_resume.py`. |

### RESUME after cooldown (T2 + T4 + T9)

| Req | Status | Evidence |
| --- | --- | --- |
| **T2** — generalize blocked-resume machinery into a reason-parameterized shared path, behavior-preserving for `blocked` | **Complete** | `resume_paused_execution` (`executor/base.py:103`) with thin `resume_blocked_execution`/`resume_recovering_execution` shims (`:127`, `:147`); `_safely_resume_paused_claimed` (`dispatch_methods.py:222`), `_claim_paused_for_resume` (`claims.py:590`) with `_claim_recovering_for_resume`/`_restore_recovering_resume_claim` shims. **Behavior-preserving** confirmed by the CRITICAL regression suites below. |
| **T4** — worker re-dispatches a `recovering` ws once `now >= provider_cooldown_not_before`; epoch-fenced `recovering → running` CAS (double-resume → one wins); `git stash --include-untracked` then `git reset --hard HEAD` before re-invoking on the SAME warm stack (same ws id + attempt id) | **Complete** | Cooldown gate: `list_resumable_recovering_ids` (`workspace_repo_resumable.py:134`, `workspace_repo.py:703`) — node-scoped, FIFO, `not_before <= now`. CAS fence: `_claim_recovering_for_resume`. Stash+reset: dispatch helper at `dispatch_methods.py:173` (`git stash push --include-untracked` → `git reset --hard HEAD`, stash-failure aborts rather than discarding work). Covered by `test_worker_recovering_resume.py` (cooldown gate blocks early resume; stash+reset on dirty worktree; double-resume epoch fence → one wins). |
| **T9** — `recovering` branch of `_begin_execution` BYPASSES blocked-only semantics (no baseline reuse, no `pending_operator_hint` injection, no grant/`resume_disable_fix_passes`, no `resume_skip_agent` — always a fresh agent re-run on the reset worktree) | **Complete** | `recovering` branch in `state_ops.py` `_begin_execution`; returns `resume_skip_agent=False`, `resume_disable_fix_passes=False`, `baseline_coverage=None`, and ignores stray hint/grant. Covered by `test_executor_recovering.py` (incl. a case where a stray hint/grant is IGNORED). |

### CRITICAL regression — `blocked` stays green (T2 guard)

| Req | Status | Evidence |
| --- | --- | --- |
| `test_worker_blocked_resume.py` + `test_worker_execution_claim_fencing.py` pass UNCHANGED after the generalization | **Complete** | Both suites green (see run #1). |
| `test_state_machine.py` + `test_recovering_status_membership.py` (slice-1 edges) green | **Complete** | Both green (see run #1). |

---

## Evidence — focused test runs

Per the AWF workspace contract, focused tests for the changed modules were run here against the
AWF-provided Postgres sidecar (`$AWF_TEST_DATABASE_URL`). The full `.awf/workspace.yml`
validation suite, the aggregate **99% coverage gate**, and OpenAPI/reason-catalog drift are
owned and run by AWF + GitHub CI after agent completion.

All recorded commands use the repo-standard `uv run --python 3.12 --extra dev` wrapper (the
canonical invocation per `CLAUDE.md` / `AGENTS.md`). Scope is intentionally focused on the
changed modules per the AWF workspace contract — the full `.awf/workspace.yml` bundle, the
aggregate 99% coverage gate, and OpenAPI/reason-catalog drift run under AWF + GitHub CI after
agent completion (see the note above), not inside the agent phase.

**Run #1 — blocked regression guard + state machine + slice-1 membership (CRITICAL):**
```bash
uv run --python 3.12 --extra dev pytest \
       tests/unit/control/test_worker_blocked_resume.py \
       tests/unit/control/test_worker_execution_claim_fencing.py \
       tests/unit/control/test_state_machine.py \
       tests/unit/control/test_recovering_status_membership.py -q
→ 132 passed in 23.86s
```

**Run #2 — new recovering enter/resume/teardown + provider helper + modified fork suites:**
```bash
uv run --python 3.12 --extra dev pytest \
       tests/unit/control/test_executor_recovering.py \
       tests/unit/control/test_worker_recovering_resume.py \
       tests/unit/service/test_provider_recovery_in_place.py \
       tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_001.py \
       tests/unit/control/test_executor_parts/test_executor_part_008.py \
       tests/unit/control/test_executor_post_agent_commit_parts/test_executor_post_agent_commit_part_003.py \
       tests/unit/db/test_operator_grants.py -q
→ 89 passed in 107.54s
```

**Static checks (changed files):**
```bash
uv run --python 3.12 --extra dev ruff check \
           src/awf/control src/awf/service/provider_recovery.py \
           src/awf/db/repositories/workspace_repo_resumable.py \
           src/awf/db/repositories/workspace_repo.py        → All checks passed!
uv run --python 3.12 --extra dev ruff format --check \
           src/awf/control src/awf/service/provider_recovery.py → 77 files already formatted
uv run --python 3.12 --extra dev mypy                            → Success: no issues found in 397 source files (mypy pins files = ["src/"])
```

**Protected-file & drift checks:**
```bash
git diff --name-only cda077704 HEAD | grep -E '<protected globs>'  → NONE (no pyproject/.github/.awf/.coveragerc/setup.cfg/apps/console)
uv run --python 3.12 --extra dev python scripts/generate_reason_catalog.py --check  → no drift (docs/REASON_CATALOG.md unchanged)
```

The new `recovering` resume reason codes are worker-internal constants
(`PROVIDER_RECOVERY_IN_PLACE_RESUME`, `RECOVERING_RESUME_*` revert reasons) in
`control/worker/constants.py`, not entries in the generated `FailureReason` catalog, so no
catalog regeneration was required.

---

## Coverage reasoning (99% gate is AWF-owned)

The slice ships dedicated tests alongside every new behavior: `test_executor_recovering.py`
(394 lines) covers the enter forks + `_begin_execution` recovering bypass;
`test_worker_recovering_resume.py` (388 lines) covers the cooldown gate, stash+reset, the
double-resume epoch fence, and the teardown guards; `test_provider_recovery_in_place.py`
(174 lines) covers `should_recover_in_place` decision branches. Modified existing fork suites
were updated to assert the budget-exhausted / non-retryable regressions still go `failed`. No
new unreachable/defensive code was added that would require a coverage exclusion. The aggregate
99% gate runs under AWF/CI after agent exit and was not lowered (no protected quality-gate file
was touched).

---

## Result

All planned SLICE-2 requirements (T2, T3, T4, T7, T8, T9) are **Complete**, with the CRITICAL
`blocked`-resume regression and slice-1 state-machine/membership suites green after the
`resume_paused_execution` generalization. No gaps remain in this slice. Out-of-scope items
(manual-retry dedup T5, merge-candidate-continuity + console surfacing T6) are deferred to
slice 3 by design.
