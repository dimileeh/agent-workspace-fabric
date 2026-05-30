# PRRT_kwDOSJAM6s6FqIfC Pydantic Dump Floor Plan

## Problem Statement And Scope

PR #302 has an unresolved review thread on `src/awf/host_setup/rendering.py`
because `render_first_run_json()` passes `fallback=str` to
`BaseModel.model_dump()`. The project dependency floor allows
`pydantic>=2.9.0`, where that keyword is unsupported. The fix must preserve
first-run rendering behavior, especially arbitrary diagnostic object coercion
and redaction, without changing protected dependency configuration.

## Requirements Checklist

- Remove reliance on unsupported `model_dump(..., fallback=...)`.
- Preserve JSON-safe rendered payloads for arbitrary detail values.
- Preserve redaction of token/provider-ref content, including content produced
  by stringifying arbitrary objects.
- Add or update focused regression coverage for the Pydantic 2.9-compatible
  call path.
- Run only focused checks; full AWF/GitHub validation remains managed after
  agent completion.

## Implementation Steps

1. Add a regression test that fails if `render_first_run_json()` passes the
   unsupported `fallback` keyword to `FirstRunPayload.model_dump()`.
2. Update `render_first_run_json()` to dump in a supported mode and perform
   local redaction plus JSON-safe coercion.
3. Keep empty optional collection cleanup unchanged.
4. Run the focused rendering test module.
5. Record validation evidence in the matching validation document.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_rendering.py -q`
  must pass.
- Full AWF/GitHub validation is intentionally not run inside the agent phase.
