# Review Thread PRRT_kwDOSJAM6s6GXkpC Resume Scan On Release Errors Validation

Plan reference:
`plans/review_thread_PRRT_kwDOSJAM6s6GXkpC_resume_scan_on_release_errors_PLAN.md`

## Requirement Status

- Run `_resume_pending_planning_scope_auto_retries_after_terminal_release`
  during a release scan even when one or more runtime-release candidates fail:
  Complete.
- Preserve existing per-candidate cleanup behavior and continue processing the
  release batch after a candidate error: Complete.
- Preserve existing error propagation from `_release_terminal_runtime_resources`
  after the safety-net resume scan has had a chance to run: Complete.
- Preserve cancellation semantics: `asyncio.CancelledError` must still stop the
  scan immediately: Complete.
- Add focused regression coverage before implementation: Complete.

## Evidence

Files changed:

- `src/awf/control/worker/cleanup.py`
- `tests/unit/control/test_worker_parts/test_worker_part_042.py`

Plan/validation artifacts:

- `plans/review_thread_PRRT_kwDOSJAM6s6GXkpC_resume_scan_on_release_errors_PLAN.md`
- `plans/review_thread_PRRT_kwDOSJAM6s6GXkpC_resume_scan_on_release_errors_VALIDATION.md`

Focused failing-before-fix regression run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_parts/test_worker_part_042.py::TestTerminalRuntimeReleasePart003::test_release_scan_runs_planning_scope_resume_safety_net_before_raising_release_error -q
```

Result before implementation: failed as expected. The runtime release error was
raised and `resumed` remained empty, proving the safety-net resume scan was
skipped.

Focused passing regression and neighbor run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_parts/test_worker_part_042.py::TestTerminalRuntimeReleasePart003::test_release_continues_batch_when_per_candidate_recording_raises tests/unit/control/test_worker_parts/test_worker_part_042.py::TestTerminalRuntimeReleasePart003::test_release_scan_runs_planning_scope_resume_safety_net_before_raising_release_error -q
```

Result after implementation: `2 passed in 4.23s`.

Focused release-scan auto-retry run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_parts/test_worker_part_042.py::TestTerminalRuntimeReleasePart003::test_release_scan_resumes_pending_planning_scope_auto_retry_after_recorded_release tests/unit/control/test_worker_parts/test_worker_part_042.py::TestTerminalRuntimeReleasePart003::test_release_scan_ignores_blocked_planning_scope_auto_retry_after_plain_manual_retry tests/unit/control/test_worker_parts/test_worker_part_042.py::TestTerminalRuntimeReleasePart003::test_default_local_release_scan_resumes_pending_planning_scope_auto_retry_on_local_node tests/unit/control/test_worker_parts/test_worker_part_042.py::TestTerminalRuntimeReleasePart003::test_release_continues_batch_when_per_candidate_recording_raises tests/unit/control/test_worker_parts/test_worker_part_042.py::TestTerminalRuntimeReleasePart003::test_release_scan_runs_planning_scope_resume_safety_net_before_raising_release_error -q
```

Result: `5 passed in 8.61s`.

Focused lint:

```bash
uv run --python 3.12 --extra dev ruff check src/awf/control/worker/cleanup.py tests/unit/control/test_worker_parts/test_worker_part_042.py
```

Result: `All checks passed!`

Full AWF/GitHub validation was not run in the agent phase because the workspace
contract assigns broad validation, provenance, logs, timeouts, and merge gating
to AWF/GitHub after agent completion.
