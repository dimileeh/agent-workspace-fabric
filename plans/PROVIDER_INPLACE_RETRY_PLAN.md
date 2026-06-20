# Provider in-place retry (AWF #612)

Retry transient provider errors **in the same workspace** instead of fail-and-relaunch a fresh one —
preserving the committed work AND the merge-candidate provenance.

## Problem

When the agent run hits a transient provider failure (e.g. `AGENT_IDLE_TIMEOUT` — 3600s of stdout/stderr
silence, `adapters/base.py:42`), `classify_provider_failure` (`adapters/provider_failures.py:140`) marks it
`retryable=True` with a cooldown. Today AWF reacts by **fail-and-relaunch**: `_mark_failed(agent_failure)` →
`_prepare_provider_recovery` (`quality_methods.py:1317`) → `create_provider_recovery_attempt_row`
(`provider_recovery.py:304`). Because the source is already `failed`, it falls to the fresh-clone branch
(`provider_recovery.py:465-489`): a **new workspace id + new attempt id**, fresh worktree, fresh stack,
discarding the source's committed branch.

A transient provider failure mid-run (pre-PR) should NOT send the whole workspace to `failed`. Today it does,
and the fail-and-relaunch reaction causes three harms (all hit the WS-2 lineage, #609):
1. **Re-provision + work waste** — a fresh workspace re-provisions a new stack from scratch and re-runs the
   agent, discarding the source's warm stack and any committed progress.
2. **Duplicate-prone** — the relaunch is a separate workspace, so a manual `awf workspace retry` races the
   auto-relaunch → a duplicate (`ws_d8a285`); the failed source shows no "superseded by auto-retry" signal,
   which misleads the operator into the redundant retry.
3. **Downstream merge-candidate break** — the relaunch creates a NEW attempt with retry-lineage
   (`parent_attempt_id`/`redispatch_from_attempt_id`, `provider_recovery.py:504-509`). A `MergeCandidate`
   (`db/models.py:755`) is created later, when the relaunched ws reaches `monitoring_pr`
   (`make_attempt_canonical_and_create_candidate`, `workspace_repo.py:1049`) — and the retry-lineage breaks its
   canonicalization, so auto-merge on the relaunch's PR couldn't fire (the #609 manual squash). NOTE: the break
   is realized at the PR boundary, NOT pre-PR — an agent-run failure has no candidate yet (a candidate requires
   a PR url, `quality_repo.py:382`). In-place retry fixes this by keeping a single CLEAN attempt id (no retry
   lineage), so the candidate created when the same ws reaches `monitoring_pr` is canonical and auto-merge works.

## What already exists (REUSE — do not rebuild)

- **In-place retry, monitor case:** `_is_recoverable_monitoring_pr_source` + `_record_monitor_in_place_recovery`
  return `new_workspace_id == source.id, in_place=True` (`provider_recovery.py:809-857`); the monitor loop
  re-raises `ProviderRecoveryRetryError` (`provider_ops.py:270`) and retries against the SAME workspace/branch/PR,
  preserving the candidate. **#612 is extending this same pattern to the agent-run path.**
- **Warm-stack pause + epoch-fenced resume (the `blocked` feature, just shipped):** non-terminal status that keeps
  the warm stack + execution claim; `_dispatch_blocked_resumes` (`dispatch_methods.py:113`) →
  `_safely_resume_blocked_claimed` (CAS re-acquires the claim) → `resume_blocked_execution`
  (`executor/base.py:102`, `state_ops.py:617`). The blocked plan **explicitly anticipated** non-protected pauses
  ("the reason/type field leaves the door open").
- **Epoch fencing (#421):** `execution_claim_epoch` bump + CAS (`claims.py:90`, `workspace_repo.py:1125-1200`).
- **Decision scaffolding:** `decide_provider_recovery` already returns `action="retry"` with
  `target_agent == current_agent` (`provider_recovery.py:210`); `ProviderRecoveryAttemptResult.in_place`
  (`provider_recovery.py:115`) is the existing in-place signal; cooldown via `provider_cooldown_not_before`
  (`provider_recovery.py:668`).

## The break to fix: agent-run failure goes terminal before it can be reused

```
 CURRENT (fail-and-relaunch)                    PROPOSED (in-place retry)
 ───────────────────────────                    ─────────────────────────
 running                                         running
   │ provider stall (retryable)                    │ provider stall (retryable) + budget left
   ▼                                                ▼
 _mark_failed(agent_failure)                     PAUSE (non-terminal)  ← reuse blocked machinery
   │  state_machine: failed→destroying ONLY        │  keeps worktree + warm stack + execution claim
   │  cleanup.py:228 compose-down (stack gone)      │  cooldown: provider_cooldown_not_before
   ▼                                                ▼  (worker dispatch after cooldown)
 _prepare_provider_recovery → fresh clone        resume_blocked_execution (re-invoke agent in place)
   │  NEW ws id + NEW attempt id                    │  SAME ws id + SAME attempt id
   ▼                                                ▼
 retry ws runs from scratch                      agent continues from preserved worktree
   │  merge candidate under NEW id                  │  merge candidate keyed to SAME id
   ▼                                                ▼
 auto-merge MISSES source id → manual squash     auto-merge FIRES (id unchanged)
```

The blocker: `failed` has no resume edge (`state_machine.py:103` — `failed→destroying` only), and the terminal
transition eagerly tears down the stack (`_release_terminal_runtime_promptly`, `cleanup.py:228`). So the divert
must land in a **non-terminal** state BEFORE teardown fires.

## Design (full in-place retry) — architecture decisions RESOLVED (review §1)

1. **New `recovering` status (D1).** Add a distinct non-terminal `WorkspaceStatus.recovering` — NOT a reuse of
   `blocked` (which means "awaiting operator"; a provider pause auto-heals). Reuse the blocked feature's
   *machinery* (warm stack, held execution claim, epoch-fenced resume dispatch) under the new status.
   `recovering` ∈ active_total ∧ EXECUTION_IN_USE_STATUSES (holds a slot), NOT terminal, NOT the Running KPI;
   distinct console tone/glyph/KPI ("auto-retrying, no action needed", distinct from "awaiting you").
2. **Route the failure into `recovering` at the agent-run failure fork (`quality_methods.py:1300-1318`).** When
   `classify_provider_failure` → `retryable=True` AND retry budget remains, transition `running → recovering`
   (epoch-guarded CAS, reuse #421) BEFORE `_mark_failed`/`_release_terminal_runtime_promptly` fire, so the
   worktree + warm stack + claim survive. Record block-epoch + `provider_cooldown_not_before` + the failure reason.
3. **Keep the warm stack (D2).** The held execution claim IS the lease (recovery_stale won't reap it); resume is
   instant. Accept the slot-hold during the (usually 120s) cooldown.
4. **Resume re-entry after cooldown.** Worker re-dispatch (`_dispatch_blocked_resumes`-analogue →
   `resume_recovering_execution`, reuse `resume_blocked_execution`) once `now ≥ provider_cooldown_not_before`,
   epoch-fenced so two workers can't both resume. **Reset the worktree to HEAD, `git stash` the uncommitted dirt
   first (D3)** — a stalled-mid-edit tree is inconsistent; resume from the last clean commit; stash keeps the
   partial work recoverable. Re-run setup is NOT needed (warm stack); re-invoke the agent in place.
5. **Same workspace id + attempt id** → the (future) merge candidate is born under the same id; auto-merge fires
   with zero lineage threading (the exact break that forced the manual squash).
6. **Dedup (D4):** manual `awf workspace retry` on a `recovering` workspace is a no-op with an ETA message
   ("auto-retry in flight, resumes ~T"); the `recovering` status is the dedup guard. The auto path is idempotent
   via the `running → recovering` state-machine guard (a second failure event on an already-recovering ws can't
   re-pause).
7. **Budget exhaustion / non-retryable:** fall through to today's terminal `failed` (+ existing fresh-relaunch
   fallback policy unchanged).

```
 RESUME (after cooldown)
 git stash (preserve partial dirt) → git reset --hard HEAD (clean, consistent)
   → epoch-fenced CAS recovering→running → resume_blocked_execution (re-invoke agent on warm stack)
```

8. **Generalize the resume machinery (D5, review §2).** Refactor `resume_blocked_execution` /
   `_dispatch_blocked_resumes` / `_safely_resume_blocked_claimed` into reason-parameterized
   `resume_paused_execution(reason=blocked|recovering)` so both pause causes flow through ONE audited
   concurrency path (epoch CAS + lease heartbeat live once). Behavior-preserving for blocked (its tests pin it),
   then add the `recovering` reason. "Make the change easy, then make the easy change."

## Test coverage (review §3)

```
CODE PATHS                                                         COVERAGE
[+] executor failure fork (quality_methods.py:1300-1318)
  ├── retryable + budget left      → recovering        [GAP] CRITICAL (regression: was → failed)
  ├── retryable + budget exhausted → failed (today)    [GAP] (boundary)
  └── non-retryable                → failed (today)    [GAP] (regression: path unchanged, assert still failed)
[+] state machine
  ├── running → recovering (epoch CAS)                 [GAP]
  ├── recovering → running (resume, epoch-fenced)      [GAP]
  ├── recovering → failed (budget exhausted on re-fail)[GAP]
  └── recovering → {cancelled}                         [GAP] (operator cancel a recovering ws)
[+] status accounting                                  [GAP] recovering ∈ active∧slot, NOT Running KPI (parametrized, mirror blocked)
[+] resume path (resume_paused_execution)
  ├── stash + reset-to-HEAD before resume              [GAP] (dirty worktree → clean resume; stash recoverable)
  ├── two workers resume same ws                       [GAP] (epoch fence → 1 wins, #421 invariant)
  ├── cooldown gate (now < not_before → no resume)     [GAP]
  └── blocked still resumes (generalization regression)[GAP] CRITICAL (blocked tests must stay green)
[+] dedup: manual retry on recovering                  [GAP] (no-op + ETA, no duplicate ws — the #612 incident)
[+] merge-candidate continuity                         [GAP] same id → get_open_for_workspace_with_merge_inputs finds it → auto-merge fires
[+] warm stack held during cooldown                    [GAP] recovery_stale does NOT reap a recovering ws (held claim = lease)

COVERAGE: 0/15 (new feature) — all paths need tests written alongside.
REGRESSIONS (CRITICAL, mandatory): failure-fork now→recovering; blocked-resume still works post-generalization; non-retryable still→failed.
```

## Implementation Tasks
- [ ] **T1 (P1)** — state machine + status: add `WorkspaceStatus.recovering` + transitions (running→recovering, recovering→{running,failed,cancelled}) + accounting (active∧slot, not Running KPI). Verify: state-bucket + transition unit tests.
- [ ] **T2 (P1)** — generalize resume machinery to `resume_paused_execution(reason)` (behavior-preserving for blocked). Verify: existing blocked-resume tests green + new param tests.
- [ ] **T3 (P1)** — failure fork: route retryable+budget → recovering (epoch-guarded) BEFORE terminal teardown; record cooldown/epoch/reason. Verify: fork unit tests (3 branches) incl. regression.
- [ ] **T4 (P1)** — resume re-entry: cooldown gate + stash+reset-to-HEAD + epoch-fenced CAS + re-invoke. Verify: resume tests incl. double-resume fence + dirty-worktree.
- [ ] **T5 (P1)** — dedup: manual `awf workspace retry` on recovering = no-op + ETA. Verify: CLI/service test (no duplicate ws).
- [ ] **T6 (P1)** — merge-candidate continuity + surfacing (console tone/glyph/KPI "auto-retrying", distinct from blocked). Verify: candidate-found-by-source-id test + console node:tests.

### Feasibility hardening (folded from the outside-voice challenge — these are why this is L, not M)
- [ ] **T7 (P1)** — lift the retryable+budget decision UP to the executor failure fork. Both agent-run forks
  (`execution_flow.py:1053-1062` AND `quality_methods.py:1300-1318`) currently call `_mark_failed`
  UNCONDITIONALLY, and `decide_provider_recovery` runs DOWNSTREAM inside `create_provider_recovery_attempt_row`
  (`provider_recovery.py:340`). To divert `running→recovering` *before* teardown, the classify+budget check must
  move (or be hoisted) into the fork, before `_mark_failed`. Verify: both-fork unit tests.
- [ ] **T8 (P1)** — widen the worker finally-block teardown guards to recognize `recovering` like `blocked`:
  `_release_execution_claim(skip_if_blocked=...)` (`claims.py:879-900`, literally checks `status == blocked`) and
  `_release_terminal_runtime_promptly`/runtime-release in the worker finally (`dispatch_methods.py:446`,
  `claims.py:731`). Otherwise the warm stack + claim are torn down despite the non-terminal status. Verify:
  worker-finally test asserts a `recovering` ws keeps its claim + stack.
- [ ] **T9 (P1)** — the `recovering` branch of `resume_paused_execution` must BYPASS blocked-only semantics
  (`_begin_execution(resume_from_blocked=True)`, `state_ops.py:654-685`: `block_baseline_coverage`,
  `pending_operator_hint` directive injection, `_active_operator_grant_specs`, `resume_skip_agent`). A
  `recovering` resume is a fresh agent re-run on the warm stack from the reset worktree — NOT a grant/hint replay.
  So D5 is not a pure rename; it adds a clean recovering branch while keeping blocked's tests green. NOTE the new
  status inherits the `blocked` special-casing blast radius (~17 source files: metrics, capacity, gc_classify,
  orphan_resources, overlap_graph, claims, manager, workspaces_response, + console) — mirror each site.

## NOT in scope
- Generalizing the pause to arbitrary non-provider, non-protected causes (only the provider-recovery cause wired).
- Changing the cooldown/retry-budget policy values (reuse existing `decide_provider_recovery`).
- The monitor in-place path (already exists; this only extends the agent-run path).
- **Provider-wide-outage thundering-herd (review §4, deferred → TODOS.md):** a broad provider outage idle-timeouts
  many running workspaces at once → many `recovering` holds → capacity starvation. Mitigation (concurrent-recovery
  cap + circuit-breaker, or free slots past a cooldown threshold) is its own design; out of scope for the
  single-blip core. The warm-hold is consistent with how `blocked` already holds slots for operator pauses.

## Failure modes (to cover)
- Stale executor clobbers the pause→running transition → epoch-guarded CAS (reuse #421).
- Two workers resume the same paused ws → epoch fence.
- Warm stack reaped while paused → held claim = lease (reuse blocked invariant) OR re-provision (OPEN-2).
- Manual retry + auto in-place retry both fire → dedup (OPEN-4).
- Resume on a dirty/partial worktree from the idle-timeout kill → stash + reset-to-HEAD before resume (D3).

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | not run |
| Outside Voice | `/codex review` (Claude subagent fallback) | Independent 2nd opinion | 1 | issues_found | challenged scope (re-scope REJECTED by user); 3 feasibility findings folded → T7-T9 |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | clean | 6 issues resolved (4 arch, 1 code-quality, 1 perf→TODO) |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | not run (console surfacing mirrors `blocked`) |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | not run |

- **CODEX:** codex CLI auth failed at runtime (refresh token expired — `codex login` needed); the outside voice ran via the Claude subagent fallback.
- **CROSS-MODEL:** the outside voice argued to re-scope to a cheap surfacing+dedup fix (no merge candidate exists pre-PR; the incident's auto-retry already worked). The user REJECTED the re-scope — a transient provider failure mid-run must be retried IN-PLACE, not flush the workspace to `failed`. The merge-candidate framing was CORRECTED: the benefit is a single CLEAN attempt-id lineage realized at the PR boundary (fixing #609's downstream candidate break), not a pre-PR candidate. The outside voice's 3 valid FEASIBILITY findings (decision computed downstream of teardown → T7; teardown guards must recognize `recovering` → T8; `resume_blocked_execution` carries blocked-only semantics → T9) were absorbed, not rejected.
- **VERDICT:** ENG CLEARED — full in-place provider retry: new `recovering` status, warm-stack hold, stash+reset-to-HEAD resume, `recovering`-as-dedup-guard, generalized `resume_paused_execution`, with feasibility hardening T7-T9. Ready to implement.

NO UNRESOLVED DECISIONS
