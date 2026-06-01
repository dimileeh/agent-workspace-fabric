# Address Thread PRRT_kwDOSJAM6s6GRP1J Plan

## Problem Statement And Scope

Retry admission computes the target worker node from `settings.worker_node_id`
without applying the same normalization used by create admission and the
worker/provisioner runtime. If `AWF_WORKER_NODE_ID` includes surrounding
whitespace, retry host-port conflict detection may scan the wrong node id and
miss an existing reservation recorded under the normalized node.

Scope is limited to retry workspace port admission and its regression coverage.

## Requirements Checklist

- Add a focused regression test showing retry rejects a same-node host-port
  conflict when `worker_node_id` has surrounding whitespace.
- Normalize retry target node identity consistently with create admission and
  worker/provisioner code.
- Keep existing source-runtime and host-port admission semantics unchanged.
- Run only targeted local checks for the changed behavior; broad AWF/GitHub
  validation remains managed by AWF after agent completion.

## Implementation Steps

1. Add a failing regression in `tests/unit/service/test_workspace_retry_port.py`
   for a retry whose target settings use `" node-a "` while an active
   conflicting reservation is stored as `"node-a"`.
2. Confirm the focused test fails against the current implementation when
   practical.
3. Update `src/awf/service/workspaces_retry.py` to use the shared node identity
   helper for configured worker node ids.
4. Re-run the focused retry-port test selection.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_retry_port.py -q`

Pass criteria: the retry-port unit tests pass locally. Full repository
validation, coverage, and CI-equivalent gates are intentionally left to AWF and
GitHub after this agent phase.
