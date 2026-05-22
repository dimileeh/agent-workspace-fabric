# Agent Timeout Salvage Continuation Plan

## Problem Statement

Two AWF dogfood workspaces produced useful implementation diffs, then failed after
the agent process emitted no output for the idle timeout window. The current
provider-recovery path created fresh retry workspaces and lost the useful
in-progress diffs. One failed workspace also exposed a staging bug where AWF tried
to stage an ignored directory path instead of staging the changed tracked files.

AWF should demonstrate that it can recover its own work: when an operator retries
a failed workspace whose source failed from an agent idle timeout and still has an
implementation diff, AWF should capture that diff as a salvage artifact and make
the retry workspace continue from it.

## Scope

- Extend the existing conformance-salvage mechanism to support agent timeout
  continuation retries.
- Keep the behavior operator-driven through the existing retry path for this
  patch; do not rework provider recovery scheduling in this slice.
- Do not hand-implement the release-task or LLM-usage product changes in the
  main worktree. Use the repaired AWF retry path to launch replacement workspaces
  that continue from the preserved failed worktrees.
- Keep validation commands project-local and narrow.

## Requirements Checklist

- Retry detects failed/cancelled source workspaces whose latest failure reason is
  `AGENT_IDLE_TIMEOUT` or `AGENT_TIMEOUT`.
- If the timed-out source has an implementation diff, retry captures it with the
  same temp-index, binary patch artifact flow used for conformance salvage.
- The retry task policy records the salvage metadata and the executor can apply
  the patch using the existing `conformance_salvage` execution path.
- The retry prompt tells the agent this is an automatic AWF timeout salvage and
  instructs it to continue from the recovered work.
- Existing conformance retry behavior remains unchanged.
- Plan-only timeout diffs do not force salvage and can still retry normally.
- Unit coverage proves timeout salvage capture and prompt/payload metadata.
- Unit coverage proves ignored parent-directory patterns do not prevent salvage
  when changed files under that directory are already tracked.
- After the control-plane fix is applied, restart AWF and use AWF retries for the
  two failed dogfood source workspaces.

## Implementation Steps

1. Add a timeout-salvage prompt builder beside the existing conformance salvage
   helpers.
2. Add a small timeout retry context helper in `src/awf/service/workspaces.py`.
3. Reuse `capture_conformance_salvage()` for timeout sources, with timeout
   evidence and no conformance evidence ref.
4. Attach the salvage metadata to the retry task policy and operation/event
   payloads.
5. Add focused service tests for timeout salvage, no-diff fallback retry, and
   ignored tracked path capture.
6. Run the narrow service tests and lint/type checks for touched Python files
   where practical.
7. Create a validation document with requirement-by-requirement evidence.

## Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_retry.py tests/unit/service/test_conformance_salvage.py -q
uv run --python 3.12 --extra dev ruff check src/awf/service/conformance_salvage.py src/awf/service/workspaces.py tests/unit/service/test_workspace_retry.py tests/unit/service/test_conformance_salvage.py
uv run --python 3.12 --extra dev mypy src/awf/service/conformance_salvage.py src/awf/service/workspaces.py
```

Pass criteria: the targeted tests pass, lint/type checks pass or any pre-existing
unrelated failures are documented, and AWF creates replacement workspaces for the
failed dogfood tasks using timeout-salvage metadata.
