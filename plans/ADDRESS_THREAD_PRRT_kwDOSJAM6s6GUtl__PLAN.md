# Address PRRT_kwDOSJAM6s6GUtl_ Plan

## Problem Statement And Scope

Retry admission currently gathers source host-port claims from retry companions and
`source.resolved_profile`. Legacy/pre-resolution retry sources can have an inline
`requested_profile` with service host ports while `resolved_profile` is still
`None`. In that case admission misses the source profile ports and can create a
retry that fails later in the provisioner instead of returning the source-runtime
409 at retry admission.

Scope is limited to retry admission port discovery for source profile snapshots
and a focused regression test.

## Requirements Checklist

- Add a regression for a legacy inline `requested_profile` with service host
  ports and no `resolved_profile`.
- Ensure retry admission includes those requested-profile service ports only
  when the source has no resolved profile snapshot.
- Preserve existing resolved-profile precedence for modern sources.
- Keep validation focused; broad AWF/GitHub validation remains managed after
  agent completion.

## Implementation Steps

1. Add a focused service retry-port unit test that fails before the fix.
2. Update `retry_workspace_row` host-port collection to fall back from
   `source.resolved_profile` to `source.requested_profile` when needed.
3. Run the focused regression test.
4. Record validation results in the matching validation document.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_retry_port.py -q -k requested_profile`
  must pass.
- Do not run full coverage, whole-repository unit suites, frontend builds, or
  CI-equivalent validation during the agent phase.
