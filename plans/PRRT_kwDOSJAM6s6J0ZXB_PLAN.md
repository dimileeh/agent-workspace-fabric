# PRRT_kwDOSJAM6s6J0ZXB Wire blocked resumes into the worker loop

## Scope
The pre-PR `blocked`-resume feature shipped its building blocks
(`_claim_blocked_for_resume`, `_dispatch_blocked_resumes`,
`_safely_resume_blocked_claimed`, `executor.resume_blocked_execution`, the
`ORDERED_BLOCKED_RESUME_REASON` constant) but never wired them into the worker
loop. `run_once()` only dispatched `requested`/`monitoring_pr`/`ready`, and
`WorkerDelegatesMixin` did not bind the blocked-resume claim/dispatch helpers.
A `blocked` workspace therefore stayed blocked forever after an operator grant
or directive — the reviewer's finding is correct and blocking.

## Steps
1. Add `WorkspaceRepository.list_resumable_blocked_ids()` returning only
   `blocked` workspaces an operator has cleared (a `pending_operator_hint`
   directive armed, or a grant active for the current `block_epoch`). Workspaces
   still awaiting an operator decision are excluded so the worker never spins
   `blocked -> running -> blocked`.
2. Add `_claim_blocked_resume_ids` batch helper in `claims.py` mirroring
   `_claim_monitoring_pr_ids` (per-row `blocked -> running` CAS).
3. Add `_list_resumable_blocked` on `ControlWorker` (FIFO by `updated_at`).
4. Bind `_claim_blocked_for_resume`, `_claim_blocked_resume_ids`,
   `_dispatch_blocked_resumes`, `_safely_resume_blocked_claimed` in `mixins.py`.
5. Wire a blocked-resume dispatch block into `run_once()` between monitor
   resumes and ready executions (paused in-flight work before fresh work).
6. Add focused regression tests: repository eligibility query + worker `run_once`
   resumes a cleared workspace and leaves an undecided one untouched.
7. Run only the narrow affected tests; AWF/GitHub own broad validation.

## Out of scope
No change to the executor resume semantics, the guide surface, or merge gates —
only the missing worker-loop wiring.
